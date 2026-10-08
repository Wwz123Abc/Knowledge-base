import threading
from concurrent.futures import ThreadPoolExecutor

from celery import Celery

from app.config import get_settings
from app.db import SessionLocal
from app.metrics import INGESTION_JOBS

settings = get_settings()
celery_app = Celery(
    "enterprise_rag",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)
celery_app.conf.update(
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    # Redis redelivers a task that hasn't been acknowledged within this window; with
    # task_acks_late a long OCR/embedding job (or connector sync) would otherwise start a
    # second time while the first is still running.
    broker_transport_options={"visibility_timeout": 6 * 3600},
)

executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="rag-ingestion")


def run_ingestion_job(job_id: str) -> str:
    from app.services import get_document_service

    with SessionLocal() as db:
        job = get_document_service().process_job(db, job_id)
        INGESTION_JOBS.labels(job.status).inc()
        status, attempts = job.status, job.attempts
    if status == "queued":
        # process_job puts a job back in the queue after a transient (network) failure.
        _schedule_retry(job_id, attempts)
    return status


def _schedule_retry(job_id: str, attempts: int) -> None:
    delay = 20 * max(attempts, 1)
    if settings.task_backend == "celery":
        process_ingestion.apply_async(args=[job_id], countdown=delay)
    else:
        timer = threading.Timer(delay, lambda: executor.submit(run_ingestion_job, job_id))
        timer.daemon = True
        timer.start()


@celery_app.task(name="app.tasks.process_ingestion")
def process_ingestion(job_id: str) -> str:
    return run_ingestion_job(job_id)


def enqueue_ingestion(job_id: str) -> None:
    if settings.task_backend == "celery":
        process_ingestion.delay(job_id)
    else:
        executor.submit(run_ingestion_job, job_id)


def run_connector_sync(run_id: str) -> str:
    from app.connectors import ConnectorService

    with SessionLocal() as db:
        run = ConnectorService().execute_sync(db, run_id)
        return run.status


@celery_app.task(name="app.tasks.connector_sync")
def connector_sync(run_id: str) -> str:
    return run_connector_sync(run_id)


def enqueue_connector_sync(run_id: str) -> None:
    if settings.task_backend == "celery":
        connector_sync.delay(run_id)
    else:
        executor.submit(run_connector_sync, run_id)
