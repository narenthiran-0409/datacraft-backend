from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.v1.auth.schemas import (
    ChangePasswordRequest,
    LoginRequest,
    LogoutRequest,
    MeResponse,
    RefreshRequest,
    TokenResponse,
)
from app.core.database import get_db
from app.core.dependencies import get_current_user, get_user_permission_codes
from app.core.redis_client import get_redis_client
from app.db.models import User
from app.modules.auth.refresh_tokens import RefreshTokenStore
from app.modules.auth.service import AuthService

router = APIRouter(prefix="/auth", tags=["auth"])


def get_auth_service(db: Session = Depends(get_db)) -> AuthService:
    return AuthService(db, RefreshTokenStore(get_redis_client()))


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, auth_service: AuthService = Depends(get_auth_service)) -> TokenResponse:
    access_token, refresh_token, _ = auth_service.login(payload.email, payload.password)
    return TokenResponse(access_token=access_token, refresh_token=refresh_token)


@router.post("/refresh", response_model=TokenResponse)
def refresh(payload: RefreshRequest, auth_service: AuthService = Depends(get_auth_service)) -> TokenResponse:
    access_token, refresh_token = auth_service.refresh(payload.refresh_token)
    return TokenResponse(access_token=access_token, refresh_token=refresh_token)


@router.post("/logout", status_code=204)
def logout(
    payload: LogoutRequest,
    current_user: User = Depends(get_current_user),
    auth_service: AuthService = Depends(get_auth_service),
) -> None:
    auth_service.logout(payload.refresh_token, current_user)


@router.get("/me", response_model=MeResponse)
def me(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> MeResponse:
    permissions = sorted(get_user_permission_codes(db, current_user.id))
    return MeResponse(
        id=current_user.id,
        email=current_user.email,
        username=current_user.username,
        full_name=current_user.full_name,
        status=current_user.status,
        permissions=permissions,
    )


@router.post("/change-password", status_code=204)
def change_password(
    payload: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    auth_service: AuthService = Depends(get_auth_service),
) -> None:
    auth_service.change_password(current_user, payload.current_password, payload.new_password)
