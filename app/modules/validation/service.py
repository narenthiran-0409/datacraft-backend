import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.exceptions import (
    DatasetNotActiveError,
    DatasetNotFoundError,
    ValidationAlreadyRunningError,
    ValidationRunNotFoundError,
)
from app.db.models import (
    Column,
    Dataset,
    Job,
    Rule,
    RuleAssignment,
    RuleVersion,
    User,
    ValidationFailure,
    ValidationResult,
    ValidationRun,
)
from app.modules.lineage.service import LineageService


class ValidationService:
    def __init__(self, db: Session) -> None:
        self._db = db
        self._lineage = LineageService(db)

    def get_validation_run(self, validation_run_id: uuid.UUID) -> ValidationRun:
        run = self._db.get(ValidationRun, validation_run_id)
        if run is None:
            raise ValidationRunNotFoundError(f"Validation run {validation_run_id} not found")
        return run

    def list_validation_runs(self, *, dataset_id: uuid.UUID | None, status: str | None) -> list[ValidationRun]:
        stmt = select(ValidationRun)
        if dataset_id is not None:
            stmt = stmt.where(ValidationRun.dataset_id == dataset_id)
        if status is not None:
            stmt = stmt.where(ValidationRun.status == status)
        stmt = stmt.order_by(ValidationRun.created_at.desc())
        return list(self._db.execute(stmt).scalars())

    def get_latest_completed_run(self, dataset_id: uuid.UUID) -> ValidationRun | None:
        return self._db.execute(
            select(ValidationRun)
            .where(ValidationRun.dataset_id == dataset_id, ValidationRun.status == "COMPLETED")
            .order_by(ValidationRun.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()

    def list_failures(
        self,
        *,
        validation_run_id: uuid.UUID,
        severity: str | None,
        column_id: uuid.UUID | None,
        rule_assignment_id: uuid.UUID | None,
        page: int,
        page_size: int,
    ) -> tuple[list[dict], int]:
        """Row-level failure detail for a run, joined server-side against
        validation_results (record_ref/row_index), rule_assignments ->
        rule_versions -> rules (rule name/type), and columns (column name)
        — the frontend gets names, not bare IDs to resolve itself.

        Filters only exist for columns validation_failures actually has
        (severity, column_id, rule_assignment_id). There is no `status`
        filter: status is a validation_results-level concept (PASSED/
        WARNING/FAILED per row), not a validation_failures column — every
        failure row is implicitly tied to a non-PASSED result already.
        """
        self.get_validation_run(validation_run_id)  # 404 if the run itself doesn't exist

        conditions = [ValidationFailure.validation_run_id == validation_run_id]
        if severity is not None:
            conditions.append(ValidationFailure.severity == severity)
        if column_id is not None:
            conditions.append(ValidationFailure.column_id == column_id)
        if rule_assignment_id is not None:
            conditions.append(ValidationFailure.rule_assignment_id == rule_assignment_id)

        count_stmt = select(func.count()).select_from(ValidationFailure).where(*conditions)
        total = self._db.execute(count_stmt).scalar_one()

        stmt = (
            select(
                ValidationFailure,
                ValidationResult.record_ref,
                ValidationResult.row_index,
                Rule.id,
                Rule.name,
                Rule.rule_type,
                RuleAssignment.assignment_scope,
                Column.name,
            )
            .join(ValidationResult, ValidationResult.id == ValidationFailure.validation_result_id)
            .join(RuleAssignment, RuleAssignment.id == ValidationFailure.rule_assignment_id)
            .join(RuleVersion, RuleVersion.id == RuleAssignment.rule_version_id)
            .join(Rule, Rule.id == RuleVersion.rule_id)
            .outerjoin(Column, Column.id == ValidationFailure.column_id)
            .where(*conditions)
            .order_by(ValidationFailure.created_at)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        rows = self._db.execute(stmt).all()
        items = [
            {
                "id": failure.id,
                "validation_run_id": failure.validation_run_id,
                "record_ref": record_ref,
                "row_index": row_index,
                "rule_assignment_id": failure.rule_assignment_id,
                "rule_id": rule_id,
                "rule_name": rule_name,
                "rule_type": rule_type,
                "assignment_scope": assignment_scope,
                "column_id": failure.column_id,
                "column_name": column_name,
                "severity": failure.severity,
                "failed_value": failure.failed_value,
                "expected_value": failure.expected_value,
                "reason": failure.reason,
                "created_at": failure.created_at,
            }
            for failure, record_ref, row_index, rule_id, rule_name, rule_type, assignment_scope, column_name in rows
        ]
        return items, total

    def _get_active_dataset(self, dataset_id: uuid.UUID) -> Dataset:
        dataset = self._db.get(Dataset, dataset_id)
        if dataset is None:
            raise DatasetNotFoundError(f"Dataset {dataset_id} not found")
        if not dataset.is_active:
            raise DatasetNotActiveError(f"Dataset {dataset_id} is not active")
        return dataset

    def start_validation(
        self, *, actor: User, dataset_id: uuid.UUID, template_id: uuid.UUID | None
    ) -> tuple[ValidationRun, Job]:
        """Implements the approved CREATED -> QUEUED sequence exactly:
        1. Create validation_run as CREATED.
        2. Resolve applicable rule assignments (the "required setup" step).
        3. Create the linked job.
        4. Transition validation_run to QUEUED.
        All in one transaction, committed once at the end — a caller that
        never sees a returned run/job also never sees a half-done CREATED
        row with no job.
        """
        dataset = self._get_active_dataset(dataset_id)

        existing = self._db.execute(
            select(Job).where(
                Job.job_type == "VALIDATION_RUN",
                Job.entity_type == "DATASET",
                Job.entity_id == dataset_id,
                Job.status.in_(("QUEUED", "RUNNING")),
            )
        ).scalars().first()
        if existing is not None:
            raise ValidationAlreadyRunningError(
                f"A validation job ({existing.id}) is already {existing.status} for this dataset"
            )

        # Step 1: CREATED.
        validation_run = ValidationRun(
            dataset_id=dataset.id,
            template_id=template_id,
            status="CREATED",
            triggered_by=actor.id,
        )
        self._db.add(validation_run)
        self._db.flush()

        # Step 2: resolve applicable, enabled rule assignments for this
        # dataset — the "required setup/assignment resolution" step. Not
        # gated on a non-empty result: a dataset with zero enabled
        # assignments still produces a valid (trivially-passing) run, same
        # spirit as the zero-row-dataset handling in the task. Resolution
        # happens again at task execution time against the live table,
        # since assignments could change between enqueue and worker pickup.
        self._db.execute(
            select(RuleAssignment.id).where(
                RuleAssignment.dataset_id == dataset.id, RuleAssignment.is_enabled.is_(True)
            )
        ).scalars().all()

        # Step 3: create the linked job.
        job = Job(
            job_type="VALIDATION_RUN",
            entity_type="DATASET",
            entity_id=dataset.id,
            created_by=actor.id,
        )
        self._db.add(job)
        self._db.flush()
        validation_run.job_id = job.id

        # Step 4: transition to QUEUED.
        validation_run.status = "QUEUED"
        validation_run.updated_at = datetime.now(timezone.utc)

        # Phase 10 touch point 3 (additive-only): DATASET -> VALIDATION_RUN,
        # written regardless of eventual outcome, in this same transaction.
        self._lineage.record_edge("DATASET", dataset.id, "VALIDATION_RUN", validation_run.id, "VALIDATED_BY")

        self._db.commit()
        self._db.refresh(validation_run)
        self._db.refresh(job)
        return validation_run, job
