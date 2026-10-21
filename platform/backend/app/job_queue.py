"""Database-owned dispatch, execution leases, and attempt-fenced updates."""
import logging
import os
import uuid
import httpx
from datetime import datetime, timedelta, timezone
from pathlib import Path
from sqlalchemy import or_
from . import database
from .models.entities import JobRecord

logger = logging.getLogger(__name__)
ACTIVE = ("generating", "validating", "evaluating")
MAX_ATTEMPTS = max(1, int(os.getenv("GENERATION_MAX_ATTEMPTS", "3")))
LEASE_SECONDS = max(30, int(os.getenv("GENERATION_LEASE_SECONDS", "120")))
DATA_DIR = Path(os.getenv("JOB_DATA_DIR", "data/jobs"))


def now():
    return datetime.now(timezone.utc)


class LostLease(Exception):
    """Another attempt owns the job, or it has already finished."""


def output_directory(job, root=None):
    directory = Path(root or DATA_DIR) / job.id
    return directory / "attempts" / job.output_attempt if job.output_attempt else directory


def publish_job(job_id, *, db=None):
    """Best effort; queued rows are durable dispatch intent, not lost on failure."""
    own_session = db is None
    db = db or database.SessionLocal()
    try:
        timestamp = now()
        reserved = db.query(JobRecord).filter(
            JobRecord.id == job_id, JobRecord.status == "queued",
            or_(JobRecord.next_dispatch_at.is_(None), JobRecord.next_dispatch_at <= timestamp),
        ).update({JobRecord.next_dispatch_at: timestamp + timedelta(seconds=30)}, synchronize_session=False)
        db.commit()
        if not reserved:
            return False
        try:
            from .tasks.generation import generate_dataset
            generate_dataset.apply_async(args=[job_id], task_id=str(uuid.uuid4()), retry=False)
            return True
        except Exception as error:
            # Never log broker URLs or provider credentials from exception strings.
            logger.warning("Dispatch deferred for job %s (%s)", job_id, type(error).__name__)
            return False
    finally:
        if own_session:
            db.close()


def claim_job(job_id):
    with database.SessionLocal() as db:
        token = str(uuid.uuid4())
        timestamp = now()
        claimed = db.query(JobRecord).filter(
            JobRecord.id == job_id, JobRecord.status == "queued",
            JobRecord.attempt_count < MAX_ATTEMPTS,
            or_(JobRecord.retry_at.is_(None), JobRecord.retry_at <= timestamp),
        ).update({
            JobRecord.status: "generating", JobRecord.attempt_token: token,
            JobRecord.attempt_count: JobRecord.attempt_count + 1,
            JobRecord.heartbeat_at: timestamp, JobRecord.updated_at: timestamp,
            JobRecord.progress: 0, JobRecord.current_stage: "Starting generation worker",
        }, synchronize_session=False)
        db.commit()
        if not claimed:
            return None
        job = db.get(JobRecord, job_id)
        return token, job.scenario, job.provider_snapshot, job.user_id


def update_attempt(job_id, token, updates):
    with database.SessionLocal() as db:
        values = dict(updates)
        if "current_stage" in values:
            values["current_stage"] = str(values["current_stage"])[:255]
        values["heartbeat_at"] = now()
        values["updated_at"] = now()
        if values.get("status") == "complete":
            values["output_attempt"] = token
        changed = db.query(JobRecord).filter(
            JobRecord.id == job_id, JobRecord.attempt_token == token,
            JobRecord.status.in_(ACTIVE),
        ).update(values, synchronize_session=False)
        db.commit()
        if not changed:
            raise LostLease(job_id)


def retryable(error):
    code = getattr(error, "status_code", None)
    return isinstance(error, (ConnectionError, TimeoutError, httpx.TimeoutException, httpx.TransportError)) or code == 429 or (
        isinstance(code, int) and 500 <= code < 600
    )


def fail_attempt(job_id, token, error):
    with database.SessionLocal() as db:
        job = db.get(JobRecord, job_id)
        if not job or job.attempt_token != token or job.status not in ACTIVE:
            return
        retry = retryable(error) and job.attempt_count < MAX_ATTEMPTS
        delay = min(60, 5 * 2 ** (job.attempt_count - 1))
        db.query(JobRecord).filter(
            JobRecord.id == job_id, JobRecord.attempt_token == token,
            JobRecord.status.in_(ACTIVE),
        ).update({
            JobRecord.status: "queued" if retry else "failed",
            JobRecord.current_stage: "Temporary provider failure; waiting to retry" if retry else f"Generation failed ({type(error).__name__})",
            JobRecord.retry_at: now() + timedelta(seconds=delay) if retry else None,
            JobRecord.next_dispatch_at: now() + timedelta(seconds=delay) if retry else None,
            JobRecord.attempt_token: None, JobRecord.download_url: None,
        }, synchronize_session=False)
        db.commit()



def reconcile_jobs():
    """Recover expired execution leases and publish queued jobs. Safe to repeat."""
    timestamp = now()
    with database.SessionLocal() as db:
        stale = db.query(JobRecord).filter(
            JobRecord.status.in_(ACTIVE),
            or_(JobRecord.heartbeat_at.is_(None), JobRecord.heartbeat_at < timestamp - timedelta(seconds=LEASE_SECONDS)),
        ).all()
        for job in stale:
            retry = bool(job.provider_snapshot) and job.attempt_count < MAX_ATTEMPTS
            # Conditional update: a worker heartbeat after the SELECT wins.
            db.query(JobRecord).filter(
                JobRecord.id == job.id, JobRecord.status.in_(ACTIVE),
                or_(JobRecord.heartbeat_at.is_(None), JobRecord.heartbeat_at < timestamp - timedelta(seconds=LEASE_SECONDS)),
            ).update({
                JobRecord.status: "queued" if retry else "failed",
                JobRecord.current_stage: "Worker interrupted; waiting to retry" if retry else "Generation interrupted; retry limit reached or legacy job",
                JobRecord.attempt_token: None, JobRecord.next_dispatch_at: timestamp,
                JobRecord.retry_at: timestamp, JobRecord.download_url: None,
            }, synchronize_session=False)
        db.commit()
        queued = db.query(JobRecord.id).filter(
            JobRecord.status == "queued",
            or_(JobRecord.next_dispatch_at.is_(None), JobRecord.next_dispatch_at <= timestamp),
            or_(JobRecord.retry_at.is_(None), JobRecord.retry_at <= timestamp),
        ).order_by(JobRecord.created_at).limit(100).all()
    for (job_id,) in queued:
        publish_job(job_id)
