"""FastAPI Users cookie authentication and shared-workspace identity routes."""

import logging
import os
import secrets
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi_users import BaseUserManager, FastAPIUsers, UUIDIDMixin, InvalidPasswordException, schemas
from fastapi_users.authentication import AuthenticationBackend, CookieTransport, JWTStrategy
from fastapi_users_db_sqlalchemy import SQLAlchemyUserDatabase
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import SessionLocal, get_async_db
from app.models.entities import JobRecord, ScenarioRecord
from app.models.user import User

logger = logging.getLogger(__name__)


def _auth_secret() -> str:
    configured = os.getenv("AUTH_SECRET_KEY")
    if configured:
        return configured
    path = Path(__file__).resolve().parents[2] / "data" / "settings" / "auth_secret"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        secret = secrets.token_urlsafe(64)
        path.write_text(secret)
        os.chmod(path, 0o600)
        return secret


SECRET = _auth_secret()
COOKIE_TRANSPORT = CookieTransport(
    cookie_name="syda_session",
    cookie_max_age=30 * 24 * 60 * 60,
    cookie_secure=os.getenv("COOKIE_SECURE", "false").lower() == "true",
    cookie_samesite="lax",
)


async def get_user_db(session: AsyncSession = Depends(get_async_db)):
    yield SQLAlchemyUserDatabase(session, User)


class UserManager(UUIDIDMixin, BaseUserManager[User, uuid.UUID]):
    reset_password_token_secret = SECRET
    verification_token_secret = SECRET

    async def validate_password(self, password: str, user) -> None:
        if len(password) < 10 or len(password) > 1024:
            raise InvalidPasswordException(reason="Use a password between 10 and 1024 characters.")

    async def on_after_register(self, user: User, request: Request | None = None) -> None:
        # If an account is added to an installation that still has pre-auth data,
        # adopt that legacy shared data for the first account only.
        from app.provider_settings import adopt_legacy_shared_data

        with SessionLocal() as session:
            account_count = session.query(User.id).count()
            if account_count == 1:
                adopt_legacy_shared_data(session, user.id)
            else:
                legacy_count = (
                    session.query(ScenarioRecord).filter(ScenarioRecord.user_id.is_(None)).count()
                    + session.query(JobRecord).filter(JobRecord.user_id.is_(None)).count()
                )
                if legacy_count:
                    logger.warning("Leaving %s legacy shared records unassigned after another account registered.", legacy_count)


async def get_user_manager(user_db=Depends(get_user_db)):
    yield UserManager(user_db)


def get_jwt_strategy() -> JWTStrategy:
    return JWTStrategy(secret=SECRET, lifetime_seconds=30 * 24 * 60 * 60)


auth_backend = AuthenticationBackend(
    name="cookie",
    transport=COOKIE_TRANSPORT,
    get_strategy=get_jwt_strategy,
)
fastapi_users = FastAPIUsers[User, uuid.UUID](get_user_manager, [auth_backend])
current_active_user = fastapi_users.current_user(active=True)

router = APIRouter(tags=["Authentication"])
router.include_router(fastapi_users.get_auth_router(auth_backend), prefix="/auth/cookie")


class UserRead(schemas.BaseUser[uuid.UUID]):
    display_name: str


class UserCreate(schemas.BaseUserCreate):
    display_name: str = Field(min_length=1, max_length=120)


router.include_router(fastapi_users.get_register_router(UserRead, UserCreate), prefix="/auth")


@router.get("/auth/me", response_model=UserRead)
async def me(user: User = Depends(current_active_user)):
    return user


async def require_authenticated(request: Request, user: User = Depends(current_active_user)) -> User:
    request.state.current_user = user
    return user
