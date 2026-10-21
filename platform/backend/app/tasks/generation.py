"""Celery transport wrapper; execution and recovery are database-owned."""
from ..celery_app import celery_app
from ..job_runner import execute_job


@celery_app.task(name="syda.generate_dataset")
def generate_dataset(job_id):
    execute_job(job_id)
