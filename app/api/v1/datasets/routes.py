import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.v1.datasets.schemas import (
    BusinessKeyCandidateResponse,
    BusinessKeyConfirmRequest,
    BusinessKeyDiscoveryResponse,
    BusinessKeyRejectedColumnResponse,
    ColumnResponse,
    DatasetListResponse,
    DatasetPatchRequest,
    DatasetPreviewResponse,
    DatasetResponse,
    KeyColumnsRequest,
    SchemaResponse,
)
from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import require_permission
from app.core.redis_client import get_redis_client
from app.db.models import User
from app.modules.connections.credential_vault import CredentialVaultClient, LocalRedisVaultClient
from app.modules.datasets.business_key_service import BusinessKeyService
from app.modules.datasets.key_service import DatasetKeyService
from app.modules.datasets.preview_service import PREVIEW_DEFAULT_ROWS, PreviewService
from app.modules.datasets.service import DatasetService

router = APIRouter(tags=["datasets"])


def get_dataset_service(db: Session = Depends(get_db)) -> DatasetService:
    return DatasetService(db)


def get_dataset_key_service(db: Session = Depends(get_db)) -> DatasetKeyService:
    return DatasetKeyService(db)


def get_vault_client() -> CredentialVaultClient:
    return LocalRedisVaultClient(get_redis_client(), settings.VAULT_LOCAL_ENCRYPTION_KEY)


def get_preview_service(db: Session = Depends(get_db)) -> PreviewService:
    return PreviewService(db, get_vault_client(), get_redis_client())


def get_business_key_service(db: Session = Depends(get_db)) -> BusinessKeyService:
    return BusinessKeyService(db, get_vault_client(), get_redis_client())


@router.get("/schemas", response_model=list[SchemaResponse])
def list_schemas(
    connection_id: uuid.UUID = Query(...),
    service: DatasetService = Depends(get_dataset_service),
    _: User = Depends(require_permission("metadata.read")),
) -> list[SchemaResponse]:
    return [SchemaResponse.model_validate(s) for s in service.list_schemas(connection_id)]


@router.get("/datasets", response_model=DatasetListResponse)
def list_datasets(
    schema_id: uuid.UUID | None = Query(default=None),
    search: str | None = Query(default=None),
    is_active: bool | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    service: DatasetService = Depends(get_dataset_service),
    _: User = Depends(require_permission("metadata.read")),
) -> DatasetListResponse:
    items, total = service.list_datasets(
        schema_id=schema_id, search=search, is_active=is_active, page=page, page_size=page_size
    )
    return DatasetListResponse(
        items=[DatasetResponse.model_validate(d) for d in items], total=total, page=page, page_size=page_size
    )


@router.get("/datasets/{dataset_id}", response_model=DatasetResponse)
def get_dataset(
    dataset_id: uuid.UUID,
    service: DatasetService = Depends(get_dataset_service),
    _: User = Depends(require_permission("metadata.read")),
) -> DatasetResponse:
    """A plain, fast Postgres read only. Never calls any source-database
    provider, and never includes foreign-key data — foreign keys exist only
    in discovery audit-event metadata this phase."""
    return DatasetResponse.model_validate(service.get_dataset(dataset_id))


@router.get("/datasets/{dataset_id}/columns", response_model=list[ColumnResponse])
def get_dataset_columns(
    dataset_id: uuid.UUID,
    service: DatasetService = Depends(get_dataset_service),
    _: User = Depends(require_permission("metadata.read")),
) -> list[ColumnResponse]:
    return [ColumnResponse.model_validate(c) for c in service.get_columns(dataset_id)]


@router.get("/datasets/{dataset_id}/preview", response_model=DatasetPreviewResponse)
def preview_dataset(
    dataset_id: uuid.UUID,
    row_count: int = Query(default=PREVIEW_DEFAULT_ROWS, ge=1),
    service: PreviewService = Depends(get_preview_service),
    current_user: User = Depends(require_permission("data_preview.read")),
) -> DatasetPreviewResponse:
    """Live read against the actual source database via the dataset's
    connection — never this platform's own tables. row_count is silently
    clamped to PREVIEW_MAX_ROWS regardless of what's requested; see
    PreviewService for why that's a clamp, not a 422 rejection."""
    result = service.preview_dataset(actor=current_user, dataset_id=dataset_id, requested_row_count=row_count)
    return DatasetPreviewResponse(**result)


