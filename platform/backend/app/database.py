import os
import logging
from datetime import datetime
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

logger = logging.getLogger(__name__)

# Local development is self-contained. Docker and hosted deployments provide an
# explicit PostgreSQL DATABASE_URL.
DEFAULT_DB_URL = "sqlite:///./syda_platform.db"
DATABASE_URL = os.getenv("DATABASE_URL", DEFAULT_DB_URL)

# SQLAlchemy 1.4+ compatibility: replace postgres:// with postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False},
    )
else:
    engine = create_engine(
        DATABASE_URL,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
    )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

if DATABASE_URL.startswith("postgresql://"):
    ASYNC_DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("sqlite:"):
    ASYNC_DATABASE_URL = DATABASE_URL.replace("sqlite:", "sqlite+aiosqlite:", 1)
else:
    ASYNC_DATABASE_URL = DATABASE_URL

async_engine = create_async_engine(ASYNC_DATABASE_URL, pool_pre_ping=True)
AsyncSessionLocal = async_sessionmaker(async_engine, expire_on_commit=False)


async def get_async_db():
    async with AsyncSessionLocal() as session:
        yield session


def get_db():
    """Dependency that yields a request-scoped database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Creates tables if they do not already exist."""
    try:
        # Ensure every table is registered before create_all, including FastAPI Users.
        from app.models.user import User
        from app.models.entities import JobRecord, ScenarioRecord

        Base.metadata.create_all(bind=engine)
        # Existing local installations created the scenarios table before it stored
        # the complete schema and workflow paths. Add the JSON column in place.
        columns = {column["name"] for column in inspect(engine).get_columns("scenarios")}
        if "configuration" not in columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE scenarios ADD COLUMN configuration JSON"))
        job_columns = {column["name"] for column in inspect(engine).get_columns("generation_jobs")}
        if "scenario_id" not in job_columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE generation_jobs ADD COLUMN scenario_id VARCHAR(36)"))
        conversation_columns = {column["name"] for column in inspect(engine).get_columns("chat_conversations")}
        with engine.begin() as connection:
            if "scenario_draft" not in conversation_columns:
                connection.execute(text("ALTER TABLE chat_conversations ADD COLUMN scenario_draft JSON"))
            if "draft_version" not in conversation_columns:
                connection.execute(text("ALTER TABLE chat_conversations ADD COLUMN draft_version INTEGER NOT NULL DEFAULT 0"))
        owner_type = "UUID" if engine.dialect.name == "postgresql" else "CHAR(32)"
        for table_name in ("scenarios", "generation_jobs"):
            columns = {column["name"] for column in inspect(engine).get_columns(table_name)}
            if "user_id" not in columns:
                with engine.begin() as connection:
                    connection.execute(text(
                        f'ALTER TABLE {table_name} ADD COLUMN user_id {owner_type} '
                        'REFERENCES "user"(id) ON DELETE CASCADE'
                    ))
            with engine.begin() as connection:
                connection.execute(text(f"CREATE INDEX IF NOT EXISTS ix_{table_name}_user_id ON {table_name}(user_id)"))
        # The old app had a single shared workspace. If it has exactly one account,
        # preserve its existing scenarios, runs, settings, and API keys under that user.
        from app.provider_settings import adopt_legacy_shared_data
        with SessionLocal() as session:
            users = session.query(User).order_by(User.id).limit(2).all()
            if len(users) == 1:
                adopt_legacy_shared_data(session, users[0].id)
            elif len(users) > 1:
                unowned = (
                    session.query(ScenarioRecord).filter(ScenarioRecord.user_id.is_(None)).count()
                    + session.query(JobRecord).filter(JobRecord.user_id.is_(None)).count()
                )
                if unowned:
                    logger.warning(
                        "Found %s legacy shared records with multiple accounts; left unassigned to avoid exposing them to the wrong user.",
                        unowned,
                    )
        # Generation currently runs inside the API process. An unfinished job
        # cannot resume after restart, so persist its interrupted state.
        with SessionLocal() as session:
            interrupted = session.query(JobRecord).filter(JobRecord.status.in_(["generating", "validating", "evaluating"])).update({
                JobRecord.status: "failed",
                JobRecord.current_stage: "Interrupted by backend restart.",
                JobRecord.updated_at: datetime.utcnow(),
            }, synchronize_session=False)
            session.commit()
            if interrupted:
                logger.warning("Marked %s interrupted generation jobs as failed", interrupted)
        logger.info("Database schema initialized successfully.")
    except Exception as e:
        logger.error(f"Error initializing database schema: {e}")
        raise
