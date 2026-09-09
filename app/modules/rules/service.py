import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import (
    DatasetNotFoundError,
    DuplicateRuleAssignmentError,
    InvalidRuleAssignmentScopeError,
    RuleAssignmentNotFoundError,
    RuleNotFoundError,
    RuleVersionNotFoundError,
    UnsupportedRuleTypeError,
)
from app.db.models import Column, Dataset, Rule, RuleAssignment, RuleAssignmentColumn, RuleVersion, User
from app.modules.audit.service import AuditingService

# The six rule types approved for Phase 5. REFERENTIAL_INTEGRITY is
# explicitly deferred to a future phase — not present here, and the frozen
# design doc declares no enum for rules.rule_type, so this is an
# application-layer restriction, not a DB CHECK constraint (see migration
# 0009's comment on the `rules` table for the reasoning).
SUPPORTED_RULE_TYPES = frozenset({"COMPLETENESS", "UNIQUENESS", "DUPLICATE", "RANGE", "PATTERN", "CROSS_COLUMN"})

_ASSIGNMENT_SCOPES = frozenset({"SINGLE_COLUMN", "DATASET_LEVEL", "CROSS_COLUMN"})

# ASCII Unit Separator — matches the frozen design's record_ref composite-key
# delimiter choice (Section 2), reused here for the same reason: a
# non-printable delimiter effectively guaranteed not to collide with real
# column-id data.
_CROSS_COLUMN_KEY_DELIMITER = ""


class RulesService:
    def __init__(self, db: Session) -> None:
        self._db = db
        self._audit = AuditingService(db)

    def get_rule(self, rule_id: uuid.UUID) -> Rule:
        rule = self._db.get(Rule, rule_id)
        if rule is None:
            raise RuleNotFoundError(f"Rule {rule_id} not found")
        return rule

    def list_rules(self, *, status: str | None = None, rule_type: str | None = None) -> list[Rule]:
        stmt = select(Rule)
        if status is not None:
            stmt = stmt.where(Rule.status == status)
        if rule_type is not None:
            stmt = stmt.where(Rule.rule_type == rule_type)
        stmt = stmt.order_by(Rule.name)
        return list(self._db.execute(stmt).scalars())

    def create_rule(
        self,
        *,
        actor: User,
        name: str,
        description: str | None,
        category: str | None,
        rule_type: str,
        origin: str,
        definition: dict,
        severity: str,
        error_message_template: str | None,
    ) -> Rule:
        if rule_type not in SUPPORTED_RULE_TYPES:
            raise UnsupportedRuleTypeError(
                f"Unsupported rule_type '{rule_type}'. Supported types: {sorted(SUPPORTED_RULE_TYPES)}"
            )

        rule = Rule(
            name=name,
            description=description,
            category=category,
            rule_type=rule_type,
            origin=origin,
            status="ACTIVE",
            created_by=actor.id,
        )
        self._db.add(rule)
        self._db.flush()

        version = RuleVersion(
            rule_id=rule.id,
            version_number=1,
            definition=definition,
            severity=severity,
            error_message_template=error_message_template,
            is_current=True,
            created_by=actor.id,
        )
        self._db.add(version)
        self._db.flush()

        self._audit.record(
            actor=actor,
            action="rule.created",
            entity_type="RULE",
            entity_id=rule.id,
            after={"name": rule.name, "rule_type": rule.rule_type},
        )
        self._db.commit()
        self._db.refresh(rule)
        return rule

    def update_rule(
        self,
        *,
        actor: User,
        rule_id: uuid.UUID,
        description: str | None,
        category: str | None,
        status: str | None,
    ) -> Rule:
        rule = self.get_rule(rule_id)
        before = {"description": rule.description, "category": rule.category, "status": rule.status}

        if description is not None:
            rule.description = description
        if category is not None:
            rule.category = category
        if status is not None:
            rule.status = status
        rule.updated_at = datetime.now(timezone.utc)

        self._audit.record(
            actor=actor,
            action="rule.updated",
            entity_type="RULE",
            entity_id=rule.id,
            before=before,
            after={"description": rule.description, "category": rule.category, "status": rule.status},
        )
        self._db.commit()
        self._db.refresh(rule)
        return rule

    def list_versions(self, rule_id: uuid.UUID) -> list[RuleVersion]:
        self.get_rule(rule_id)
        return list(
            self._db.execute(
                select(RuleVersion).where(RuleVersion.rule_id == rule_id).order_by(RuleVersion.version_number.desc())
            ).scalars()
        )

    def publish_version(
        self,
        *,
        actor: User,
        rule_id: uuid.UUID,
        definition: dict,
        severity: str,
        error_message_template: str | None,
    ) -> RuleVersion:
        rule = self.get_rule(rule_id)

        current = self._db.execute(
            select(RuleVersion).where(RuleVersion.rule_id == rule.id, RuleVersion.is_current.is_(True))
        ).scalar_one_or_none()
        next_version_number = 1 if current is None else current.version_number + 1

        if current is not None:
            current.is_current = False

        new_version = RuleVersion(
            rule_id=rule.id,
            version_number=next_version_number,
            definition=definition,
            severity=severity,
            error_message_template=error_message_template,
            is_current=True,
            created_by=actor.id,
        )
        self._db.add(new_version)
        self._db.flush()

        self._audit.record(
            actor=actor,
            action="rule.version_published",
            entity_type="RULE",
            entity_id=rule.id,
            after={"version_number": new_version.version_number},
        )
        self._db.commit()
        self._db.refresh(new_version)
        return new_version


