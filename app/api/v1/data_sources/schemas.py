import uuid
from datetime import datetime

from pydantic import BaseModel


class DataSourceCreateRequest(BaseModel):
    name: str
    description: str | None = None
    owner_team: str | None = None
    business_domain: str | None = None


class DataSourceUpdateRequest(BaseModel):
    description: str | None = None
    owner_team: str | None = None
    business_domain: str | None = None


class DataSourceResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    owner_team: str | None
    business_domain: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime | None

    model_config = {"from_attributes": True}
