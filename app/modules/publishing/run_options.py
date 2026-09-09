"""Redis-backed storage for per-run publishing options that have no column
on the frozen publish_runs table (currently just overwrite). Mirrors
app.modules.profiling.run_options exactly, for the identical reason: a
Celery task kwarg is not reliably recoverable if run_publish is ever
re-invoked by job_id/publish_run_id alone — which is exactly what happens
here by design, since drift-acknowledge re-enqueues the SAME task a second
time after the first dequeue exited early. Storing the value in Redis,
keyed by publish_run_id, makes it recoverable across both deliveries.
"""
import uuid

from redis import Redis

_KEY_PREFIX = "publish_run:"
_KEY_SUFFIX = ":overwrite"

# Safety-net TTL only — the key is explicitly cleared when the run reaches
# a terminal state (or stays queued pending drift-acknowledge; either way,
# this is generous enough to outlive any realistic run).
_TTL_SECONDS = 60 * 60 * 24


def _key(publish_run_id: uuid.UUID) -> str:
    return f"{_KEY_PREFIX}{publish_run_id}{_KEY_SUFFIX}"


def set_overwrite(redis_client: Redis, publish_run_id: uuid.UUID, value: bool) -> None:
    redis_client.set(_key(publish_run_id), "1" if value else "0", ex=_TTL_SECONDS)


def get_overwrite(redis_client: Redis, publish_run_id: uuid.UUID) -> bool:
    """Defaults to False if the key is missing — the conservative choice,
    since omitting overwrite is always the safe degrade (never silently
    clobbers an existing file)."""
    raw = redis_client.get(_key(publish_run_id))
    if raw is None:
        return False
    return raw in ("1", b"1")


def clear_overwrite(redis_client: Redis, publish_run_id: uuid.UUID) -> None:
    redis_client.delete(_key(publish_run_id))
