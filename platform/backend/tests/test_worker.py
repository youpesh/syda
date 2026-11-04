"""Test suite for the database-native GenerationWorker."""
import time
import uuid
import pytest
from types import SimpleNamespace
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app import database, job_queue, job_runner
from app.models.entities import JobRecord
from app.worker import GenerationWorker


@pytest.fixture
def worker_db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'worker_jobs.db'}", connect_args={"check_same_thread": False})
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(job_runner, "DATA_DIR", tmp_path / 'outputs')
    yield factory
    engine.dispose()


def add_job(factory, **values):
    with factory() as db:
        job = JobRecord(
            id=str(uuid.uuid4()),
            status="queued",
            scenario={},
            provider_snapshot={"id": "offline"},
            **values,
        )
        db.add(job)
        db.commit()
        return job.id


def test_worker_run_single_pass_processes_job(worker_db, monkeypatch):
    job_id = add_job(worker_db)
    executed = []

    def mock_run_generation(jid, *args, **kwargs):
        executed.append(jid)
        kwargs['update_job'](jid, {'status': 'complete', 'progress': 100})

    monkeypatch.setattr(job_runner, 'run_generation', mock_run_generation)
    monkeypatch.setattr(job_runner.ScenarioConfiguration, 'model_validate', lambda v: v)

    worker = GenerationWorker(concurrency=1)
    had_work = worker.run_single_pass()

    assert had_work is True
    assert executed == [job_id]

    with worker_db() as db:
        job = db.get(JobRecord, job_id)
        assert job.status == 'complete'
        assert job.progress == 100


def test_worker_run_single_pass_returns_false_when_idle(worker_db):
    worker = GenerationWorker(concurrency=1)
    assert worker.run_single_pass() is False


def test_worker_stop_signal_shuts_down_cleanly():
    worker = GenerationWorker(concurrency=1, poll_interval=0.1)
    assert not worker.stop_event.is_set()
    worker.stop()
    assert worker.stop_event.is_set()
