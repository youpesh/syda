import uuid
from datetime import datetime
from sqlalchemy import Column, String, Integer, Text, JSON, DateTime, ForeignKey, Uuid
from app.database import Base


class JobRecord(Base):
    """Stores synthetic data generation job executions and evaluation states."""

    __tablename__ = "generation_jobs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()), index=True)
    user_id = Column(Uuid(as_uuid=True), ForeignKey("user.id", ondelete="CASCADE"), nullable=True, index=True)
    status = Column(String(32), default="generating", index=True)
    scenario_id = Column(String(36), nullable=True, index=True)
    progress = Column(Integer, default=0)
    current_stage = Column(String(255), default="Initializing")
    scenario = Column(JSON, nullable=False)
    stats = Column(JSON, nullable=True)
    download_url = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

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

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()), index=True)
    user_id = Column(Uuid(as_uuid=True), ForeignKey("user.id", ondelete="CASCADE"), nullable=True, index=True)
    title = Column(String(255), nullable=False, index=True)
    description = Column(Text, nullable=False)
    record_count = Column(Integer, default=10000)
    secondary_metric = Column(JSON, nullable=True)
    workflow = Column(JSON, nullable=False)
    rules = Column(JSON, nullable=False)
    configuration = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

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

    key = Column(String(64), primary_key=True)
    value = Column(String(255), nullable=False)


class UserPreference(Base):
    """Nonsecret settings owned by one authenticated user."""

    __tablename__ = "user_preferences"

    user_id = Column(Uuid(as_uuid=True), ForeignKey("user.id", ondelete="CASCADE"), primary_key=True)
    key = Column(String(64), primary_key=True)
    value = Column(String(255), nullable=False)


class UserProviderCredential(Base):
    """Encrypted provider credential owned by one authenticated user."""

    __tablename__ = "user_provider_credentials"

    user_id = Column(Uuid(as_uuid=True), ForeignKey("user.id", ondelete="CASCADE"), primary_key=True)
    provider = Column(String(40), primary_key=True)
    encrypted_key = Column(Text, nullable=False)


class ChatConversation(Base):
    """A user's saved AI-assisted scenario design conversation."""

    __tablename__ = "chat_conversations"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()), index=True)
    user_id = Column(Uuid(as_uuid=True), ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String(160), nullable=False, default="New conversation")
    messages = Column(JSON, nullable=False, default=list)
    runs = Column(JSON, nullable=False, default=list)
    scenario_draft = Column(JSON, nullable=True)
    draft_version = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, index=True)

    def to_dict(self, include_content: bool = True):
        payload = {
            "id": self.id,
            "title": self.title,
            "createdAt": self.created_at.isoformat() if self.created_at else None,
            "updatedAt": self.updated_at.isoformat() if self.updated_at else None,
            "messageCount": len(self.messages or []),
        }
        if include_content:
            payload["messages"] = self.messages or []
            payload["runs"] = self.runs or []
            payload["scenarioDraft"] = self.scenario_draft
            payload["draftVersion"] = self.draft_version or 0
        return payload
