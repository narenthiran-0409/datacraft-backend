import uuid
from datetime import datetime

from pydantic import BaseModel


class CredentialInput(BaseModel):
    username: str
    password: str


class ConnectionCreateRequest(BaseModel):
    data_source_id: uuid.UUID
    connection_type_id: uuid.UUID
    name: str
    environment: str = "UNKNOWN"
    host: str
    port: int
    database_name: str | None = None
    service_name: str | None = None
    username: str
    credential: CredentialInput
    config: dict = {}


class ConnectionUpdateRequest(BaseModel):
    name: str | None = None
    environment: str | None = None
    host: str | None = None
    port: int | None = None
    database_name: str | None = None
    service_name: str | None = None
    username: str | None = None
    credential: CredentialInput | None = None
    config: dict | None = None


class ConnectionResponse(BaseModel):
    id: uuid.UUID
    data_source_id: uuid.UUID
    connection_type_id: uuid.UUID
    name: str
    environment: str
    host: str
    port: int
    database_name: str | None
    service_name: str | None
    username: str
    config: dict
    status: str
    last_tested_at: datetime | None
    last_test_latency_ms: int | None
    is_active: bool
    deactivated_at: datetime | None
    created_at: datetime
    updated_at: datetime | None

    model_config = {"from_attributes": True}


class ConnectionTypeResponse(BaseModel):
    id: uuid.UUID
    code: str
    display_name: str
    is_active: bool

    model_config = {"from_attributes": True}
