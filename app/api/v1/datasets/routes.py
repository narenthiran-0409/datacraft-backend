import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.v1.datasets.schemas import (
    ColumnResponse,
    DatasetListResponse,
    DatasetPatchRequest,
    DatasetResponse,
    KeyColumnsRequest,
    SchemaResponse,
)
from app.core.database import get_db
from app.core.dependencies import require_permission
from app.db.models import User
from app.modules.datasets.key_service import DatasetKeyService
from app.modules.datasets.service import DatasetService

router = APIRouter(tags=["datasets"])


def get_dataset_service(db: Session = Depends(get_db)) -> DatasetService:
    return DatasetService(db)


def get_dataset_key_service(db: Session = Depends(get_db)) -> DatasetKeyService:
    return DatasetKeyService(db)


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


@router.patch("/datasets/{dataset_id}", response_model=DatasetResponse)
def patch_dataset(
    dataset_id: uuid.UUID,
    payload: DatasetPatchRequest,
    service: DatasetService = Depends(get_dataset_service),
    current_user: User = Depends(require_permission("metadata.manage")),
) -> DatasetResponse:
    dataset = service.set_active(actor=current_user, dataset_id=dataset_id, is_active=payload.is_active)
    return DatasetResponse.model_validate(dataset)
