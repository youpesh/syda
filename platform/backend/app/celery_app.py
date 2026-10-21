"""A single long-running generation queue; job results live in PostgreSQL."""
import os
from celery import Celery
from celery.signals import worker_process_init

TIME_LIMIT = max(60, int(os.getenv("GENERATION_TIME_LIMIT", "3600")))
celery_app = Celery("syda", broker=os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0"), include=["app.tasks.generation"])
celery_app.conf.update(
    task_default_queue="generation", task_serializer="json", accept_content=["json"],
    task_ignore_result=True, worker_prefetch_multiplier=1,
    task_acks_late=True, task_reject_on_worker_lost=True,
    task_soft_time_limit=TIME_LIMIT - 10, task_time_limit=TIME_LIMIT,
    broker_connection_timeout=2, broker_connection_retry_on_startup=True,
    broker_transport_options={"visibility_timeout": TIME_LIMIT + 60, "socket_connect_timeout": 2, "socket_timeout": 2},
)


@worker_process_init.connect
def reset_database_pool(**kwargs):
    from .database import engine
    engine.dispose(close=False)
