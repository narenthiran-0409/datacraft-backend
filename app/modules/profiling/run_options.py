"""Redis-backed storage for per-run profiling options that have no column
on the frozen profile_runs table (currently just include_top_values).

Why Redis and not a Celery task kwarg: a task kwarg is reliably preserved
across normal execution, duplicate delivery, and Celery-level retries,
because Celery redelivers/retries the exact original message. But it is
NOT recoverable if run_profile is ever re-invoked by job_id/profile_run_id
alone — e.g. a manual recovery of a job stuck RUNNING after a worker died
before acking (task_acks_late is off by default in this project, so such a
message is simply lost, not redelivered). Storing the value in Redis, keyed
by profile_run_id, makes it recoverable for the lifetime of the run
regardless of how or how many times the task ends up being invoked, without
adding a column to the frozen profile_runs schema.

Mirrors the existing pattern in app.modules.jobs.service (the
job:{id}:cancel_requested key) — this project already stores transient,
per-run state in Redis rather than Postgres when a schema column isn't
available/appropriate.
"""
import uuid

from redis import Redis

_KEY_PREFIX = "profile_run:"
_KEY_SUFFIX = ":include_top_values"

# Safety-net TTL only — the key is explicitly cleared when the run reaches
# a terminal state. Generous enough to outlive any realistic run, short
# enough not to leak forever if cleanup is ever skipped.
_TTL_SECONDS = 60 * 60 * 24


def _key(profile_run_id: uuid.UUID) -> str:
    return f"{_KEY_PREFIX}{profile_run_id}{_KEY_SUFFIX}"


def set_include_top_values(redis_client: Redis, profile_run_id: uuid.UUID, value: bool) -> None:
    redis_client.set(_key(profile_run_id), "1" if value else "0", ex=_TTL_SECONDS)


def get_include_top_values(redis_client: Redis, profile_run_id: uuid.UUID) -> bool:
    """Defaults to False if the key is missing (e.g. it already expired,
    or was somehow never set) — the conservative choice, since omitting
    top-values is always a safe degrade, never a correctness problem
    (never leaks anything that wasn't requested)."""
    raw = redis_client.get(_key(profile_run_id))
    if raw is None:
        return False
    return raw in ("1", b"1")


def clear_include_top_values(redis_client: Redis, profile_run_id: uuid.UUID) -> None:
    redis_client.delete(_key(profile_run_id))
