"""Database-native queue worker process.

Claims queued jobs directly from PostgreSQL/SQLite, executes them with
attempt fencing and heartbeat leases, and automatically reconciles abandoned leases.
Replaces Celery, Redis, and external dispatcher processes.
"""
import logging
import os
import signal
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Event
from . import database
from .job_queue import (
    POLL_INTERVAL_SECONDS,
    claim_next_job,
    reconcile_jobs,
)
from .job_runner import execute_job

logger = logging.getLogger("syda.worker")
CONCURRENCY = max(1, int(os.getenv("GENERATION_CONCURRENCY", "1")))
RECONCILE_INTERVAL = max(5, int(os.getenv("GENERATION_RECONCILE_INTERVAL", "15")))


class GenerationWorker:
    def __init__(self, concurrency=CONCURRENCY, poll_interval=POLL_INTERVAL_SECONDS):
        self.concurrency = concurrency
        self.poll_interval = poll_interval
        self.stop_event = Event()
        self.last_reconcile = 0.0

    def stop(self, *args):
        logger.info("Worker received stop signal, shutting down...")
        self.stop_event.set()

    def run_single_pass(self):
        """Claims and executes one job if available. Returns True if a job was processed."""
        now_time = time.monotonic()
        if now_time - self.last_reconcile >= RECONCILE_INTERVAL:
            try:
                reconcile_jobs()
            except Exception as error:
                logger.warning("Reconciliation pass failed: %s", error)
            self.last_reconcile = now_time

        claimed = claim_next_job()
        if claimed is None:
            return False

        job_id = claimed[0]
        logger.info("Claimed generation job %s", job_id)
        try:
            execute_job(job_id, claimed=claimed)
        except Exception as error:
            logger.exception("Error executing job %s: %s", job_id, error)
        return True

    def run(self):
        logger.info("Starting Syda generation worker (concurrency=%d)", self.concurrency)
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, self.stop)
            except (ValueError, AttributeError):
                # Signals might not be supported in some thread/subagent contexts
                pass

        if self.concurrency == 1:
            while not self.stop_event.is_set():
                had_work = self.run_single_pass()
                if not had_work:
                    self.stop_event.wait(self.poll_interval)
        else:
            with ThreadPoolExecutor(max_workers=self.concurrency) as executor:
                futures = set()
                while not self.stop_event.is_set():
                    futures = {f for f in futures if not f.done()}

                    now_time = time.monotonic()
                    if now_time - self.last_reconcile >= RECONCILE_INTERVAL:
                        try:
                            reconcile_jobs()
                        except Exception as error:
                            logger.warning("Reconciliation pass failed: %s", error)
                        self.last_reconcile = now_time

                    if len(futures) < self.concurrency:
                        claimed = claim_next_job()
                        if claimed is not None:
                            job_id = claimed[0]
                            logger.info("Claimed generation job %s", job_id)
                            fut = executor.submit(execute_job, job_id, claimed)
                            futures.add(fut)
                            continue

                    self.stop_event.wait(self.poll_interval)

                if futures:
                    logger.info("Waiting for %d running jobs to complete...", len(futures))
                    for f in as_completed(futures, timeout=30):
                        pass

        logger.info("Worker stopped cleanly.")


def main():
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    database.init_db()
    worker = GenerationWorker()
    worker.run()


if __name__ == "__main__":
    main()
