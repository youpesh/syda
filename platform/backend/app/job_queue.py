"""Database-owned queue, execution leases, and attempt-fenced updates."""
import logging
import os
import uuid
import httpx
from datetime import datetime, timedelta, timezone
from pathlib import Path
from sqlalchemy import or_, text
from . import database
from .models.entities import JobRecord

logger = logging.getLogger(__name__)
ACTIVE = ("generating", "validating", "evaluating")
MAX_ATTEMPTS = max(1, int(os.getenv("GENERATION_MAX_ATTEMPTS", "3")))
LEASE_SECONDS = max(30, int(os.getenv("GENERATION_LEASE_SECONDS", "120")))
POLL_INTERVAL_SECONDS = max(0.5, float(os.getenv("GENERATION_POLL_INTERVAL_SECONDS", "2.0")))
DATA_DIR = Path(os.getenv("JOB_DATA_DIR", "data/jobs"))


def now():
    return datetime.now(timezone.utc)


class LostLease(Exception):
    """Another attempt owns the job, or it has already finished."""


def output_directory(job, root=None):
    directory = Path(root or DATA_DIR) / job.id
    return directory / "attempts" / job.output_attempt if job.output_attempt else directory


def claim_job(job_id):
    """Atomically claim a specific queued job by ID."""
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


def claim_next_job():
    """Atomically claim the next available queued job from the database.

    On PostgreSQL, uses `FOR UPDATE SKIP LOCKED` so concurrent workers claim distinct jobs
    with zero locks or deadlocks. On SQLite, uses atomic conditional updates with retry.
    Returns (job_id, token, scenario, provider_snapshot, user_id) or None.
    """
    with database.SessionLocal() as db:
        timestamp = now()
        token = str(uuid.uuid4())
        is_postgres = bool(db.bind and db.bind.dialect.name == "postgresql")

        for _ in range(5):
            query = db.query(JobRecord).filter(
                JobRecord.status == "queued",
                JobRecord.attempt_count < MAX_ATTEMPTS,
                or_(JobRecord.retry_at.is_(None), JobRecord.retry_at <= timestamp),
            ).order_by(JobRecord.created_at)

            if is_postgres:
                candidate = query.with_for_update(skip_locked=True).first()
            else:
                candidate = query.first()

            if not candidate:
                return None

            job_id = candidate.id
            claimed = db.query(JobRecord).filter(
                JobRecord.id == job_id, JobRecord.status == "queued",
                JobRecord.attempt_count == candidate.attempt_count,
            ).update({
                JobRecord.status: "generating", JobRecord.attempt_token: token,
                JobRecord.attempt_count: JobRecord.attempt_count + 1,
                JobRecord.heartbeat_at: timestamp, JobRecord.updated_at: timestamp,
                JobRecord.progress: 0, JobRecord.current_stage: "Starting generation worker",
            }, synchronize_session=False)
            db.commit()
            if claimed:
                job = db.get(JobRecord, job_id)
                return job.id, token, job.scenario, job.provider_snapshot, job.user_id

        return None


def cancel_job(job_id, user_id=None):
    """Cancel a queued or active generation job."""
    with database.SessionLocal() as db:
        query = db.query(JobRecord).filter(
            JobRecord.id == job_id,
            JobRecord.status.in_(("queued", *ACTIVE)),
        )
        if user_id is not None:
            query = query.filter(JobRecord.user_id == user_id)
        changed = query.update({
            JobRecord.status: "cancelled",
            JobRecord.current_stage: "Generation cancelled by user",
            JobRecord.updated_at: now(),
            JobRecord.attempt_token: None,
        }, synchronize_session=False)
        db.commit()
        return bool(changed)


def notify_job_queued(db=None):
    """Signal workers via PostgreSQL LISTEN/NOTIFY for zero-latency wakeup."""
    own_session = db is None
    db = db or database.SessionLocal()
    try:
        if db.bind and db.bind.dialect.name == "postgresql":
            db.execute(text("NOTIFY syda_generation_queue;"))
            db.commit()
    except Exception as error:
        logger.debug("Queue notify omitted or unsupported (%s)", error)
    finally:
        if own_session:
            db.close()


def publish_job(job_id, *, db=None):
    """Signal that a job is ready for execution.

    In the database-native queue, persisting the JobRecord with status='queued'
    is itself the durable dispatch. This notifies listening workers.
    """
    notify_job_queued(db=db)
    return True


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
    # Provider SDKs commonly wrap the underlying HTTP timeout/transport error.
    seen = set()
    for _ in range(5):
        if error is None or id(error) in seen:
            return False
        seen.add(id(error))
        code = getattr(error, "status_code", None)
        if isinstance(error, (ConnectionError, TimeoutError, httpx.TimeoutException, httpx.TransportError)) or code == 429 or (
            isinstance(code, int) and 500 <= code < 600
        ):
            return True
        error = error.__cause__
    return False


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
    """Recover expired execution leases for interrupted or crashed workers."""
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
                JobRecord.attempt_token: None,
                JobRecord.retry_at: timestamp,
                JobRecord.download_url: None,
            }, synchronize_session=False)
        db.commit()
