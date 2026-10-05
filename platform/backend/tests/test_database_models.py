"""Tests for SQLAlchemy 2.0 models, timezone awareness, and database initialization."""

import uuid
from datetime import datetime, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, init_db
from app.models.entities import (
    ChatConversation,
    JobRecord,
    ScenarioRecord,
    UserPreference,
    UserProviderCredential,
    WorkspacePreference,
    utcnow,
)
from app.models.user import User
from app.api.agent import _scenario_is_valid_draft
from app.models.scenario import ScenarioConfiguration
from app.provider_settings import provider_preference


def test_utcnow_is_timezone_aware():
    now = utcnow()
    assert now.tzinfo is not None
    assert now.tzinfo == timezone.utc


def test_models_mapped_attributes_and_serialization():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    with TestingSession() as session:
        user = User(
            id=uuid.uuid4(),
            email="models-test@example.com",
            hashed_password="test-hashed-pwd",
            is_active=True,
            is_superuser=False,
            is_verified=True,
            display_name="Tester",
        )
        session.add(user)
        session.commit()

        # ChatConversation
        convo = ChatConversation(
            user_id=user.id,
            title="Initial Title",
            messages=[{"role": "user", "content": "hi"}],
            runs=[{"run_id": "r1"}],
            scenario_draft={"title": "Draft"},
            draft_version=1,
        )
        session.add(convo)
        session.commit()

        # Update attributes directly
        convo.title = "Updated Title"
        convo.draft_version = convo.draft_version + 1
        convo.updated_at = datetime.now(timezone.utc)
        convo.messages = [{"role": "user", "content": "hi again"}]
        session.commit()

        d = convo.to_dict(include_content=True)
        assert d["title"] == "Updated Title"
        assert d["draftVersion"] == 2
        assert d["messageCount"] == 1
        assert len(d["messages"]) == 1

        # UserPreference & WorkspacePreference
        pref = UserPreference(user_id=user.id, key="provider", value="openai")
        ws_pref = WorkspacePreference(key="theme", value="dark")
        cred = UserProviderCredential(user_id=user.id, provider="openai", encrypted_key="enc123")
        session.add_all([pref, ws_pref, cred])
        session.commit()

        assert pref.value == "openai"
        assert ws_pref.value == "dark"
        assert cred.encrypted_key == "enc123"

        # provider_preference helper returns plain str
        assert provider_preference(session, user.id) == "openai"
        assert provider_preference(session, None) == "automatic"


def test_scenario_is_valid_draft():
    assert not _scenario_is_valid_draft(None)

    valid_config = ScenarioConfiguration(
        title="Valid scenario",
        description="A valid scenario draft",
        recordCount=100,
        secondaryMetric={"label": "Tables", "value": "2"},
        workflow=["Users", "Orders"],
        rules=["Rule 1"],
        schemas={"Users": {"id": "integer"}, "Orders": {"id": "integer"}},
    )
    assert _scenario_is_valid_draft(valid_config)

    invalid_config = ScenarioConfiguration(
        title="Invalid",
        description="Missing workflow",
        recordCount=100,
        secondaryMetric={"label": "Tables", "value": "1"},
        workflow=["Users"],  # Less than 2 steps
        rules=["Rule 1"],
        schemas={"Users": {"id": "integer"}},
    )
    assert not _scenario_is_valid_draft(invalid_config)


def test_init_db_runs_successfully():
    init_db()
