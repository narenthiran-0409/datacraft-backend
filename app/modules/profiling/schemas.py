import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class ProfileRunCreateRequest(BaseModel):
    sample_size: int | None = Field(
        default=None,
        description="Explicit sample size. Omit to use PROFILING_DEFAULT_SAMPLE_SIZE. "
        "An explicit value over PROFILING_MAX_SAMPLE_SIZE is rejected with 422, never silently downgraded.",
    )
    full_scan: bool = Field(
        default=False,
        description="Profile every row instead of a sample. Rejected with 422 if the dataset's "
        "row_count_estimate exceeds PROFILING_MAX_FULL_SCAN_ROWS.",
    )
    include_top_values: bool = Field(
        default=False,
        description="Populate column_profiles.value_distribution with up to the 10 most frequent "
        "values per column (each truncated to ~100 characters). Off by default.",
    )


class ProfileRunResponse(BaseModel):
    id: uuid.UUID
    dataset_id: uuid.UUID
    job_id: uuid.UUID | None
    status: str
    sample_size: int | None
    row_count: int | None = Field(
        description="Exact if this run performed a full scan; otherwise an estimate captured from "
        "Discovery's catalog statistics (datasets.row_count_estimate) at the time this run executed."
    )
    quality_score: Decimal | None = Field(
        default=None,
        description="Reserved for a later Validation/Rule Engine phase. Never populated by Profiling — "
        "always null on every row this phase produces.",
    )
    null_percentage: Decimal | None = Field(
        default=None,
        description="Reserved for a later Validation/Rule Engine phase. Never populated by Profiling — "
        "always null on every row this phase produces.",
    )
    duplicate_percentage: Decimal | None = Field(
        description="Full-row duplication rate computed from the sampled rows only — an estimate, "
        "not an exact dataset-wide measurement."
    )
    error_message: str | None
    triggered_by: uuid.UUID | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ColumnProfileResponse(BaseModel):
    """Not currently returned by any Phase 4 endpoint — only the 4 routes
    the approved plan named exist (none of them return column-level data;
    column_profiles rows are inspected directly against the database this
    phase). Kept because the approved plan's schema list requires it.

    pattern_summary is a JSONB blob (not separately typed) that always
    contains at least:
      - exact_stats: bool — whether null_count/distinct_count/duplicate_count
        for THIS column came from the source database's exact push-down
        query (get_dataset_column_stats) or were derived from the pulled
        sample because the exact-stats call timed out for this run.
      - blank_count / blank_percentage — empty-string blanks, distinct from
        NULLs, for string-typed columns.
      It may also contain dominant string-shape pattern info for string
      columns (e.g. detected format buckets)."""

    id: uuid.UUID
    profile_run_id: uuid.UUID
    column_id: uuid.UUID
    null_count: int | None
    null_percentage: Decimal | None
    distinct_count: int | None
    distinct_percentage: Decimal | None
    duplicate_count: int | None
    duplicate_percentage: Decimal | None
    min_value: str | None
    max_value: str | None
    mean_value: Decimal | None
    median_value: Decimal | None
    mode_value: str | None
    min_length: int | None
    max_length: int | None
    avg_length: Decimal | None
    outlier_count: int | None
    pattern_summary: dict | None
    value_distribution: list[dict] | None = Field(
        default=None,
        description="Top-N (max 10) most frequent values, each value truncated to ~100 characters. "
        "Only populated when the triggering run had include_top_values=True.",
    )
    created_at: datetime
    stddev_value: Decimal | None
    sum_value: Decimal | None

    model_config = {"from_attributes": True}
