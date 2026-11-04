"""Execution and recovery contracts for the database-native queue."""
import uuid
from datetime import timedelta
from types import SimpleNamespace
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app import database, job_queue, job_runner
from app.models.entities import JobRecord
from app.models.user import User


@pytest.fixture
def queue_db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'jobs.db'}", connect_args={"check_same_thread": False})
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(job_runner, "DATA_DIR", tmp_path / 'outputs')
    yield factory
    engine.dispose()


def add_job(factory, **values):
    with factory() as db:
        job = JobRecord(id=str(uuid.uuid4()), status="queued", scenario={}, provider_snapshot={"id": "offline"}, **values)
        db.add(job)
        db.commit()
        return job.id


def test_duplicate_delivery_only_claims_once(queue_db):
    job_id = add_job(queue_db)
    claimed = job_queue.claim_job(job_id)
    assert claimed
    assert job_queue.claim_job(job_id) is None
    with queue_db() as db:
        assert db.get(JobRecord, job_id).attempt_count == 1


def test_stale_attempt_cannot_publish_or_fail_new_attempt(queue_db, monkeypatch):
    job_id = add_job(queue_db)
    old = job_queue.claim_job(job_id)[0]
    with queue_db() as db:
        job = db.get(JobRecord, job_id)
        job.heartbeat_at = job_queue.now() - timedelta(seconds=job_queue.LEASE_SECONDS + 1)
        db.commit()
    job_queue.reconcile_jobs()
    new = job_queue.claim_job(job_id)[0]
    assert old != new
    with pytest.raises(job_queue.LostLease):
        job_queue.update_attempt(job_id, old, {'status': 'complete'})
    job_queue.fail_attempt(job_id, old, ValueError('old failure'))
    with queue_db() as db:
        assert db.get(JobRecord, job_id).attempt_token == new
        assert db.get(JobRecord, job_id).status == 'generating'


def test_claim_next_job_fifo_order_and_transitions(queue_db):
    id1 = add_job(queue_db)
    id2 = add_job(queue_db)

    first = job_queue.claim_next_job()
    assert first is not None
    assert first[0] == id1

    second = job_queue.claim_next_job()
    assert second is not None
    assert second[0] == id2

    assert job_queue.claim_next_job() is None


def test_claim_next_job_respects_retry_backoff(queue_db):
    job_id = add_job(queue_db)
    token = job_queue.claim_job(job_id)[0]
    # Fail with retryable error (sets retry_at into the future)
    job_queue.fail_attempt(job_id, token, TimeoutError("temporary"))

    with queue_db() as db:
        job = db.get(JobRecord, job_id)
        assert job.status == "queued"
        retry_at = job.retry_at.replace(tzinfo=job_queue.timezone.utc) if job.retry_at.tzinfo is None else job.retry_at
        assert retry_at > job_queue.now()

    # Next job claim should skip this job until retry_at passes
    assert job_queue.claim_next_job() is None

    # Fast-forward retry_at
    with queue_db() as db:
        job = db.get(JobRecord, job_id)
        job.retry_at = job_queue.now() - timedelta(seconds=1)
        db.commit()

    reclaimed = job_queue.claim_next_job()
    assert reclaimed is not None
    assert reclaimed[0] == job_id


def test_concurrent_claim_next_job_claims_distinct_rows(queue_db):
    from concurrent.futures import ThreadPoolExecutor
    id1 = add_job(queue_db)
    id2 = add_job(queue_db)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: job_queue.claim_next_job(), [1, 2]))

    claimed_ids = {r[0] for r in results if r is not None}
    assert claimed_ids == {id1, id2}
    assert len(claimed_ids) == 2


def test_temporary_failure_is_bounded_and_respects_backoff(queue_db):
    job_id = add_job(queue_db)
    for attempt in range(job_queue.MAX_ATTEMPTS):
        claimed = job_queue.claim_job(job_id)
        assert claimed
        job_queue.fail_attempt(job_id, claimed[0], TimeoutError('temporary'))
        with queue_db() as db:
            job = db.get(JobRecord, job_id)
            if attempt + 1 < job_queue.MAX_ATTEMPTS:
                assert job.status == 'queued'
                assert job_queue.claim_job(job_id) is None
                job.retry_at = job_queue.now() - timedelta(seconds=1)
                db.commit()
            else:
                assert job.status == 'failed'
    assert job_queue.claim_job(job_id) is None


