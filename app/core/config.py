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

    # A data source/connection deactivated for longer than this is excluded
    # from list responses entirely, even when inactive records are
    # explicitly requested — still soft-deactivated in the database, never
    # deleted, just no longer surfaced by the list endpoints. See
    # DataSourcesService.list_data_sources / ConnectionsService.list_connections.
    INACTIVE_RECORD_VISIBILITY_DAYS: int = 30

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

    # The affected-record audit-layer build (StagingService._build_records,
    # producing staging_records rows) is still fully synchronous within the
    # HTTP request — this ceiling is what keeps that request bounded.
    # Exceeding it is rejected with 422 before any staging_runs row is
    # created, never silently truncated. Phase 4.12's full-dataset physical
    # materialization is a SEPARATE, always-async step (Celery job) that
    # this setting does not bound — its cost scales with total source row
    # count, not the number of approved corrections.
    MAX_SYNCHRONOUS_STAGING_RECORDS: int = 50000
    # Bounded timeout for the batched fetch_rows_by_keys() call. Gets its
    # own setting rather than reusing PROFILING_QUERY_TIMEOUT_SECONDS, which
    # is documented as being for ordinary profiling calls specifically.
    STAGING_QUERY_TIMEOUT_SECONDS: int = 30

    # Phase 4.12 — materialized staging dataset. Bounds how many source rows
    # are held in memory at once by provider.iter_rows() during a full-table
    # copy into staging_data.<table> — the whole point of the batched/
    # streaming read contract (never provider.sample_rows(), never a full
    # in-memory materialization of the source table). Deliberately its own
    # setting rather than reusing any existing sample-size constant, since
    # this bounds a single INSERT batch's memory footprint, not a sample.
    STAGING_MATERIALIZATION_BATCH_SIZE: int = 2000
    # Bounded timeout for a single provider.count_rows() / iter_rows() batch
    # query against the source, mirroring STAGING_QUERY_TIMEOUT_SECONDS' role
    # for fetch_rows_by_keys().
    STAGING_MATERIALIZATION_QUERY_TIMEOUT_SECONDS: int = 60

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

    # Adaptive correction evidence (Phase 1 — app.modules.ai.evidence wired
    # into AISuggestionService.generate_corrections). Defaults OFF: when
    # False, no live source query is ever attempted and correction behavior
    # is unchanged from before this feature existed. The failing row is
    # located via a targeted provider.fetch_rows_by_keys() lookup (Phase 3);
    # comparable rows still come from one bounded provider.sample_rows()
    # call, deliberately never a full-table scan. See
    # AISuggestionService._gather_relationship_evidence's docstring.
    AI_CORRECTION_EVIDENCE_ENABLED: bool = False
    AI_CORRECTION_COMPARABLE_SAMPLE_SIZE: int = 2000
    AI_CORRECTION_MIN_COMPARABLE_GROUP_SIZE: int = 3
    AI_CORRECTION_MIN_FIT_QUALITY: float = 0.9

    # Phase 4.1 foundation only: reserved for the advanced multi-strategy
    # candidate-generation work (sequence/gap, string/template,
    # date-progression evidence, candidate aggregation/ranking — see
    # app/modules/ai/candidates.py). Nothing reads this setting yet; no
    # code path is gated on it until a later Phase 4 sub-phase wires one
    # up. Defaults False, same safe-by-default posture as
    # AI_CORRECTION_EVIDENCE_ENABLED.
    AI_CORRECTION_ADVANCED_INFERENCE_ENABLED: bool = False

    # Phase 4.6 — business-key candidate discovery. One bounded
    # provider.sample_rows() call per discovery/reverification request,
    # never a dedicated new provider capability (see
    # app.modules.datasets.business_key_service). Generous relative to
    # AI_CORRECTION_COMPARABLE_SAMPLE_SIZE because this needs the WHOLE
    # table to claim VERIFIED_UNIQUE (via SampleResult.is_full_scan) —
    # a real full-table scan for genuinely large tables would need a
    # dedicated provider aggregate capability, deliberately out of scope
    # here (see Phase 4.6 final report's "known limitations").
    BUSINESS_KEY_DISCOVERY_SAMPLE_SIZE: int = 5000


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
