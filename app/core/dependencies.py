import uuid

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.exceptions import InvalidCredentialsError, PermissionDeniedError
from app.core.security import decode_token
from app.db.models import Permission, Role, RolePermission, User, UserRole
from app.modules.audit.service import AuditingService

_bearer_scheme = HTTPBearer(auto_error=False)


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None:
        raise InvalidCredentialsError("Not authenticated")

    try:
        payload = decode_token(credentials.credentials)
    except jwt.PyJWTError:
        raise InvalidCredentialsError("Invalid or expired token") from None

    if payload.get("token_type") != "access":
        raise InvalidCredentialsError("Invalid token type")

    user = db.get(User, uuid.UUID(payload["sub"]))
    if user is None or user.status != "ACTIVE":
        raise InvalidCredentialsError("Invalid or inactive user")

    request.state.current_user_id = user.id
    return user


def get_user_permission_codes(db: Session, user_id: uuid.UUID) -> set[str]:
    rows = db.execute(
        select(Permission.code)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .join(Role, Role.id == RolePermission.role_id)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user_id)
    ).scalars()
    return set(rows)


def require_permission(code: str):
    def dependency(
        request: Request,
        current_user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> User:
        permission_codes = get_user_permission_codes(db, current_user.id)
        if code not in permission_codes:
            AuditingService(db).record(
                actor=current_user,
                action="permission.denied",
                entity_type="USER",
                entity_id=current_user.id,
                metadata={"required_permission": code, "path": str(request.url.path)},
            )
            db.commit()
            raise PermissionDeniedError(f"Missing required permission: {code}")
        return current_user

    return dependency