class RuleAssignmentService:
    def __init__(self, db: Session) -> None:
        self._db = db
        self._audit = AuditingService(db)

    def get_assignment(self, assignment_id: uuid.UUID) -> RuleAssignment:
        assignment = self._db.get(RuleAssignment, assignment_id)
        if assignment is None:
            raise RuleAssignmentNotFoundError(f"Rule assignment {assignment_id} not found")
        return assignment

    def list_assignments(
        self, *, dataset_id: uuid.UUID | None = None, is_enabled: bool | None = None
    ) -> list[RuleAssignment]:
        stmt = select(RuleAssignment)
        if dataset_id is not None:
            stmt = stmt.where(RuleAssignment.dataset_id == dataset_id)
        if is_enabled is not None:
            stmt = stmt.where(RuleAssignment.is_enabled == is_enabled)
        stmt = stmt.order_by(RuleAssignment.assigned_at.desc())
        return list(self._db.execute(stmt).scalars())

    def _validate_scope_shape(
        self, *, assignment_scope: str, column_id: uuid.UUID | None, column_ids: list[uuid.UUID] | None
    ) -> None:
        if assignment_scope not in _ASSIGNMENT_SCOPES:
            raise InvalidRuleAssignmentScopeError(
                f"assignment_scope must be one of {sorted(_ASSIGNMENT_SCOPES)}, got '{assignment_scope}'"
            )
        if assignment_scope == "SINGLE_COLUMN":
            if column_id is None:
                raise InvalidRuleAssignmentScopeError("SINGLE_COLUMN assignments require column_id")
            if column_ids:
                raise InvalidRuleAssignmentScopeError("SINGLE_COLUMN assignments must not set column_ids")
        elif assignment_scope == "DATASET_LEVEL":
            if column_id is not None or column_ids:
                raise InvalidRuleAssignmentScopeError(
                    "DATASET_LEVEL assignments must not set column_id or column_ids"
                )
        elif assignment_scope == "CROSS_COLUMN":
            if column_id is not None:
                raise InvalidRuleAssignmentScopeError("CROSS_COLUMN assignments must not set column_id")
            if not column_ids or len(column_ids) < 2:
                raise InvalidRuleAssignmentScopeError("CROSS_COLUMN assignments require at least 2 column_ids")

    def create_assignment(
        self,
        *,
        actor: User,
        rule_version_id: uuid.UUID,
        dataset_id: uuid.UUID,
        assignment_scope: str,
        column_id: uuid.UUID | None,
        column_ids: list[uuid.UUID] | None,
        template_id: uuid.UUID | None,
    ) -> RuleAssignment:
        if self._db.get(RuleVersion, rule_version_id) is None:
            raise RuleVersionNotFoundError(f"Rule version {rule_version_id} not found")
        if self._db.get(Dataset, dataset_id) is None:
            raise DatasetNotFoundError(f"Dataset {dataset_id} not found")

        self._validate_scope_shape(assignment_scope=assignment_scope, column_id=column_id, column_ids=column_ids)

        if assignment_scope == "SINGLE_COLUMN":
            valid = self._db.execute(
                select(Column.id).where(Column.id == column_id, Column.dataset_id == dataset_id)
            ).scalar_one_or_none()
            if valid is None:
                raise InvalidRuleAssignmentScopeError(f"Column {column_id} does not belong to dataset {dataset_id}")

        cross_column_key: str | None = None
        if assignment_scope == "CROSS_COLUMN":
            valid_ids = set(
                self._db.execute(
                    select(Column.id).where(Column.id.in_(column_ids), Column.dataset_id == dataset_id)
                ).scalars()
            )
            missing = [str(c) for c in column_ids if c not in valid_ids]
            if missing:
                raise InvalidRuleAssignmentScopeError(
                    f"Column(s) do not belong to dataset {dataset_id}: {', '.join(missing)}"
                )
            # Deterministic, order-independent key: sorted stringified UUIDs
            # joined with the Unit Separator (mirrors record_ref's composite
            # key delimiter choice, Section 2 of the frozen design).
            cross_column_key = _CROSS_COLUMN_KEY_DELIMITER.join(sorted(str(c) for c in column_ids))

        assignment = RuleAssignment(
            rule_version_id=rule_version_id,
            dataset_id=dataset_id,
            assignment_scope=assignment_scope,
            column_id=column_id if assignment_scope == "SINGLE_COLUMN" else None,
            cross_column_key=cross_column_key,
            template_id=template_id,
            is_enabled=True,
            assigned_by=actor.id,
        )
        self._db.add(assignment)

        try:
            self._db.flush()
        except IntegrityError as exc:
            self._db.rollback()
            raise DuplicateRuleAssignmentError(
                "An equivalent rule assignment already exists for this dataset/scope/template"
            ) from exc

        if assignment_scope == "CROSS_COLUMN":
            for ordinal, cid in enumerate(column_ids):
                self._db.add(RuleAssignmentColumn(rule_assignment_id=assignment.id, column_id=cid, ordinal=ordinal))
            self._db.flush()

        self._audit.record(
            actor=actor,
            action="rule_assignment.created",
            entity_type="RULE_ASSIGNMENT",
            entity_id=assignment.id,
            after={"dataset_id": str(dataset_id), "assignment_scope": assignment_scope},
        )
        self._db.commit()
        self._db.refresh(assignment)
        return assignment

    def soft_disable(self, *, actor: User, assignment_id: uuid.UUID) -> RuleAssignment:
        """Sets is_enabled=false. Never a physical delete — preserves
        assignment history, validation history, auditability, and
        references from validation_failures, per the global "no physical
        deletes on business-critical entities" convention."""
        assignment = self.get_assignment(assignment_id)
        assignment.is_enabled = False
        assignment.updated_at = datetime.now(timezone.utc)

        self._audit.record(
            actor=actor,
            action="rule_assignment.removed",
            entity_type="RULE_ASSIGNMENT",
            entity_id=assignment.id,
        )
        self._db.commit()
        self._db.refresh(assignment)
        return assignment
