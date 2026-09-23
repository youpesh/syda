import uuid
from datetime import datetime
from sqlalchemy import Column, String, Integer, Text, JSON, DateTime
from app.database import Base


class JobRecord(Base):
    """Stores synthetic data generation job executions and evaluation states."""

    __tablename__ = "generation_jobs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4())[:8], index=True)
    status = Column(String(32), default="generating", index=True)
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

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4())[:8], index=True)
    title = Column(String(255), nullable=False, index=True)
    description = Column(Text, nullable=False)
    record_count = Column(Integer, default=10000)
    secondary_metric = Column(JSON, nullable=True)
    workflow = Column(JSON, nullable=False)
    rules = Column(JSON, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "recordCount": self.record_count,
            "secondaryMetric": self.secondary_metric,
            "workflow": self.workflow,
            "rules": self.rules,
            "createdAt": self.created_at.isoformat() if self.created_at else None,
            "updatedAt": self.updated_at.isoformat() if self.updated_at else None,
        }
