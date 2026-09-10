from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ConnectionType, Permission, Role, RolePermission

# Permissions seeded by Phase 2 migration 0006.
PHASE2_PERMISSIONS = {
    "users.read",
    "users.manage",
    "connections.read",
    "connections.manage",
    "data_sources.read",
    "data_sources.manage",
    "audit.read",
}

# Permissions seeded by Phase 3 migration 0007, granted to every role per
# the frozen role hierarchy (see that migration's docstring).
PHASE3_PERMISSIONS = {"discovery.run", "metadata.read", "metadata.manage"}

# Permission seeded by Phase 4 migration 0008, granted to every role per the
# same frozen role hierarchy (see that migration's docstring).
PHASE4_PERMISSIONS = {"profiling.run"}

# Permission seeded by Phase 5 migration 0009, granted to every role per the
# same frozen role hierarchy (see that migration's docstring).
PHASE5_PERMISSIONS = {"validation.run"}

# Permissions seeded by Phase 6 migration 0010, granted to every role per the
# same frozen role hierarchy (see that migration's docstring).
PHASE6_PERMISSIONS = {"review.read", "review.edit"}

# Permissions seeded by Phase 7 migration 0011. Unlike every prior phase,
# NOT a uniform grant: approval.read goes to all 5 roles (same "everyone
# inherits" pattern as before), but approval.decide goes only to
# administrator, approver, and publisher — the first genuine divergence
# between analyst/reviewer and approver in this project's permission
# history (see that migration's docstring).
PHASE7_UNIVERSAL_PERMISSIONS = {"approval.read"}
PHASE7_DECIDE_ONLY_PERMISSIONS = {"approval.decide"}

# Permissions seeded by Phase 8 migration 0012 — same non-uniform split
# pattern as Phase 7: staging.read goes to all 5 roles, staging.create only
# to administrator, approver, and publisher (see that migration's
# docstring).
PHASE8_UNIVERSAL_PERMISSIONS = {"staging.read"}
PHASE8_CREATE_ONLY_PERMISSIONS = {"staging.create"}

# Permissions seeded by Phase 9 migration 0013. Same non-uniform split
# pattern as Phase 7/8, but with one new twist: publish.read goes to all 5
# roles, while publish.execute goes ONLY to administrator and publisher —
# NOT approver. This is the first phase in this project where a role does
# not simply inherit the next stage's capability (approver decides
# approvals and can trigger staging, but cannot trigger/acknowledge
# publishing) — see the 0013 migration's docstring.
PHASE9_UNIVERSAL_PERMISSIONS = {"publish.read"}
PHASE9_EXECUTE_ONLY_PERMISSIONS = {"publish.execute"}

# Permission seeded by Phase 10 migration 0014, granted to every role — back
# to the uniform "everyone inherits" pattern (unlike Phase 7/8/9's
# non-uniform splits), since lineage has no mutation surface at all: there
# is no lineage.manage/lineage.build, so there is nothing to withhold from
# any role (see the 0014 migration's docstring).
PHASE10_PERMISSIONS = {"lineage.read"}

# Permission seeded by Phase 11 migration 0015, granted to every role —
# same uniform pattern as Phase 10: Reports has no mutation surface at
# all (no reports.manage/create/export/admin), so there is nothing to
# withhold from any role (see the 0015 migration's docstring).
PHASE11_PERMISSIONS = {"reports.read"}

# Permissions seeded by Phase 12 migration 0016. Non-uniform split, but
# unlike Phase 7/8/9's "universal read + restricted action" shape, BOTH
# ai.chat and ai.suggest go to the SAME four roles (administrator, analyst,
# approver, reviewer) — publisher alone gets neither. AI is an advisory
# data-quality/review capability, not a publishing capability (see the
# 0016 migration's docstring).
PHASE12_PERMISSIONS = {"ai.chat", "ai.suggest"}

