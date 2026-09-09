import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.v1.users.schemas import (
    ResetPasswordRequest,
    ResetPasswordResponse,
    RoleResponse,
    UserCreateRequest,
    UserResponse,
    UserUpdateRequest,
)
from app.core.database import get_db
from app.core.dependencies import require_permission
from app.core.redis_client import get_redis_client
from app.db.models import User
from app.modules.auth.refresh_tokens import RefreshTokenStore
from app.modules.users.service import UsersService

router = APIRouter(tags=["users"])


def get_users_service(db: Session = Depends(get_db)) -> UsersService:
    return UsersService(db, RefreshTokenStore(get_redis_client()))


def _to_user_response(service: UsersService, user: User) -> UserResponse:
    return UserResponse(
        id=user.id,
        email=user.email,
        username=user.username,
        full_name=user.full_name,
        status=user.status,
        require_password_reset=user.require_password_reset,
        last_login_at=user.last_login_at,
        role_ids=service.get_role_ids_for_user(user.id),
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


@router.get("/users", response_model=list[UserResponse])
def list_users(
    service: UsersService = Depends(get_users_service),
    _: User = Depends(require_permission("users.read")),
) -> list[UserResponse]:
    return [_to_user_response(service, u) for u in service.list_users()]


@router.post("/users", response_model=UserResponse, status_code=201)
def create_user(
    payload: UserCreateRequest,
    service: UsersService = Depends(get_users_service),
    current_user: User = Depends(require_permission("users.manage")),
) -> UserResponse:
    user = service.create_user(
        actor=current_user,
        email=payload.email,
        username=payload.username,
        full_name=payload.full_name,
        password=payload.password,
        role_ids=payload.role_ids,
    )
    return _to_user_response(service, user)


@router.get("/users/{user_id}", response_model=UserResponse)
def get_user(
    user_id: uuid.UUID,
    service: UsersService = Depends(get_users_service),
    _: User = Depends(require_permission("users.read")),
) -> UserResponse:
    return _to_user_response(service, service.get_user(user_id))


@router.put("/users/{user_id}", response_model=UserResponse)
def update_user(
    user_id: uuid.UUID,
    payload: UserUpdateRequest,
    service: UsersService = Depends(get_users_service),
    current_user: User = Depends(require_permission("users.manage")),
) -> UserResponse:
    user = service.update_user(
        actor=current_user,
        user_id=user_id,
        full_name=payload.full_name,
        username=payload.username,
        status=payload.status,
        role_ids=payload.role_ids,
    )
    return _to_user_response(service, user)


@router.post("/users/{user_id}/reset-password", response_model=ResetPasswordResponse)
def reset_password(
    user_id: uuid.UUID,
    payload: ResetPasswordRequest,
    service: UsersService = Depends(get_users_service),
    current_user: User = Depends(require_permission("users.manage")),
) -> ResetPasswordResponse:
    password = service.reset_password(actor=current_user, user_id=user_id, new_password=payload.new_password)
    return ResetPasswordResponse(temporary_password=password)


@router.get("/roles", response_model=list[RoleResponse])
def list_roles(
    service: UsersService = Depends(get_users_service),
    _: User = Depends(require_permission("users.read")),
) -> list[RoleResponse]:
    return [RoleResponse.model_validate(r) for r in service.list_roles()]
