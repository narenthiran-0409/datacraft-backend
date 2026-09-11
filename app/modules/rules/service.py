import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import (
    DatasetNotFoundError,
    DuplicateRuleAssignmentError,
    InvalidRuleAssignmentScopeError,
    InvalidRuleReviewTransitionError,
    RuleAssignmentNotFoundError,
    RuleNotFoundError,
    RulePromotionNotSupportedError,
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

# Origins whose output must never become active without an explicit human
# review — enforced structurally in create_rule() below (every caller,
# present or future, gets this for free) rather than trusted to each
# caller to remember. Matches every other AI-adjacent feature in this
# system's standing rule: model/heuristic output is always PROPOSED,
# never authoritative, until a human with the right permission acts on it.
_REVIEW_REQUIRED_ORIGINS = frozenset({"AI_RECOMMENDED", "PATTERN_DETECTED"})

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

        # Structural guarantee, not a convention any individual caller has
        # to remember: any rule whose origin is AI_RECOMMENDED or
        # PATTERN_DETECTED starts PENDING_REVIEW no matter what — a rule
        # only becomes ACTIVE via promote_rule(), which requires
        # rules.manage. This is the single choke point every rule
        # creation path (this API, the pattern detector, the AI fallback)
        # goes through, so the "nothing automated becomes active" rule
        # can't be bypassed by a future caller forgetting to set status.
        initial_status = "PENDING_REVIEW" if origin in _REVIEW_REQUIRED_ORIGINS else "ACTIVE"

        rule = Rule(
            name=name,
            description=description,
            category=category,
            rule_type=rule_type,
            origin=origin,
            status=initial_status,
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

    def _resolve_promotion_assignment(
        self, rule: Rule, rule_version: RuleVersion
    ) -> tuple[str, uuid.UUID | None, uuid.UUID]:
        """Derives (assignment_scope, column_id, dataset_id) for the
        rule_assignment promote_rule() creates, from the detection
        metadata RuleDetectionService/AISuggestionService actually write
        into rule_version.definition["_detected_for"] — verified directly
        against that code, not assumed: {dataset_id, column_id,
        column_name, confidence[, ai_suggestion_id]}. Every detection this
        codebase currently produces stores exactly one column_id — there
        is no dataset-level or multi-column detection path today, so
        scope is derived from rule_type against the validation engine's
        real per-type semantics (app.modules.validation.engine), never
        from a stored scope field (none exists):

          - COMPLETENESS/UNIQUENESS/RANGE/PATTERN: their evaluators each
            take a single column_name -> SINGLE_COLUMN, using the stored
            column_id.
          - DUPLICATE: evaluate_duplicate takes no column at all (it's a
            whole-row check) -> DATASET_LEVEL; any stored column_id is
            not used. (A DATASET_LEVEL COMPLETENESS/UNIQUENESS assignment
            would still be silently skipped by _do_evaluation() — a
            separate, still-open bug from an earlier investigation, not
            fixed here — but that code path is never reached from this
            method: COMPLETENESS/UNIQUENESS always resolve to
            SINGLE_COLUMN above, never DATASET_LEVEL.)
          - CROSS_COLUMN: evaluate_cross_column needs 2+ specific columns
            via rule_assignment_columns, which detection never captures
            (only one column_id is ever stored) — refused rather than
            guessing a shape that wouldn't match what the evaluator
            actually needs.

        Also covers a rule that reached PENDING_REVIEW without ever going
        through detection at all (e.g. an admin PATCHing status by hand
        via the generic update_rule() endpoint, which doesn't validate
        status transitions the way promote/dismiss do) — such a rule has
        no _detected_for metadata, so this raises rather than promoting
        it into an unassigned, silently-inert ACTIVE rule (the original
        bug, reintroduced via a different path).
        """
        detected_for = (rule_version.definition or {}).get("_detected_for")
        if not detected_for or not detected_for.get("dataset_id"):
            raise RulePromotionNotSupportedError(
                f"Rule {rule.id} has no recorded detection metadata (_detected_for) to derive its "
                "assignment from — it wasn't created by the rule detector, so promote_rule() has no "
                "way to know what dataset/column it should apply to. Create its assignment manually "
                "via POST /rule-assignments instead."
            )
        dataset_id = uuid.UUID(detected_for["dataset_id"])

        if rule.rule_type == "DUPLICATE":
            return "DATASET_LEVEL", None, dataset_id
        if rule.rule_type == "CROSS_COLUMN":
            raise RulePromotionNotSupportedError(
                f"Rule {rule.id} is CROSS_COLUMN, which needs 2+ specific columns "
                "(rule_assignment_columns) — detection only ever records one column_id, so "
                "promote_rule() cannot honestly reconstruct a valid CROSS_COLUMN assignment for it. "
                "Create its assignment manually via POST /rule-assignments instead."
            )

        column_id_str = detected_for.get("column_id")
        if not column_id_str:
            raise RulePromotionNotSupportedError(
                f"Rule {rule.id} (rule_type={rule.rule_type}) has no column_id recorded in its "
                "detection metadata — cannot create a SINGLE_COLUMN assignment for it."
            )
        return "SINGLE_COLUMN", uuid.UUID(column_id_str), dataset_id

    def promote_rule(self, *, actor: User, rule_id: uuid.UUID) -> Rule:
        """The only path a PENDING_REVIEW rule (pattern-detected or
        AI-recommended) can ever take to become ACTIVE — always an
        explicit human action, never automatic. Requires the rule to
        currently be PENDING_REVIEW: promoting an already-ACTIVE or
        DISABLED rule is rejected rather than silently accepted, so this
        can't be used as a backdoor status-setter for rules outside the
        review workflow (RuleUpdateRequest/update_rule already covers
        general status changes for those).

        BUG FIX: promoting used to only flip rules.status to ACTIVE and
        stop there. Validation's assignment-resolution query
        (app.modules.validation.tasks) never checks rules.status at
        all — only rule_assignments.is_enabled — so a promoted rule with
        no assignment had, and could only ever have had, zero effect on
        any real validation run: "promoted" was a status label with no
        actual behavior behind it. This method now creates the correct
        rule_assignment (see _resolve_promotion_assignment) in the same
        transaction as the status flip: both are flushed together and
        committed together, so a failure creating the assignment (e.g. a
        genuine duplicate) leaves the rule PENDING_REVIEW rather than
        landing in the same "ACTIVE but inert" state this fix exists to
        eliminate — and, just as importantly, success never leaves an
        assignment pointing at a rule that's still PENDING_REVIEW
        (validation not checking rules.status means an enabled assignment
        alone is enough to be evaluated, promoted or not)."""
        rule = self.get_rule(rule_id)
        if rule.status != "PENDING_REVIEW":
            raise InvalidRuleReviewTransitionError(
                f"Rule {rule_id} is {rule.status!r}, not PENDING_REVIEW — nothing to promote"
            )

        rule_version = self._db.execute(
            select(RuleVersion).where(RuleVersion.rule_id == rule.id, RuleVersion.is_current.is_(True))
        ).scalar_one()
        assignment_scope, column_id, dataset_id = self._resolve_promotion_assignment(rule, rule_version)

        if self._db.get(Dataset, dataset_id) is None:
            raise DatasetNotFoundError(f"Dataset {dataset_id} not found")
        if column_id is not None:
            valid_column = self._db.execute(
                select(Column.id).where(Column.id == column_id, Column.dataset_id == dataset_id)
            ).scalar_one_or_none()
            if valid_column is None:
                raise InvalidRuleAssignmentScopeError(f"Column {column_id} does not belong to dataset {dataset_id}")

        rule.status = "ACTIVE"
        rule.updated_at = datetime.now(timezone.utc)

        assignment = RuleAssignment(
            rule_version_id=rule_version.id, dataset_id=dataset_id, assignment_scope=assignment_scope,
            column_id=column_id, cross_column_key=None, template_id=None, is_enabled=True, assigned_by=actor.id,
        )
        self._db.add(assignment)

        try:
            self._db.flush()
        except IntegrityError as exc:
            self._db.rollback()
            raise DuplicateRuleAssignmentError(
                "An equivalent rule assignment already exists for this dataset/scope/template"
            ) from exc

        self._audit.record(
            actor=actor,
            action="rule.promoted",
            entity_type="RULE",
            entity_id=rule.id,
            before={"status": "PENDING_REVIEW"},
            after={"status": "ACTIVE"},
        )
        self._audit.record(
            actor=actor,
            action="rule_assignment.created",
            entity_type="RULE_ASSIGNMENT",
            entity_id=assignment.id,
            after={"dataset_id": str(dataset_id), "assignment_scope": assignment_scope},
        )
        self._db.commit()
        self._db.refresh(rule)
        return rule

    def dismiss_rule(self, *, actor: User, rule_id: uuid.UUID) -> Rule:
        """Rejects a PENDING_REVIEW rule — sets it DISABLED rather than
        deleting it, matching this project's standing no-physical-deletes-
        on-business-critical-entities convention (RuleAssignmentService.
        soft_disable follows the same pattern). Same PENDING_REVIEW
        precondition as promote_rule, for the same reason."""
        rule = self.get_rule(rule_id)
        if rule.status != "PENDING_REVIEW":
            raise InvalidRuleReviewTransitionError(
                f"Rule {rule_id} is {rule.status!r}, not PENDING_REVIEW — nothing to dismiss"
            )

        rule.status = "DISABLED"
        rule.updated_at = datetime.now(timezone.utc)

        self._audit.record(
            actor=actor,
            action="rule.dismissed",
            entity_type="RULE",
            entity_id=rule.id,
            before={"status": "PENDING_REVIEW"},
            after={"status": "DISABLED"},
        )
        self._db.commit()
        self._db.refresh(rule)
        return rule


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
