import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.v1.data_sources.schemas import DataSourceCreateRequest, DataSourceResponse, DataSourceUpdateRequest
from app.core.database import get_db
from app.core.dependencies import require_permission
from app.db.models import User
from app.modules.data_sources.service import DataSourcesService

router = APIRouter(prefix="/data-sources", tags=["data-sources"])


def get_data_sources_service(db: Session = Depends(get_db)) -> DataSourcesService:
    return DataSourcesService(db)


@router.get("", response_model=list[DataSourceResponse])
def list_data_sources(
    is_active: bool | None = Query(default=None),
    service: DataSourcesService = Depends(get_data_sources_service),
    _: User = Depends(require_permission("data_sources.read")),
) -> list[DataSourceResponse]:
    return [DataSourceResponse.model_validate(d) for d in service.list_data_sources(is_active=is_active)]


@router.post("", response_model=DataSourceResponse, status_code=201)
def create_data_source(
    payload: DataSourceCreateRequest,
    service: DataSourcesService = Depends(get_data_sources_service),
    current_user: User = Depends(require_permission("data_sources.manage")),
) -> DataSourceResponse:
    data_source = service.create_data_source(
        actor=current_user,
        name=payload.name,
        description=payload.description,
        owner_team=payload.owner_team,
        business_domain=payload.business_domain,
    )
    return DataSourceResponse.model_validate(data_source)


@router.get("/{data_source_id}", response_model=DataSourceResponse)
def get_data_source(
    data_source_id: uuid.UUID,
    service: DataSourcesService = Depends(get_data_sources_service),
    _: User = Depends(require_permission("data_sources.read")),
) -> DataSourceResponse:
    return DataSourceResponse.model_validate(service.get_data_source(data_source_id))


@router.put("/{data_source_id}", response_model=DataSourceResponse)
def update_data_source(
    data_source_id: uuid.UUID,
    payload: DataSourceUpdateRequest,
    service: DataSourcesService = Depends(get_data_sources_service),
    current_user: User = Depends(require_permission("data_sources.manage")),
) -> DataSourceResponse:
    data_source = service.update_data_source(
        actor=current_user,
        data_source_id=data_source_id,
        description=payload.description,
        owner_team=payload.owner_team,
        business_domain=payload.business_domain,
    )
    return DataSourceResponse.model_validate(data_source)


@router.delete("/{data_source_id}", response_model=DataSourceResponse)
def deactivate_data_source(
    data_source_id: uuid.UUID,
    service: DataSourcesService = Depends(get_data_sources_service),
    current_user: User = Depends(require_permission("data_sources.manage")),
) -> DataSourceResponse:
    data_source = service.deactivate_data_source(actor=current_user, data_source_id=data_source_id)
    return DataSourceResponse.model_validate(data_source)


@router.post("/{data_source_id}/reactivate", response_model=DataSourceResponse)
def reactivate_data_source(
    data_source_id: uuid.UUID,
    service: DataSourcesService = Depends(get_data_sources_service),
    current_user: User = Depends(require_permission("data_sources.manage")),
) -> DataSourceResponse:
    data_source = service.reactivate_data_source(actor=current_user, data_source_id=data_source_id)
    return DataSourceResponse.model_validate(data_source)
