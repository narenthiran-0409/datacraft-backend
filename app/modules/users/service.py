import secrets
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.exceptions import EmailAlreadyExistsError, UserNotFoundError
from app.core.security import hash_password
from app.db.models import Role, User, UserRole
from app.modules.audit.service import AuditingService
from app.modules.auth.refresh_tokens import RefreshTokenStore


class UsersService:
    def __init__(self, db: Session, token_store: RefreshTokenStore) -> None:
        self._db = db
        self._token_store = token_store
        self._audit = AuditingService(db)

    def list_users(self) -> list[User]:
        return list(self._db.execute(select(User).order_by(User.created_at)).scalars())

    def get_user(self, user_id: uuid.UUID) -> User:
        user = self._db.get(User, user_id)
        if user is None:
            raise UserNotFoundError(f"User {user_id} not found")
        return user

    def get_role_ids_for_user(self, user_id: uuid.UUID) -> list[uuid.UUID]:
        return list(self._db.execute(select(UserRole.role_id).where(UserRole.user_id == user_id)).scalars())

    def create_user(
        self,
        *,
        actor: User,
        email: str,
        username: str | None,
        full_name: str | None,
        password: str,
        role_ids: list[uuid.UUID],
    ) -> User:
        existing = self._db.execute(select(User).where(func.lower(User.email) == email.lower())).scalar_one_or_none()
        if existing is not None:
            raise EmailAlreadyExistsError(f"A user with email {email} already exists")

        user = User(
            email=email,
            username=username,
            full_name=full_name,
            password_hash=hash_password(password),
            created_by=actor.id,
        )
        self._db.add(user)
        self._db.flush()

        self._assign_roles(user, role_ids, actor)

        self._audit.record(
            actor=actor,
            action="user.created",
            entity_type="USER",
            entity_id=user.id,
            after={"email": user.email, "username": user.username, "role_ids": [str(r) for r in role_ids]},
        )
        self._db.commit()
        self._db.refresh(user)
        return user

    def update_user(
        self,
        *,
        actor: User,
        user_id: uuid.UUID,
        full_name: str | None,
        username: str | None,
        status: str | None,
        role_ids: list[uuid.UUID] | None,
    ) -> User:
        user = self.get_user(user_id)
        before = {"full_name": user.full_name, "username": user.username, "status": user.status}

        if full_name is not None:
            user.full_name = full_name
        if username is not None:
            user.username = username
        if status is not None:
            user.status = status
        user.updated_at = datetime.now(timezone.utc)

        if role_ids is not None:
            self._replace_roles(user, role_ids, actor)

        self._audit.record(
            actor=actor,
            action="user.updated",
            entity_type="USER",
            entity_id=user.id,
            before=before,
            after={"full_name": user.full_name, "username": user.username, "status": user.status},
        )
        self._db.commit()
        self._db.refresh(user)
        return user

    def reset_password(self, *, actor: User, user_id: uuid.UUID, new_password: str | None) -> str:
        user = self.get_user(user_id)
        generated = new_password is None
        password = new_password or secrets.token_urlsafe(12)

        user.password_hash = hash_password(password)
        user.require_password_reset = True
        user.last_password_changed_at = datetime.now(timezone.utc)
        user.updated_at = user.last_password_changed_at

        self._token_store.revoke_all_for_user(user.id)

        self._audit.record(
            actor=actor,
            action="user.password_changed",
            entity_type="USER",
            entity_id=user.id,
            metadata={"reset_by": str(actor.id), "generated": generated},
        )
        self._db.commit()
        return password

    def _assign_roles(self, user: User, role_ids: list[uuid.UUID], actor: User) -> None:
        for role_id in role_ids:
            self._db.add(UserRole(user_id=user.id, role_id=role_id, assigned_by=actor.id))
        if role_ids:
            self._audit.record(
                actor=actor,
                action="user.role_assigned",
                entity_type="USER",
                entity_id=user.id,
                after={"role_ids": [str(r) for r in role_ids]},
            )

    def _replace_roles(self, user: User, role_ids: list[uuid.UUID], actor: User) -> None:
        existing = set(self.get_role_ids_for_user(user.id))
        desired = set(role_ids)
        if existing == desired:
            return

        self._db.query(UserRole).filter(UserRole.user_id == user.id).delete()
        self._db.flush()
        for role_id in role_ids:
            self._db.add(UserRole(user_id=user.id, role_id=role_id, assigned_by=actor.id))

        self._audit.record(
            actor=actor,
            action="user.role_assigned",
            entity_type="USER",
            entity_id=user.id,
            before={"role_ids": [str(r) for r in existing]},
            after={"role_ids": [str(r) for r in desired]},
        )

    def list_roles(self) -> list[Role]:
        return list(self._db.execute(select(Role).order_by(Role.name)).scalars())
