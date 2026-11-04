"""Opt-in test using real Redis delivery and a real Celery worker."""
import os
import time
import uuid
import pytest

if os.getenv('SYDA_QUEUE_INTEGRATION') != '1':
    pytest.skip('Set SYDA_QUEUE_INTEGRATION=1 with Redis and PostgreSQL running', allow_module_level=True)

from celery.contrib.testing.worker import start_worker
from app import database, job_runner, job_queue
from app.celery_app import celery_app
from app.models.entities import JobRecord
from app.tasks.generation import generate_dataset
from test_generation_api import _scenario


def test_real_worker_completes_offline_job_once_and_publishes_files(monkeypatch, tmp_path):
    database.init_db()
    job_id = str(uuid.uuid4())
    monkeypatch.setattr(job_runner, 'DATA_DIR', tmp_path)
    with database.SessionLocal() as db:
        db.add(JobRecord(id=job_id, status='queued', scenario=_scenario(3), provider_snapshot={'id':'offline'}))
        db.commit()
    try:
        with start_worker(celery_app, perform_ping_check=False, pool='solo', concurrency=1, shutdown_timeout=20):
            assert job_queue.publish_job(job_id)
            generate_dataset.apply_async(args=[job_id])
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                with database.SessionLocal() as db:
                    job = db.get(JobRecord, job_id)
                    if job.status in ('complete','failed'):
                        assert job.status == 'complete', job.current_stage
                        assert job.attempt_count == 1
                        files=list(job_queue.output_directory(job, tmp_path).glob('*.csv'))
                        assert len(files)==2
                        assert job.stats['recordsGenerated']==6
                        break
                time.sleep(0.2)
            else:
                pytest.fail('Celery did not complete the queued job within 60 seconds')
    finally:
        with database.SessionLocal() as db:
            db.query(JobRecord).filter(JobRecord.id==job_id).delete()
            db.commit()
