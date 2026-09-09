"""Standalone script to bootstrap the first administrator user.

Not run automatically by any migration or app startup path — run manually,
once, against the target database:

    python scripts/create_admin_user.py --email admin@example.com --password 'Str0ng!Passw0rd'

If --password is omitted, a random password is generated and printed once.
Requires the 'administrator' role to already exist (seeded by migration
0006_seed_roles_and_permissions).
"""
import argparse
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.core.database import SessionLocal
from app.core.security import hash_password
from app.db.models import Role, User, UserRole


def create_admin_user(email: str, password: str) -> None:
    db = SessionLocal()
    try:
        existing = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if existing is not None:
            raise SystemExit(f"A user with email {email} already exists (id={existing.id})")

        admin_role = db.execute(select(Role).where(Role.name == "administrator")).scalar_one_or_none()
        if admin_role is None:
            raise SystemExit(
                "No 'administrator' role found. Run migrations first "
                "(alembic upgrade head), which seeds it via 0006_seed_roles_and_permissions."
            )

        user = User(
            email=email,
            password_hash=hash_password(password),
            status="ACTIVE",
            require_password_reset=True,
        )
        db.add(user)
        db.flush()

        db.add(UserRole(user_id=user.id, role_id=admin_role.id))
        db.commit()

        print(f"Created administrator user {email} (id={user.id})")
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Create the first administrator user.")
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=False, default=None)
    args = parser.parse_args()

    password = args.password or secrets.token_urlsafe(16)
    create_admin_user(args.email, password)

    if args.password is None:
        print(f"Generated password (save this now, it will not be shown again): {password}")


if __name__ == "__main__":
    main()
