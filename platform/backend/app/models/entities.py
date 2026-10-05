import uuid
from typing import Any
from datetime import datetime, timezone
from sqlalchemy import String, Integer, Text, JSON, DateTime, ForeignKey, Uuid
from sqlalchemy.orm import Mapped, mapped_column
from ..database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JobRecord(Base):
    """Stores synthetic data generation job executions and evaluation states."""

    __tablename__ = "generation_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()), index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("user.id", ondelete="CASCADE"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="generating", index=True)
    scenario_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    current_stage: Mapped[str] = mapped_column(String(255), default="Initializing")
    scenario: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    stats: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    download_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    def to_dict(self):
        return {
            "jobId": self.id,
            "status": self.status,
            "scenarioId": self.scenario_id,
            "progress": self.progress,
            "currentStage": self.current_stage,
            "scenario": self.scenario,
            "stats": self.stats,
            "downloadUrl": self.download_url,
            "createdAt": self.created_at.isoformat() if self.created_at else None,
            "updatedAt": self.updated_at.isoformat() if self.updated_at else None,
        }


class ScenarioRecord(Base):
    """Stores saved scenario configurations and templates."""

    __tablename__ = "scenarios"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()), index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("user.id", ondelete="CASCADE"), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    record_count: Mapped[int] = mapped_column(Integer, default=10000)
    secondary_metric: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    workflow: Mapped[Any] = mapped_column(JSON, nullable=False)
    rules: Mapped[Any] = mapped_column(JSON, nullable=False)
    configuration: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    def to_dict(self):
        configuration = self.configuration or {
            "title": self.title,
            "description": self.description,
            "recordCount": self.record_count,
            "secondaryMetric": self.secondary_metric or {"label": "", "value": ""},
            "workflow": self.workflow,
            "rules": self.rules,
        }
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "recordCount": self.record_count,
            "secondaryMetric": self.secondary_metric,
            "workflow": self.workflow,
            "rules": self.rules,
            "configuration": configuration,
            "createdAt": self.created_at.isoformat() if self.created_at else None,
            "updatedAt": self.updated_at.isoformat() if self.updated_at else None,
        }


class WorkspacePreference(Base):
    """Legacy global preferences retained only for one-time account migration."""

    __tablename__ = "workspace_preferences"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255), nullable=False)


class UserPreference(Base):
    """Nonsecret settings owned by one authenticated user."""

    __tablename__ = "user_preferences"

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("user.id", ondelete="CASCADE"), primary_key=True)
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255), nullable=False)


class UserProviderCredential(Base):
    """Encrypted provider credential owned by one authenticated user."""

    __tablename__ = "user_provider_credentials"

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("user.id", ondelete="CASCADE"), primary_key=True)
    provider: Mapped[str] = mapped_column(String(40), primary_key=True)
    encrypted_key: Mapped[str] = mapped_column(Text, nullable=False)


class ChatConversation(Base):
    """A user's saved AI-assisted scenario design conversation."""

    __tablename__ = "chat_conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(160), nullable=False, default="New conversation")
    messages: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    runs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    scenario_draft: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    draft_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, index=True)

    def to_dict(self, include_content: bool = True):
        payload: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "createdAt": self.created_at.isoformat() if self.created_at else None,
            "updatedAt": self.updated_at.isoformat() if self.updated_at else None,
            "messageCount": len(self.messages) if isinstance(self.messages, (list, tuple)) else 0,
        }
        if include_content:
            payload["messages"] = self.messages or []
            payload["runs"] = self.runs or []
            payload["scenarioDraft"] = self.scenario_draft
            payload["draftVersion"] = self.draft_version or 0
        return payload
