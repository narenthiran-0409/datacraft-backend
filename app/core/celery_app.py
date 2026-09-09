from celery import Celery

from app.core.config import settings

celery_app = Celery(
    settings.APP_NAME,
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
)


@celery_app.task(name="app.core.celery_app.health_check_task")
def health_check_task() -> str:
    """Trivial task proving the worker boots and can execute work."""
    return "pong"


# Imported after celery_app is defined (these modules do `from
# app.core.celery_app import celery_app` themselves) so their @celery_app.task
# decorators register against this instance without a circular import.
from app.modules.ai import tasks as _ai_tasks  # noqa: E402,F401
from app.modules.discovery import tasks as _discovery_tasks  # noqa: E402,F401
from app.modules.jobs import tasks as _jobs_tasks  # noqa: E402,F401
from app.modules.profiling import tasks as _profiling_tasks  # noqa: E402,F401
from app.modules.publishing import tasks as _publishing_tasks  # noqa: E402,F401
from app.modules.validation import tasks as _validation_tasks  # noqa: E402,F401

celery_app.conf.beat_schedule = {
    "sweep-stale-jobs": {
        "task": "jobs.sweep_stale_jobs",
        "schedule": 300.0,
    },
}
