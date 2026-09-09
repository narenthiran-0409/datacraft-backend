"""Reports: a strictly READ-ONLY aggregation module. Every method here
computes its result LIVE, at query time, directly from Phase 1-10's
authoritative tables — never from LineageService, never from any cached/
persisted aggregation, and never by writing to any table (Reports owns
none). No method here mutates any row anywhere.

quality-by-dataset deliberately does NOT read datasets.last_quality_score /
datasets.last_validated_at, even though inspection confirmed both ARE
populated live by Phase 5 — Decision 2 (Phase 11 design) requires
computing this live from validation_runs directly instead, leaving those
two columns dormant from this module's perspective."""
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    ApprovalRequest,
    Connection,
    Correction,
    Dataset,
    Issue,
    Rule,
    RuleAssignment,
    RuleVersion,
    Schema,
    ValidationFailure,
    ValidationRun,
)


class ReportsService:
    def __init__(self, db: Session) -> None:
        self._db = db

    # --- 1. quality-trend ------------------------------------------------

    def get_quality_trend(
        self, *, dataset_id: uuid.UUID | None, from_dt: datetime, to_dt: datetime
    ) -> list[dict]:
        stmt = select(
            ValidationRun.id, ValidationRun.dataset_id, ValidationRun.created_at, ValidationRun.quality_score
        ).where(
            ValidationRun.created_at >= from_dt,
            ValidationRun.created_at <= to_dt,
            ValidationRun.quality_score.isnot(None),  # NULL (zero-row) runs excluded, never fabricated
        )
        if dataset_id is not None:
            stmt = stmt.where(ValidationRun.dataset_id == dataset_id)
        stmt = stmt.order_by(ValidationRun.created_at)

        rows = self._db.execute(stmt).all()
        return [
            {
                "validation_run_id": r.id, "dataset_id": r.dataset_id,
                "created_at": r.created_at, "quality_score": r.quality_score,
            }
            for r in rows
        ]

    # --- 2. rule-effectiveness --------------------------------------------

    def get_rule_effectiveness(
        self, *, dataset_id: uuid.UUID | None, from_dt: datetime, to_dt: datetime
    ) -> list[dict]:
        stmt = (
            select(
                Rule.id, Rule.name, Rule.rule_type, ValidationFailure.severity, func.count().label("count")
            )
            .join(RuleAssignment, RuleAssignment.id == ValidationFailure.rule_assignment_id)
            .join(RuleVersion, RuleVersion.id == RuleAssignment.rule_version_id)
            .join(Rule, Rule.id == RuleVersion.rule_id)
            .join(ValidationRun, ValidationRun.id == ValidationFailure.validation_run_id)
            .where(ValidationRun.created_at >= from_dt, ValidationRun.created_at <= to_dt)
            .group_by(Rule.id, Rule.name, Rule.rule_type, ValidationFailure.severity)
        )
        if dataset_id is not None:
            stmt = stmt.where(ValidationRun.dataset_id == dataset_id)

        rows = self._db.execute(stmt).all()

        by_rule: dict[uuid.UUID, dict] = {}
        total_failures = 0
        for r in rows:
            total_failures += r.count
            entry = by_rule.setdefault(
                r.id, {"rule_id": r.id, "rule_name": r.name, "rule_type": r.rule_type,
                       "failure_count": 0, "severity_breakdown": {}}
            )
            entry["failure_count"] += r.count
            entry["severity_breakdown"][r.severity] = entry["severity_breakdown"].get(r.severity, 0) + r.count

        results = []
        for entry in by_rule.values():
            entry["failure_rate"] = (
                Decimal(entry["failure_count"]) / Decimal(total_failures) if total_failures > 0 else None
            )
            results.append(entry)
        results.sort(key=lambda e: e["failure_count"], reverse=True)
        return results

    # --- 3. quality-by-dataset ---------------------------------------------

    def get_quality_by_dataset(self, *, data_source_id: uuid.UUID | None) -> list[dict]:
        dataset_stmt = (
            select(Dataset.id, Dataset.name)
            .join(Schema, Schema.id == Dataset.schema_id)
            .join(Connection, Connection.id == Schema.connection_id)
            .where(Dataset.is_active.is_(True))
        )
        if data_source_id is not None:
            dataset_stmt = dataset_stmt.where(Connection.data_source_id == data_source_id)
        datasets = self._db.execute(dataset_stmt).all()
        if not datasets:
            return []
        dataset_ids = [d.id for d in datasets]

        rn = func.row_number().over(
            partition_by=ValidationRun.dataset_id, order_by=ValidationRun.created_at.desc()
        ).label("rn")
        ranked = (
            select(
                ValidationRun.id, ValidationRun.dataset_id, ValidationRun.created_at,
                ValidationRun.quality_score, rn,
            )
            .where(ValidationRun.dataset_id.in_(dataset_ids), ValidationRun.quality_score.isnot(None))
            .subquery()
        )
        latest_rows = self._db.execute(
            select(ranked.c.id, ranked.c.dataset_id, ranked.c.created_at, ranked.c.quality_score)
            .where(ranked.c.rn == 1)
        ).all()
        latest_by_dataset = {r.dataset_id: r for r in latest_rows}

        results = []
        for d in datasets:
            latest = latest_by_dataset.get(d.id)
            results.append(
                {
                    "dataset_id": d.id, "dataset_name": d.name,
                    "latest_quality_score": latest.quality_score if latest else None,
                    "latest_validation_run_id": latest.id if latest else None,
                    "latest_validated_at": latest.created_at if latest else None,
                }
            )
        return results

    # --- 4. review-performance ---------------------------------------------

    def get_review_performance(self, *, from_dt: datetime, to_dt: datetime) -> list[dict]:
        stmt = (
            select(Correction.decided_by, Correction.decided_at, Issue.created_at.label("issue_created_at"))
            .join(Issue, Issue.id == Correction.issue_id)
            .where(
                Correction.decided_by.isnot(None), Correction.decided_at.isnot(None),
                Correction.decided_at >= from_dt, Correction.decided_at <= to_dt,
            )
        )
        rows = self._db.execute(stmt).all()

        by_reviewer: dict[uuid.UUID, dict] = {}
        for r in rows:
            entry = by_reviewer.setdefault(
                r.decided_by, {"reviewer_id": r.decided_by, "decision_count": 0, "_latencies": []}
            )
            entry["decision_count"] += 1
            latency_seconds = (r.decided_at - r.issue_created_at).total_seconds()
            entry["_latencies"].append(latency_seconds)

        results = []
        for entry in by_reviewer.values():
            latencies = entry.pop("_latencies")
            entry["avg_latency_seconds"] = sum(latencies) / len(latencies) if latencies else None
            results.append(entry)
        results.sort(key=lambda e: e["decision_count"], reverse=True)
        return results

    # --- 5. approval-metrics -------------------------------------------------

    def get_approval_metrics(self, *, from_dt: datetime, to_dt: datetime) -> dict:
        stmt = select(ApprovalRequest.status, ApprovalRequest.requested_at, ApprovalRequest.decided_at).where(
            ApprovalRequest.requested_at >= from_dt, ApprovalRequest.requested_at <= to_dt
        )
        rows = self._db.execute(stmt).all()

        total_requests = len(rows)
        counts = {"APPROVED": 0, "REJECTED": 0, "PARTIALLY_APPROVED": 0, "PENDING": 0}
        latencies = []
        for r in rows:
            counts[r.status] = counts.get(r.status, 0) + 1
            if r.decided_at is not None:
                latencies.append((r.decided_at - r.requested_at).total_seconds())

        approval_rate = Decimal(counts["APPROVED"]) / Decimal(total_requests) if total_requests > 0 else None
        return {
            "total_requests": total_requests,
            "approved_count": counts["APPROVED"],
            "rejected_count": counts["REJECTED"],
            "partially_approved_count": counts["PARTIALLY_APPROVED"],
            "pending_count": counts["PENDING"],
            "approval_rate": approval_rate,
            "avg_decision_latency_seconds": sum(latencies) / len(latencies) if latencies else None,
        }