# Permissions seeded by migration 0017 — a correction, not a new phase: the
# locked Phase 5 design specified rules.read/rules.manage/
# rule_assignments.manage for the Rule Catalog module, but migrations
# 0006/0007/0009 never actually seeded them (Phase 5's rule-catalog routes
# were left gated behind Phase 3's metadata.read/metadata.manage instead).
# Non-uniform split, same shape as Phase 7/8/9: rules.read and
# rule_assignments.manage go to all five roles, but rules.manage — the gate
# on rule creation/versioning, since CUSTOM_EXPRESSION rules are
# code-execution-adjacent — goes to administrator only.
PHASE5_RBAC_CORRECTION_UNIVERSAL_PERMISSIONS = {"rules.read", "rule_assignments.manage"}
PHASE5_RBAC_CORRECTION_ADMIN_ONLY_PERMISSIONS = {"rules.manage"}

# Permission seeded by migration 0018 for the new Data Preview endpoint
# (GET /api/v1/datasets/{id}/preview). Granted to all five roles uniformly
# — a pure read with no side effects, so it follows this project's
# universal-read convention (metadata.read, profiling.run, validation.run,
# etc. are all universal too) rather than the write/action-tier convention
# that produces non-uniform splits (rules.manage, publish.execute, ...).
# See that migration's docstring for the full reasoning — this is the
# first permission gating raw source row content rather than metadata.
DATA_PREVIEW_PERMISSIONS = {"data_preview.read"}

ALL_ROLE_PERMISSIONS = (
    PHASE2_PERMISSIONS
    | PHASE3_PERMISSIONS
    | PHASE4_PERMISSIONS
    | PHASE5_PERMISSIONS
    | PHASE6_PERMISSIONS
    | PHASE7_UNIVERSAL_PERMISSIONS
    | PHASE7_DECIDE_ONLY_PERMISSIONS
    | PHASE8_UNIVERSAL_PERMISSIONS
    | PHASE8_CREATE_ONLY_PERMISSIONS
    | PHASE9_UNIVERSAL_PERMISSIONS
    | PHASE9_EXECUTE_ONLY_PERMISSIONS
    | PHASE10_PERMISSIONS
    | PHASE11_PERMISSIONS
    | PHASE12_PERMISSIONS
    | PHASE5_RBAC_CORRECTION_UNIVERSAL_PERMISSIONS
    | PHASE5_RBAC_CORRECTION_ADMIN_ONLY_PERMISSIONS
    | DATA_PREVIEW_PERMISSIONS
)


def test_connection_types_seeded(db: Session) -> None:
    codes = set(db.execute(select(ConnectionType.code)).scalars())
    assert codes == {"POSTGRESQL", "SQL_SERVER", "MYSQL", "ORACLE", "SAP_HANA"}


def test_roles_seeded(db: Session) -> None:
    names = set(db.execute(select(Role.name)).scalars())
    assert names == {"analyst", "reviewer", "approver", "publisher", "administrator"}


def test_permissions_seeded(db: Session) -> None:
    codes = set(db.execute(select(Permission.code)).scalars())
    assert codes == ALL_ROLE_PERMISSIONS


def _permission_codes_for_role(db: Session, role_name: str) -> set[str]:
    rows = db.execute(
        select(Permission.code)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .join(Role, Role.id == RolePermission.role_id)
        .where(Role.name == role_name)
    ).scalars()
    return set(rows)


def test_administrator_has_every_permission(db: Session) -> None:
    assert _permission_codes_for_role(db, "administrator") == ALL_ROLE_PERMISSIONS