def test_invalid_input_fails_without_retry_or_sensitive_error(queue_db):
    job_id = add_job(queue_db)
    token = job_queue.claim_job(job_id)[0]
    job_queue.fail_attempt(job_id, token, ValueError('secret api credential'))
    with queue_db() as db:
        job = db.get(JobRecord, job_id)
        assert job.status == 'failed'
        assert 'secret' not in job.current_stage


def test_completed_attempt_selects_output_and_ignores_redelivery(queue_db, tmp_path):
    job_id = add_job(queue_db)
    token = job_queue.claim_job(job_id)[0]
    job_queue.update_attempt(job_id, token, {'status':'complete', 'progress':100})
    with queue_db() as db:
        job = db.get(JobRecord, job_id)
        assert job_queue.output_directory(job, tmp_path) == tmp_path / job_id / 'attempts' / token
    assert job_queue.claim_job(job_id) is None


def test_legacy_download_paths_are_preserved(queue_db, tmp_path):
    job_id = add_job(queue_db)
    with queue_db() as db:
        assert job_queue.output_directory(db.get(JobRecord, job_id), tmp_path) == tmp_path / job_id


def test_api_initialization_does_not_fail_live_worker(queue_db):
    job_id = add_job(queue_db)
    token = job_queue.claim_job(job_id)[0]
    database.init_db()
    with queue_db() as db:
        job = db.get(JobRecord, job_id)
        assert job.status == 'generating'
        assert job.attempt_token == token


def test_live_heartbeats_are_not_recovered(queue_db, monkeypatch):
    job_id = add_job(queue_db)
    job_queue.claim_job(job_id)
    job_queue.reconcile_jobs()
    with queue_db() as db:
        assert db.get(JobRecord, job_id).status == 'generating'


def test_reconcile_recovers_interrupted_worker_to_queued(queue_db):
    job_id = add_job(queue_db)
    token = job_queue.claim_job(job_id)[0]
    with queue_db() as db:
        job = db.get(JobRecord, job_id)
        job.heartbeat_at = job_queue.now() - timedelta(seconds=job_queue.LEASE_SECONDS + 10)
        db.commit()

    job_queue.reconcile_jobs()
    with queue_db() as db:
        job = db.get(JobRecord, job_id)
        assert job.status == "queued"
        assert job.attempt_token is None

    # Job is now eligible to be reclaimed
    reclaimed = job_queue.claim_next_job()
    assert reclaimed is not None
    assert reclaimed[0] == job_id


def test_worker_uses_saved_provider_choice_and_owner_key(queue_db, monkeypatch):
    job_id = add_job(queue_db, user_id=uuid.uuid4())
    with queue_db() as db:
        job=db.get(JobRecord,job_id)
        job.provider_snapshot={'id':'openai','model':'saved-model','base_url':None}
        owner=job.user_id
        db.commit()
    calls=[]
    monkeypatch.setattr(job_runner, 'provider_key', lambda provider,user_id,db: calls.append((provider,user_id)) or 'secret')
    monkeypatch.setattr(job_runner.ScenarioConfiguration, 'model_validate', lambda value: value)
    def run(job_id,scenario,provider,**kwargs):
        assert provider['model']=='saved-model'
        assert provider['key']=='secret'
        kwargs['update_job'](job_id,{'status':'complete'})
    monkeypatch.setattr(job_runner, 'run_generation', run)
    job_runner.execute_job(job_id)
    assert calls==[('openai',owner)]
    with queue_db() as db:
        assert db.get(JobRecord,job_id).status=='complete'


def test_missing_saved_key_fails_instead_of_changing_provider(queue_db, monkeypatch):
    job_id=add_job(queue_db)
    with queue_db() as db:
        db.get(JobRecord,job_id).provider_snapshot={'id':'openai','model':'saved-model'}
        db.commit()
    monkeypatch.setattr(job_runner,'provider_key',lambda *args:None)
    monkeypatch.setattr(job_runner,'run_generation',lambda *args,**kw:pytest.fail('generation should not run'))
    job_runner.execute_job(job_id)
    with queue_db() as db:
        assert db.get(JobRecord,job_id).status=='failed'


def test_provider_timeout_wrapped_by_sdk_is_retryable():
    error=RuntimeError('provider wrapper')
    error.__cause__=TimeoutError('network timeout')
    assert job_queue.retryable(error)
    assert not job_queue.retryable(SimpleNamespace(status_code=401, __cause__=None))


def test_concurrent_deliveries_only_claim_one_attempt(queue_db):
    from concurrent.futures import ThreadPoolExecutor
    job_id=add_job(queue_db)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims=list(pool.map(job_queue.claim_job,[job_id,job_id]))
    assert sum(value is not None for value in claims)==1
