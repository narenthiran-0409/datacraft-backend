import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel


class SchemaResponse(BaseModel):
    id: uuid.UUID
    connection_id: uuid.UUID
    name: str
    is_active: bool
    discovered_at: datetime
    created_at: datetime
    updated_at: datetime | None

    model_config = {"from_attributes": True}


class DatasetResponse(BaseModel):
    id: uuid.UUID
    schema_id: uuid.UUID
    name: str
    object_type: str
    key_strategy: str
    row_count_estimate: int | None
    column_count: int | None
    last_quality_score: Decimal | None
    is_active: bool
    discovered_at: datetime
    created_at: datetime
    updated_at: datetime | None

    model_config = {"from_attributes": True}


class DatasetListResponse(BaseModel):
    items: list[DatasetResponse]
    total: int
    page: int
    page_size: int


class ColumnResponse(BaseModel):
    id: uuid.UUID
    dataset_id: uuid.UUID
    name: str
    ordinal_position: int
    native_data_type: str | None
    normalized_data_type: str
    max_length: int | None
    numeric_precision: int | None
    numeric_scale: int | None
    is_nullable: bool
    is_primary_key: bool
    is_active: bool
    discovered_at: datetime

    model_config = {"from_attributes": True}


class KeyColumnInput(BaseModel):
    column_id: uuid.UUID
    ordinal: int


class KeyColumnsRequest(BaseModel):
    columns: list[KeyColumnInput]


class DatasetPatchRequest(BaseModel):
    is_active: bool


class DatasetPreviewResponse(BaseModel):
    """Live rows pulled directly from the source database, never from any
    table this platform owns. String values in `rows` are each truncated
    to 100 characters (see PreviewService). `capped_to_max` is True when
    `requested_row_count` exceeded the server-enforced cap (100) and was
    silently reduced."""

    dataset_id: uuid.UUID
    schema_name: str
    table_name: str
    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int
    requested_row_count: int
    capped_to_max: bool = False


class BusinessKeyRejectedColumnResponse(BaseModel):
    name: str
    normalized_data_type: str
    reason: str


class BusinessKeyCandidateResponse(BaseModel):
    columns: list[str]
    width: int
    status: str
    reason: str | None
    total_rows_evaluated: int
    null_key_rows: int
    distinct_key_count: int
    duplicate_key_groups: int
    verification_level: str | None


class BusinessKeyDiscoveryResponse(BaseModel):
    """Phase 4.6 — DETECTION ONLY. Never implies adoption: adoption is a
    separate explicit call to POST /datasets/{dataset_id}/business-key/confirm.
    `already_has_reliable_key=True` means discovery did not even run —
    an existing source PK or already-declared key is always preferred."""

    dataset_id: uuid.UUID
    existing_key_strategy: str
    already_has_reliable_key: bool
    status: str | None
    recommended: BusinessKeyCandidateResponse | None
    candidates: list[BusinessKeyCandidateResponse]
    rejected_columns: list[BusinessKeyRejectedColumnResponse]
    widths_searched: list[int]
    total_rows_evaluated: int | None
    is_full_scan: bool | None
    reason: str | None


class BusinessKeyConfirmRequest(BaseModel):
    """Columns the caller believes are currently the verified candidate —
    echoed back from a prior discover() call. Confirmation always
    reverifies live and rejects if this no longer matches what a fresh
    check finds, so this can never silently adopt stale evidence."""

    columns: list[str]
