"""One task per job. All state changes are fenced by the execution attempt."""
import logging
from threading import Event, Thread
from . import database
from .job_queue import DATA_DIR, LEASE_SECONDS, LostLease, claim_job, update_attempt, fail_attempt
from .models.scenario import ScenarioConfiguration
from .provider_settings import provider_key
from .services.generation import run_generation

logger = logging.getLogger(__name__)


def execute_job(job_id):
    claimed = claim_job(job_id)
    if claimed is None:
        return
    token, scenario, snapshot, user_id = claimed
    stop = Event()

    def heartbeat():
        while not stop.wait(LEASE_SECONDS / 3):
            try:
                update_attempt(job_id, token, {})
            except LostLease:
                return
            except Exception:
                logger.warning("Could not refresh generation lease for %s", job_id)

    thread = Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        if not snapshot:
            raise ValueError("Job has no provider snapshot")
        provider = dict(snapshot)
        if provider["id"] != "offline":
            with database.SessionLocal() as db:
                key = provider_key(provider["id"], user_id, db)
            if not key:
                raise ValueError("Saved provider credential is unavailable")
            provider["key"] = key
        run_generation(
            job_id, ScenarioConfiguration.model_validate(scenario), provider,
            output_dir=DATA_DIR / job_id / "attempts" / token,
            update_job=lambda _, values: update_attempt(job_id, token, values),
        )
    except LostLease:
        logger.info("Generation attempt no longer owns job %s", job_id)
    except Exception as error:
        fail_attempt(job_id, token, error)
        logger.warning("Generation attempt failed for %s (%s)", job_id, type(error).__name__)
    finally:
        stop.set()
        thread.join(timeout=5)
