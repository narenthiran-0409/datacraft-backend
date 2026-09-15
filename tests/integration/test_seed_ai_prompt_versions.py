"""Tests for scripts/seed_ai_prompt_versions.py's versioning behavior —
added per the Phase 4.5 acceptance-fix report's PROMPT VERSION CHECK
requirement. Runs against the real (isolated) test database via the
standard `db` fixture — never the live dataquality DB, and this file
never invokes the script's own CLI/`main()`, only the underlying
`seed_ai_prompt_versions()` function directly, so nothing here seeds any
real environment.
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AIPromptVersion
from scripts.seed_ai_prompt_versions import seed_ai_prompt_versions

_TEST_KEY = "ai_correction"


def _versions(db: Session) -> list[AIPromptVersion]:
    return list(
        db.execute(
            select(AIPromptVersion).where(AIPromptVersion.prompt_key == _TEST_KEY).order_by(AIPromptVersion.version_number)
        ).scalars()
    )


def test_fresh_seed_creates_version_1_active(db: Session) -> None:
    seed_ai_prompt_versions(db, force=False, created_by_email=None)
    versions = _versions(db)
    assert len(versions) == 1
    assert versions[0].version_number == 1
    assert versions[0].is_active is True


def test_seed_without_force_skips_when_an_active_version_already_exists(db: Session) -> None:
    seed_ai_prompt_versions(db, force=False, created_by_email=None)
    first_id = _versions(db)[0].id

    seed_ai_prompt_versions(db, force=False, created_by_email=None)  # should be a no-op
    versions = _versions(db)

    assert len(versions) == 1
    assert versions[0].id == first_id
    assert versions[0].is_active is True


def test_force_reseed_preserves_the_old_active_version_as_a_retired_row(db: Session) -> None:
    """The exact behavior the Phase 4.5 acceptance-fix report needed
    confirmed: --force must never update/overwrite the existing active
    row's template in place — it retires it (is_active=False, template
    and id unchanged) and INSERTS a brand new row at version_number + 1."""
    seed_ai_prompt_versions(db, force=False, created_by_email=None)
    original = _versions(db)[0]
    original_id, original_template, original_created_at = original.id, original.template, original.created_at

    seed_ai_prompt_versions(db, force=True, created_by_email=None)
    versions = _versions(db)

    assert len(versions) == 2  # the old row still exists — never deleted or overwritten
    v1, v2 = versions[0], versions[1]

    assert v1.id == original_id  # same row, not replaced
    assert v1.version_number == 1
    assert v1.template == original_template  # untouched
    assert v1.created_at == original_created_at  # untouched
    assert v1.is_active is False  # retired, not deleted

    assert v2.id != original_id  # a genuinely new row
    assert v2.version_number == 2
    assert v2.is_active is True
    assert v2.template == original_template  # PROMPTS dict unchanged between these two calls in this test


def test_repeated_force_reseeds_keep_creating_new_versions_never_reusing_numbers(db: Session) -> None:
    seed_ai_prompt_versions(db, force=False, created_by_email=None)
    seed_ai_prompt_versions(db, force=True, created_by_email=None)
    seed_ai_prompt_versions(db, force=True, created_by_email=None)
    versions = _versions(db)

    assert [v.version_number for v in versions] == [1, 2, 3]
    assert [v.is_active for v in versions] == [False, False, True]
    # Every earlier row still fully intact — full version history preserved.
    assert len({v.id for v in versions}) == 3


def test_current_seed_script_ai_correction_template_would_become_version_4(db: Session) -> None:
    """Documents, against the real seeding function (not just by reading
    the source), that the current in-repo PROMPTS["ai_correction"] text —
    the Phase 4.5 v4 candidate — is a genuinely NEW version rather than an
    edit of the existing active v3 row, simulating the DB history this
    project's real environment already has (v1 retired, v2 retired, v3
    active) before applying the current script's content."""
    from app.db.models import User

    admin = db.execute(select(User).limit(1)).scalar_one_or_none()
    created_by = admin.id if admin is not None else None

    for version_number in (1, 2):
        db.add(
            AIPromptVersion(
                prompt_key=_TEST_KEY, version_number=version_number, template=f"historical v{version_number}",
                default_model=None, is_active=False, created_by=created_by,
            )
        )
    db.add(
        AIPromptVersion(
            prompt_key=_TEST_KEY, version_number=3, template="historical v3 (currently active)",
            default_model=None, is_active=True, created_by=created_by,
        )
    )
    db.commit()

    seed_ai_prompt_versions(db, force=True, created_by_email=None)
    versions = _versions(db)

    assert [v.version_number for v in versions] == [1, 2, 3, 4]
    assert versions[2].template == "historical v3 (currently active)"  # v3 untouched
    assert versions[2].is_active is False  # retired
    assert versions[3].is_active is True  # v4 is the new active version
    assert versions[3].template != versions[2].template  # genuinely new content, not a copy
