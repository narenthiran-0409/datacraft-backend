import uuid

from app.core.celery_app import celery_app
from app.core.database import SessionLocal
from app.core.redis_client import get_redis_client
from app.db.models import User
from app.modules.ai.suggestion_service import AISuggestionService
from app.modules.jobs.service import JobsService

# job_type='AI_SUGGESTION' for all four tasks below — the existing
# idempotency-check-at-task-start pattern (re-fetch job status, exit
# cleanly if already terminal) mirrors run_publish/run_validation/
# run_profile exactly. No linked "run" table entry is registered in
# app.modules.jobs.tasks._RUN_TABLE_BY_JOB_TYPE for AI_SUGGESTION:
# unlike PROFILE_RUN/VALIDATION_RUN/PUBLISH, an ai_suggestions row is
# only ever created upon SUCCESSFUL completion (there is no in-flight
# status in that table's own vocabulary — PROPOSED/ACCEPTED/REJECTED/
# EXPIRED all describe a completed, reviewable proposal, never a
# "generating" state) — so there is never a dangling placeholder row for
# the stale-job sweep to need to flip; jobs.status alone remains
# authoritative for AI_SUGGESTION jobs, exactly as it already is.


def _run_ai_job(job_id: str, work) -> dict:
    db = SessionLocal()
    try:
        redis_client = get_redis_client()
        jobs_service = JobsService(db, redis_client)
        job = jobs_service.get(uuid.UUID(job_id))
        if job.status in ("CANCELLED", "COMPLETED", "FAILED"):
            return {"status": job.status}

        actor = db.get(User, job.created_by) if job.created_by else None
        jobs_service.mark_running(job.id)

        try:
            result = work(db, actor)
        except Exception as exc:  # noqa: BLE001 — task boundary: never let an unhandled exception escape uncaptured
            jobs_service.mark_failed(job.id, f"{type(exc).__name__}: {exc}")
            db.commit()
            return {"status": "FAILED", "error": str(exc)}

        jobs_service.mark_completed(job.id)
        db.commit()
        return {"status": "COMPLETED", **result}
    finally:
        db.close()


@celery_app.task(name="ai.run_run_summary")
def run_ai_run_summary(job_id: str, validation_run_id: str) -> dict:
    def work(db, actor):
        suggestion = AISuggestionService(db).generate_run_summary(uuid.UUID(validation_run_id), actor)
        return {"ai_suggestion_id": str(suggestion.id)}

    return _run_ai_job(job_id, work)


@celery_app.task(name="ai.run_prioritization")
def run_ai_prioritization(job_id: str, review_run_id: str) -> dict:
    def work(db, actor):
        suggestion = AISuggestionService(db).generate_prioritization(uuid.UUID(review_run_id), actor)
        return {"ai_suggestion_id": str(suggestion.id)}

    return _run_ai_job(job_id, work)


@celery_app.task(name="ai.run_cluster")
def run_ai_cluster(job_id: str, review_run_id: str) -> dict:
    def work(db, actor):
        suggestion = AISuggestionService(db).generate_cluster(uuid.UUID(review_run_id), actor)
        return {"ai_suggestion_id": str(suggestion.id)}

    return _run_ai_job(job_id, work)


@celery_app.task(name="ai.run_corrections")
def run_ai_corrections(job_id: str, review_run_id: str) -> dict:
    def work(db, actor):
        suggestions = AISuggestionService(db).generate_corrections(uuid.UUID(review_run_id), actor)
        return {"ai_suggestion_ids": [str(s.id) for s in suggestions], "count": len(suggestions)}

    return _run_ai_job(job_id, work)
