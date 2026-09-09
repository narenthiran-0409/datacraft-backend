import uuid
from datetime import datetime, timezone

import jwt
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.exceptions import AccountLockedError, InvalidCredentialsError, InvalidRefreshTokenError
from app.core.security import create_access_token, decode_token, hash_password, verify_password
from app.db.models import User
from app.modules.audit.service import AuditingService
from app.modules.auth.refresh_tokens import RefreshTokenStore


class AuthService:
    def __init__(self, db: Session, token_store: RefreshTokenStore) -> None:
        self._db = db
        self._token_store = token_store
        self._audit = AuditingService(db)

    def login(self, email: str, password: str) -> tuple[str, str, User]:
        user = self._db.execute(select(User).where(func.lower(User.email) == email.lower())).scalar_one_or_none()

        if user is None or not verify_password(password, user.password_hash):
            self._audit.record(
                actor=user,
                action="user.login_failed",
                entity_type="USER",
                entity_id=user.id if user else uuid.uuid4(),
                metadata={"email": email},
            )
            self._db.commit()
            raise InvalidCredentialsError("Invalid email or password")

        if user.status == "LOCKED":
            self._audit.record(
                actor=user,
                action="user.login_failed",
                entity_type="USER",
                entity_id=user.id,
                metadata={"reason": "account_locked"},
            )
            self._db.commit()
            raise AccountLockedError("This account is locked")

        if user.status != "ACTIVE":
            self._audit.record(
                actor=user,
                action="user.login_failed",
                entity_type="USER",
                entity_id=user.id,
                metadata={"reason": "account_inactive"},
            )
            self._db.commit()
            raise InvalidCredentialsError("Invalid email or password")

        access_token, _ = create_access_token(user.id)
        refresh_token = self._token_store.issue(user.id)

        user.last_login_at = datetime.now(timezone.utc)
        self._audit.record(actor=user, action="user.login", entity_type="USER", entity_id=user.id)
        self._db.commit()

        return access_token, refresh_token, user

    def refresh(self, refresh_token: str) -> tuple[str, str]:
        try:
            payload = decode_token(refresh_token)
        except jwt.PyJWTError:
            raise InvalidRefreshTokenError("Invalid or expired refresh token") from None

        if payload.get("token_type") != "refresh":
            raise InvalidRefreshTokenError("Invalid token type")

        jti = payload["jti"]
        user_id = self._token_store.consume(jti)
        if user_id is None:
            raise InvalidRefreshTokenError("Refresh token has already been used or is unknown")

        user = self._db.get(User, user_id)
        if user is None or user.status != "ACTIVE":
            raise InvalidRefreshTokenError("Invalid or inactive user")

        access_token, _ = create_access_token(user.id)
        new_refresh_token = self._token_store.issue(user.id)
        return access_token, new_refresh_token

    def logout(self, refresh_token: str, current_user: User) -> None:
        try:
            payload = decode_token(refresh_token)
            jti = payload.get("jti")
        except jwt.PyJWTError:
            jti = None

        if jti:
            self._token_store.revoke(jti, current_user.id)

        self._audit.record(actor=current_user, action="user.logout", entity_type="USER", entity_id=current_user.id)
        self._db.commit()

    def change_password(self, user: User, current_password: str, new_password: str) -> None:
        if not verify_password(current_password, user.password_hash):
            raise InvalidCredentialsError("Current password is incorrect")

        before = {"last_password_changed_at": user.last_password_changed_at.isoformat() if user.last_password_changed_at else None}
        user.password_hash = hash_password(new_password)
        user.last_password_changed_at = datetime.now(timezone.utc)
        user.require_password_reset = False

        self._token_store.revoke_all_for_user(user.id)

        self._audit.record(
            actor=user,
            action="user.password_changed",
            entity_type="USER",
            entity_id=user.id,
            before=before,
            after={"last_password_changed_at": user.last_password_changed_at.isoformat()},
        )
        self._db.commit()
