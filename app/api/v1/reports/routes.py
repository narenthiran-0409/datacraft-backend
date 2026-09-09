import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import require_permission
from app.core.exceptions import InvalidDateRangeError
from app.db.models import User
from app.modules.reports.schemas import (
    ApprovalMetricsResponse,
    DatasetQualityRow,
    QualityByDatasetResponse,
    QualityTrendPoint,
    QualityTrendResponse,
    ReviewerPerformanceRow,
    ReviewPerformanceResponse,
    RuleEffectivenessResponse,
    RuleEffectivenessRow,
)
from app.modules.reports.service import ReportsService

router = APIRouter(prefix="/reports", tags=["reports"])


def get_reports_service(db: Session = Depends(get_db)) -> ReportsService:
    return ReportsService(db)


def _validate_range(from_dt: datetime, to_dt: datetime) -> None:
    if from_dt > to_dt:
        raise InvalidDateRangeError("'from' must not be after 'to'")


@router.get("/quality-trend", response_model=QualityTrendResponse)
def get_quality_trend(
    from_: datetime = Query(alias="from"),
    to: datetime = Query(),
    dataset_id: uuid.UUID | None = Query(default=None),
    service: ReportsService = Depends(get_reports_service),
    _: User = Depends(require_permission("reports.read")),
) -> QualityTrendResponse:
    _validate_range(from_, to)
    points = service.get_quality_trend(dataset_id=dataset_id, from_dt=from_, to_dt=to)
    return QualityTrendResponse(points=[QualityTrendPoint(**p) for p in points])


@router.get("/rule-effectiveness", response_model=RuleEffectivenessResponse)
def get_rule_effectiveness(
    from_: datetime = Query(alias="from"),
    to: datetime = Query(),
    dataset_id: uuid.UUID | None = Query(default=None),
    service: ReportsService = Depends(get_reports_service),
    _: User = Depends(require_permission("reports.read")),
) -> RuleEffectivenessResponse:
    _validate_range(from_, to)
    rules = service.get_rule_effectiveness(dataset_id=dataset_id, from_dt=from_, to_dt=to)
    return RuleEffectivenessResponse(rules=[RuleEffectivenessRow(**r) for r in rules])


@router.get("/quality-by-dataset", response_model=QualityByDatasetResponse)
def get_quality_by_dataset(
    data_source_id: uuid.UUID | None = Query(default=None),
    service: ReportsService = Depends(get_reports_service),
    _: User = Depends(require_permission("reports.read")),
) -> QualityByDatasetResponse:
    datasets = service.get_quality_by_dataset(data_source_id=data_source_id)
    return QualityByDatasetResponse(datasets=[DatasetQualityRow(**d) for d in datasets])


@router.get("/review-performance", response_model=ReviewPerformanceResponse)
def get_review_performance(
    from_: datetime = Query(alias="from"),
    to: datetime = Query(),
    service: ReportsService = Depends(get_reports_service),
    _: User = Depends(require_permission("reports.read")),
) -> ReviewPerformanceResponse:
    _validate_range(from_, to)
    reviewers = service.get_review_performance(from_dt=from_, to_dt=to)
    return ReviewPerformanceResponse(reviewers=[ReviewerPerformanceRow(**r) for r in reviewers])


@router.get("/approval-metrics", response_model=ApprovalMetricsResponse)
def get_approval_metrics(
    from_: datetime = Query(alias="from"),
    to: datetime = Query(),
    service: ReportsService = Depends(get_reports_service),
    _: User = Depends(require_permission("reports.read")),
) -> ApprovalMetricsResponse:
    _validate_range(from_, to)
    metrics = service.get_approval_metrics(from_dt=from_, to_dt=to)
    return ApprovalMetricsResponse(**metrics)
