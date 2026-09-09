import uuid
from datetime import datetime
from decimal import Decimal

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
