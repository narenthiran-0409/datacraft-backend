"""Response schemas for all 5 approved Reports endpoints. Every field here
is an identifier, count, rate, timestamp, or aggregate/summary number.
None of these models has, or may ever gain, a field capable of holding a
row-level value (row_snapshot, failed_value, original_value, final_value,
corrected_fields, credentials, or any other raw payload) — this is a
structural property of the schemas themselves, not just a convention."""
import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel


class QualityTrendPoint(BaseModel):
    validation_run_id: uuid.UUID
    dataset_id: uuid.UUID
    created_at: datetime
    quality_score: Decimal


class QualityTrendResponse(BaseModel):
    points: list[QualityTrendPoint]


class RuleEffectivenessRow(BaseModel):
    rule_id: uuid.UUID
    rule_name: str
    rule_type: str
    failure_count: int
    failure_rate: Decimal | None
    severity_breakdown: dict[str, int]


class RuleEffectivenessResponse(BaseModel):
    rules: list[RuleEffectivenessRow]


class DatasetQualityRow(BaseModel):
    dataset_id: uuid.UUID
    dataset_name: str
    latest_quality_score: Decimal | None
    latest_validation_run_id: uuid.UUID | None
    latest_validated_at: datetime | None


class QualityByDatasetResponse(BaseModel):
    datasets: list[DatasetQualityRow]


class ReviewerPerformanceRow(BaseModel):
    reviewer_id: uuid.UUID
    decision_count: int
    avg_latency_seconds: float | None


class ReviewPerformanceResponse(BaseModel):
    reviewers: list[ReviewerPerformanceRow]


class ApprovalMetricsResponse(BaseModel):
    total_requests: int
    approved_count: int
    rejected_count: int
    partially_approved_count: int
    pending_count: int
    approval_rate: Decimal | None
    avg_decision_latency_seconds: float | None
