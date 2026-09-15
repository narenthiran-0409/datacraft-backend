"""Phase 4.8 — staged revalidation service.

Computes, ON DEMAND, whether a StagingRecord's row_snapshot actually
satisfies the dataset's currently-enabled rules — never persisted.

Why no persistence / no migration: staged revalidation status is a pure
function of two things already durably stored: staging_records.row_snapshot
(written once, at staging time, never mutated afterward) and the
targeted RuleAssignment/RuleVersion's definition (each RuleAssignment
locks a specific rule_version_id at assignment time, also immutable).
Recomputing from these two immutable facts always yields the identical
answer, at any time, in any process — there is nothing to persist that
isn't already durably recorded elsewhere, and persisting a redundant
copy would only create a staleness risk (e.g. if a rule's definition
were edited after the fact — it isn't; RuleVersion rows are themselves
immutable) for zero benefit. See revalidate_staging_record's docstring
below for the exact scope decision.

Never queries the live source, never invokes AI, never writes anything.
"""
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import StagingRecordNotFoundError, StagingRunNotFoundError
from app.db.models import Column, Rule, RuleAssignment, RuleAssignmentColumn, RuleVersion, StagingRecord, StagingRun
from app.modules.validation.staged_revalidation import StagedRuleRevalidation, revalidate_row_against_rule


@dataclass(frozen=True)
class StagedRuleRevalidationReport(StagedRuleRevalidation):
    rule_assignment_id: uuid.UUID
    rule_id: uuid.UUID


class StagedRevalidationService:
    def __init__(self, db: Session) -> None:
        self._db = db

    def revalidate_staging_record(self, staging_record_id: uuid.UUID) -> list[StagedRuleRevalidationReport]:
        """Scope decision: every currently-ENABLED RuleAssignment for the
        record's dataset is evaluated against the staged snapshot — not
        merely the one rule that originally produced the Issue(s) this
        record was corrected for. This is deliberately the smallest scope
        that still catches "correction A fixes rule X but the same edited
        value now breaks rule Y" (both assigned to the same or a
        different column), while staying row-level (never a full-dataset
        re-scan): exactly the balance the Phase 4.8 spec asks for.
        Disabled/unassigned rules are never evaluated — same
        RuleAssignment.is_enabled.is_(True) filter the real validation
        run itself uses (app.modules.validation.tasks)."""
        staging_record = self._db.get(StagingRecord, staging_record_id)
        if staging_record is None:
            raise StagingRecordNotFoundError(f"Staging record {staging_record_id} not found")
        staging_run = self._db.get(StagingRun, staging_record.staging_run_id)
        if staging_run is None:
            raise StagingRunNotFoundError(f"Staging run {staging_record.staging_run_id} not found")

        assignments = list(
            self._db.execute(
                select(RuleAssignment).where(
                    RuleAssignment.dataset_id == staging_run.dataset_id, RuleAssignment.is_enabled.is_(True)
                )
            ).scalars()
        )
        active_columns = self._db.execute(
            select(Column).where(Column.dataset_id == staging_run.dataset_id, Column.is_active.is_(True))
        ).scalars().all()
        columns_by_id = {c.id: c for c in active_columns}

        reports: list[StagedRuleRevalidationReport] = []
        for assignment in assignments:
            rule_version = self._db.get(RuleVersion, assignment.rule_version_id)
            rule = self._db.get(Rule, rule_version.rule_id)
            definition = rule_version.definition or {}

            column_name = None
            column_names_in_order = None
            if rule.rule_type == "CROSS_COLUMN":
                rac_rows = self._db.execute(
                    select(RuleAssignmentColumn)
                    .where(RuleAssignmentColumn.rule_assignment_id == assignment.id)
                    .order_by(RuleAssignmentColumn.ordinal)
                ).scalars().all()
                column_names_in_order = [
                    columns_by_id[rac.column_id].name for rac in rac_rows if rac.column_id in columns_by_id
                ]
            else:
                column = columns_by_id.get(assignment.column_id) if assignment.column_id else None
                column_name = column.name if column is not None else None

            result = revalidate_row_against_rule(
                row_snapshot=staging_record.row_snapshot, rule_type=rule.rule_type, definition=definition,
                column_name=column_name, column_names_in_order=column_names_in_order,
            )
            reports.append(
                StagedRuleRevalidationReport(
                    rule_type=result.rule_type, column_name=result.column_name, status=result.status,
                    reason=result.reason, checked_value=result.checked_value,
                    rule_assignment_id=assignment.id, rule_id=rule.id,
                )
            )
        return reports
