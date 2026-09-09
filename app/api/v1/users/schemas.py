import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr


class UserCreateRequest(BaseModel):
    email: EmailStr
    username: str | None = None
    full_name: str | None = None
    password: str
    role_ids: list[uuid.UUID] = []


class UserUpdateRequest(BaseModel):
    full_name: str | None = None
    username: str | None = None
    status: str | None = None
    role_ids: list[uuid.UUID] | None = None


class ResetPasswordRequest(BaseModel):
    new_password: str | None = None


class ResetPasswordResponse(BaseModel):
    temporary_password: str


class UserResponse(BaseModel):
    id: uuid.UUID
    email: str
    username: str | None
    full_name: str | None
    status: str
    require_password_reset: bool
    last_login_at: datetime | None
    role_ids: list[uuid.UUID]
    created_at: datetime
    updated_at: datetime | None

    model_config = {"from_attributes": True}


class RoleResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    is_system_role: bool

    model_config = {"from_attributes": True}