@router.put("/datasets/{dataset_id}/key-columns", response_model=DatasetResponse)
def set_dataset_key_columns(
    dataset_id: uuid.UUID,
    payload: KeyColumnsRequest,
    service: DatasetKeyService = Depends(get_dataset_key_service),
    current_user: User = Depends(require_permission("metadata.manage")),
) -> DatasetResponse:
    dataset = service.set_key_columns(
        actor=current_user,
        dataset_id=dataset_id,
        key_columns=[kc.model_dump() for kc in payload.columns],
    )
    return DatasetResponse.model_validate(dataset)


def _serialize_candidate(evidence) -> BusinessKeyCandidateResponse:
    return BusinessKeyCandidateResponse(
        columns=list(evidence.columns), width=evidence.width, status=evidence.status, reason=evidence.reason,
        total_rows_evaluated=evidence.total_rows_evaluated, null_key_rows=evidence.null_key_rows,
        distinct_key_count=evidence.distinct_key_count, duplicate_key_groups=evidence.duplicate_key_groups,
        verification_level=evidence.verification_level,
    )


@router.get("/datasets/{dataset_id}/business-key/candidates", response_model=BusinessKeyDiscoveryResponse)
def discover_business_key_candidates_route(
    dataset_id: uuid.UUID,
    service: BusinessKeyService = Depends(get_business_key_service),
    current_user: User = Depends(require_permission("metadata.read")),
) -> BusinessKeyDiscoveryResponse:
    """Phase 4.6 — DETECTION ONLY. Never mutates dataset_key_columns or
    datasets.key_strategy. See BusinessKeyService.discover for the exact
    read-only guarantee."""
    report = service.discover(actor=current_user, dataset_id=dataset_id)
    result = report.result
    return BusinessKeyDiscoveryResponse(
        dataset_id=report.dataset_id,
        existing_key_strategy=report.existing_key_strategy,
        already_has_reliable_key=report.already_has_reliable_key,
        status=result.status if result else None,
        recommended=_serialize_candidate(result.recommended) if result and result.recommended else None,
        candidates=[_serialize_candidate(c) for c in result.candidates] if result else [],
        rejected_columns=[
            BusinessKeyRejectedColumnResponse(name=rc.name, normalized_data_type=rc.normalized_data_type, reason=rc.reason)
            for rc in (result.rejected_columns if result else [])
        ],
        widths_searched=list(result.widths_searched) if result else [],
        total_rows_evaluated=result.total_rows_evaluated if result else None,
        is_full_scan=result.is_full_scan if result else None,
        reason=result.reason if result else None,
    )


@router.post("/datasets/{dataset_id}/business-key/confirm", response_model=DatasetResponse)
def confirm_business_key_route(
    dataset_id: uuid.UUID,
    payload: BusinessKeyConfirmRequest,
    service: BusinessKeyService = Depends(get_business_key_service),
    current_user: User = Depends(require_permission("metadata.manage")),
) -> DatasetResponse:
    """Phase 4.6 — the only path that may adopt a business-key candidate.
    Always reverifies live before adopting — see BusinessKeyService.confirm."""
    dataset = service.confirm(actor=current_user, dataset_id=dataset_id, columns=payload.columns)
    return DatasetResponse.model_validate(dataset)


@router.patch("/datasets/{dataset_id}", response_model=DatasetResponse)
def patch_dataset(
    dataset_id: uuid.UUID,
    payload: DatasetPatchRequest,
    service: DatasetService = Depends(get_dataset_service),
    current_user: User = Depends(require_permission("metadata.manage")),
) -> DatasetResponse:
    dataset = service.set_active(actor=current_user, dataset_id=dataset_id, is_active=payload.is_active)
    return DatasetResponse.model_validate(dataset)