def test_analyst_has_phase2_reads_plus_all_phase3_and_phase4_permissions(db: Session) -> None:
    assert _permission_codes_for_role(db, "analyst") == {
        "users.read",
        "connections.read",
        "data_sources.read",
        "audit.read",
    } | PHASE3_PERMISSIONS | PHASE4_PERMISSIONS | PHASE5_PERMISSIONS | PHASE6_PERMISSIONS | PHASE7_UNIVERSAL_PERMISSIONS | PHASE8_UNIVERSAL_PERMISSIONS | PHASE9_UNIVERSAL_PERMISSIONS | PHASE10_PERMISSIONS | PHASE11_PERMISSIONS | PHASE12_PERMISSIONS | PHASE5_RBAC_CORRECTION_UNIVERSAL_PERMISSIONS | DATA_PREVIEW_PERMISSIONS


def test_reviewer_has_only_phase3_through_phase6_plus_approval_read(db: Session) -> None:
    # reviewer does NOT get approval.decide or staging.create — the
    # divergence from approver/publisher in this project's permission
    # history, now established for both Phase 7 and Phase 8.
    assert (
        _permission_codes_for_role(db, "reviewer")
        == PHASE3_PERMISSIONS
        | PHASE4_PERMISSIONS
        | PHASE5_PERMISSIONS
        | PHASE6_PERMISSIONS
        | PHASE7_UNIVERSAL_PERMISSIONS
        | PHASE8_UNIVERSAL_PERMISSIONS
        | PHASE9_UNIVERSAL_PERMISSIONS
        | PHASE10_PERMISSIONS
        | PHASE11_PERMISSIONS
        | PHASE12_PERMISSIONS
        | PHASE5_RBAC_CORRECTION_UNIVERSAL_PERMISSIONS
        | DATA_PREVIEW_PERMISSIONS
    )


def test_approver_has_reviewer_set_plus_approval_decide_and_staging_create_but_not_publish_execute(db: Session) -> None:
    # Phase 9's genuine divergence: approver keeps every Phase 7/8
    # capability (approval.decide, staging.create) but does NOT get
    # publish.execute — the first time in this project a role doesn't
    # simply inherit the next stage's action permission.
    expected = (
        PHASE3_PERMISSIONS
        | PHASE4_PERMISSIONS
        | PHASE5_PERMISSIONS
        | PHASE6_PERMISSIONS
        | PHASE7_UNIVERSAL_PERMISSIONS
        | PHASE7_DECIDE_ONLY_PERMISSIONS
        | PHASE8_UNIVERSAL_PERMISSIONS
        | PHASE8_CREATE_ONLY_PERMISSIONS
        | PHASE9_UNIVERSAL_PERMISSIONS
        | PHASE10_PERMISSIONS
        | PHASE11_PERMISSIONS
        | PHASE12_PERMISSIONS
        | PHASE5_RBAC_CORRECTION_UNIVERSAL_PERMISSIONS
        | DATA_PREVIEW_PERMISSIONS
    )
    assert _permission_codes_for_role(db, "approver") == expected


def test_publisher_has_approver_set_plus_publish_execute(db: Session) -> None:
    # Phase 12's genuine divergence, mirroring Phase 9's own: publisher
    # does NOT get ai.chat/ai.suggest — AI is advisory to review/approval,
    # not a publishing capability.
    expected = (
        PHASE3_PERMISSIONS
        | PHASE4_PERMISSIONS
        | PHASE5_PERMISSIONS
        | PHASE6_PERMISSIONS
        | PHASE7_UNIVERSAL_PERMISSIONS
        | PHASE7_DECIDE_ONLY_PERMISSIONS
        | PHASE8_UNIVERSAL_PERMISSIONS
        | PHASE8_CREATE_ONLY_PERMISSIONS
        | PHASE9_UNIVERSAL_PERMISSIONS
        | PHASE9_EXECUTE_ONLY_PERMISSIONS
        | PHASE10_PERMISSIONS
        | PHASE11_PERMISSIONS
        | PHASE5_RBAC_CORRECTION_UNIVERSAL_PERMISSIONS
        | DATA_PREVIEW_PERMISSIONS
    )
    assert _permission_codes_for_role(db, "publisher") == expected
