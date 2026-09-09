from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    APP_NAME: str = "dataquality-platform"
    ENVIRONMENT: str = "local"
    LOG_LEVEL: str = "INFO"
    API_V1_PREFIX: str = "/api/v1"

    # Comma-separated list of origins allowed to call this API from a
    # browser. Defaults to the datacraft frontend's actual dev-server
    # origin (vite --port=3000, per its package.json "dev" script) plus
    # Vite's own default port as a fallback for a differently-configured
    # instance. Never defaults to "*" — that's broader than this API
    # actually needs.
    CORS_ALLOWED_ORIGINS: str = "http://localhost:3000,http://localhost:5173"

    @property
    def cors_allowed_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.CORS_ALLOWED_ORIGINS.split(",") if origin.strip()]

    DATABASE_URL: str = "postgresql+psycopg://dq_user:dq_password@localhost:5432/dataquality"
    REDIS_URL: str = "redis://localhost:6379/0"

    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/2"

    JWT_SECRET_KEY: str = "local-dev-secret-change-me-32-bytes-minimum"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7

    VAULT_LOCAL_ENCRYPTION_KEY: str = "auxfxLith1IRMEbLccKJOaae4X4dQf7IITmLV3NpVS0="

    DISCOVERY_QUERY_TIMEOUT_SECONDS: int = 30
    STALE_JOB_THRESHOLD_MINUTES: int = 60

    PROFILING_QUERY_TIMEOUT_SECONDS: int = 30
    # A full-table COUNT(DISTINCT ...) pushdown is inherently heavier than a
    # single discovery catalog lookup, so it gets its own, larger budget
    # rather than sharing PROFILING_QUERY_TIMEOUT_SECONDS.
    PROFILING_EXACT_STATS_TIMEOUT_SECONDS: int = 60
    PROFILING_EXACT_STATS_MAX_COLUMNS_PER_QUERY: int = 25
    PROFILING_DATASET_TIMEOUT_SECONDS: int = 300
    PROFILING_DEFAULT_SAMPLE_SIZE: int = 30000
    PROFILING_MAX_SAMPLE_SIZE: int = 200000
    PROFILING_MAX_FULL_SCAN_ROWS: int = 5000000

    # Caps persisted validation_failures rows per run — never stops
    # evaluation or alters aggregate counts/quality_score. See
    # app.modules.validation.engine for exact enforcement.
    VALIDATION_MAX_FAILURES: int = 10000
    # Bounds in-process full-dataset evaluation (CROSS_COLUMN, and DUPLICATE
    # when no push-down aggregate is available) — mirrors
    # PROFILING_DATASET_TIMEOUT_SECONDS' role for a similarly expensive
    # full-dataset operation.
    VALIDATION_DATASET_TIMEOUT_SECONDS: int = 300

    # Staging is fully synchronous (no Celery task) — this ceiling is what
    # keeps a single HTTP request bounded. Exceeding it is rejected with 422
    # before any staging_runs row is created, never silently truncated.
    MAX_SYNCHRONOUS_STAGING_RECORDS: int = 50000
    # Bounded timeout for the batched fetch_rows_by_keys() call. Gets its
    # own setting rather than reusing PROFILING_QUERY_TIMEOUT_SECONDS, which
    # is documented as being for ordinary profiling calls specifically.
    STAGING_QUERY_TIMEOUT_SECONDS: int = 30

    # The one directory FILE_EXPORT publishing is permitted to write into.
    # target_reference is resolved relative to this root and validated to
    # reject any path that escapes it (path traversal protection).
    PUBLISH_FILE_EXPORT_DIRECTORY: str = "./data/publish_exports"

    # AI Orchestrator (Phase 12). AI_ENABLED defaults false — the safe
    # default so no deployment ever accidentally activates live LLM calls.
    # ANTHROPIC_API_KEY comes from environment/secret configuration only —
    # never the database, never source code, never logged or returned in
    # any API response.
    AI_ENABLED: bool = False
    AI_DEFAULT_PROVIDER: str = "anthropic"
    AI_DEFAULT_MODEL: str = "claude-sonnet-4-5"
    AI_REQUEST_TIMEOUT_SECONDS: int = 60
    AI_MAX_CONTEXT_TOKENS: int = 8000
    AI_RETRY_MAX_ATTEMPTS: int = 2
    ANTHROPIC_API_KEY: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
