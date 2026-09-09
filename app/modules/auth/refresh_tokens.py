import uuid
from datetime import datetime, timedelta, timezone

from redis import Redis

from app.core.config import settings
from app.core.security import create_token

_TOKEN_KEY_PREFIX = "refresh_token:"
_USER_TOKENS_KEY_PREFIX = "user_refresh_tokens:"


class RefreshTokenStore:
    """Redis-backed refresh-token state. Rotate-on-use: a valid refresh
    consumes its jti and issues a fresh access+refresh pair; an unknown or
    already-used jti is rejected."""

    def __init__(self, redis_client: Redis) -> None:
        self._redis = redis_client
        self._ttl = timedelta(minutes=settings.REFRESH_TOKEN_EXPIRE_MINUTES)

    def issue(self, user_id: uuid.UUID) -> str:
        token, jti = create_token(user_id, "refresh", self._ttl)
        expires_at = (datetime.now(timezone.utc) + self._ttl).isoformat()

        pipe = self._redis.pipeline()
        pipe.hset(f"{_TOKEN_KEY_PREFIX}{jti}", mapping={"user_id": str(user_id), "expires_at": expires_at})
        pipe.expire(f"{_TOKEN_KEY_PREFIX}{jti}", self._ttl)
        pipe.sadd(f"{_USER_TOKENS_KEY_PREFIX}{user_id}", jti)
        pipe.expire(f"{_USER_TOKENS_KEY_PREFIX}{user_id}", self._ttl)
        pipe.execute()
        return token

    def consume(self, jti: str) -> uuid.UUID | None:
        """Atomically checks and deletes the jti. Returns the owning user_id,
        or None if the jti is unknown/already used/expired."""
        key = f"{_TOKEN_KEY_PREFIX}{jti}"
        user_id_raw = self._redis.hget(key, "user_id")
        if user_id_raw is None:
            return None

        deleted = self._redis.delete(key)
        if deleted == 0:
            return None

        user_id = uuid.UUID(user_id_raw if isinstance(user_id_raw, str) else user_id_raw.decode())
        self._redis.srem(f"{_USER_TOKENS_KEY_PREFIX}{user_id}", jti)
        return user_id

    def revoke(self, jti: str, user_id: uuid.UUID) -> None:
        self._redis.delete(f"{_TOKEN_KEY_PREFIX}{jti}")
        self._redis.srem(f"{_USER_TOKENS_KEY_PREFIX}{user_id}", jti)

    def revoke_all_for_user(self, user_id: uuid.UUID) -> None:
        set_key = f"{_USER_TOKENS_KEY_PREFIX}{user_id}"
        jtis = self._redis.smembers(set_key)
        if jtis:
            keys = [f"{_TOKEN_KEY_PREFIX}{jti if isinstance(jti, str) else jti.decode()}" for jti in jtis]
            self._redis.delete(*keys)
        self._redis.delete(set_key)
