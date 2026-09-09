import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import EmailAlreadyExistsError
from app.db.models import AuditEvent, Role
from app.modules.auth.refresh_tokens import RefreshTokenStore
from app.modules.users.service import UsersService


def _service(db: Session, redis_client) -> UsersService:
    return UsersService(db, RefreshTokenStore(redis_client))


def test_create_user_assigns_role_and_audits(db: Session, redis_client, admin_user) -> None:
    service = _service(db, redis_client)
    analyst_role = db.execute(select(Role).where(Role.name == "analyst")).scalar_one()

    user = service.create_user(
        actor=admin_user,
        email="new.user@example.com",
        username="newuser",
        full_name="New User",
        password="Str0ng!Passw0rd",
        role_ids=[analyst_role.id],
    )

    assert user.email == "new.user@example.com"
    assert service.get_role_ids_for_user(user.id) == [analyst_role.id]

    events = db.execute(select(AuditEvent).where(AuditEvent.entity_id == user.id)).scalars().all()
    actions = {e.action for e in events}
    assert "user.created" in actions
    assert "user.role_assigned" in actions


def test_create_user_duplicate_email_rejected(db: Session, redis_client, admin_user) -> None:
    service = _service(db, redis_client)
    service.create_user(
        actor=admin_user, email="dupe@example.com", username=None, full_name=None,
        password="Str0ng!Passw0rd", role_ids=[],
    )

    with pytest.raises(EmailAlreadyExistsError):
        service.create_user(
            actor=admin_user, email="DUPE@example.com", username=None, full_name=None,
            password="Str0ng!Passw0rd", role_ids=[],
        )


def test_update_user_replaces_roles_and_audits(db: Session, redis_client, admin_user) -> None:
    service = _service(db, redis_client)
    analyst_role = db.execute(select(Role).where(Role.name == "analyst")).scalar_one()
    reviewer_role = db.execute(select(Role).where(Role.name == "reviewer")).scalar_one()

    user = service.create_user(
        actor=admin_user, email="upd@example.com", username=None, full_name=None,
        password="Str0ng!Passw0rd", role_ids=[analyst_role.id],
    )

    updated = service.update_user(
        actor=admin_user, user_id=user.id, full_name="Updated Name", username=None,
        status=None, role_ids=[reviewer_role.id],
    )

    assert updated.full_name == "Updated Name"
    assert service.get_role_ids_for_user(user.id) == [reviewer_role.id]


def test_reset_password_revokes_refresh_tokens(db: Session, redis_client, admin_user) -> None:
    service = _service(db, redis_client)
    user = service.create_user(
        actor=admin_user, email="reset@example.com", username=None, full_name=None,
        password="Str0ng!Passw0rd", role_ids=[],
    )

    store = RefreshTokenStore(redis_client)
    store.issue(user.id)
    assert redis_client.smembers(f"user_refresh_tokens:{user.id}")

    service.reset_password(actor=admin_user, user_id=user.id, new_password="N3wStr0ng!Passw0rd")

    assert not redis_client.smembers(f"user_refresh_tokens:{user.id}")

    events = db.execute(select(AuditEvent).where(AuditEvent.entity_id == user.id, AuditEvent.action == "user.password_changed")).scalars().all()
    assert len(events) == 1
