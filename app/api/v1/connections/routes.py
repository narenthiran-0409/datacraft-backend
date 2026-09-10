import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.v1.connections.schemas import (
    ConnectionCreateRequest,
    ConnectionResponse,
    ConnectionTypeResponse,
    ConnectionUpdateRequest,
)
from app.api.v1.jobs.schemas import JobResponse
from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import require_permission
from app.core.exceptions import DiscoveryAlreadyRunningError
from app.core.redis_client import get_redis_client
from app.db.models import User
from app.modules.connections.credential_vault import CredentialVaultClient, LocalRedisVaultClient
from app.modules.connections.service import ConnectionsService
from app.modules.discovery.tasks import run_discovery
from app.modules.jobs.service import JobsService

router = APIRouter(tags=["connections"])


def get_vault_client() -> CredentialVaultClient:
    return LocalRedisVaultClient(get_redis_client(), settings.VAULT_LOCAL_ENCRYPTION_KEY)


def get_connections_service(db: Session = Depends(get_db)) -> ConnectionsService:
    return ConnectionsService(db, get_vault_client())


def get_jobs_service(db: Session = Depends(get_db)) -> JobsService:
    return JobsService(db, get_redis_client())


@router.get("/connection-types", response_model=list[ConnectionTypeResponse])
def list_connection_types(
    service: ConnectionsService = Depends(get_connections_service),
    _: User = Depends(require_permission("connections.read")),
) -> list[ConnectionTypeResponse]:
    return [ConnectionTypeResponse.model_validate(ct) for ct in service.list_connection_types()]


@router.get("/connections", response_model=list[ConnectionResponse])
def list_connections(
    is_active: bool | None = Query(default=None),
    service: ConnectionsService = Depends(get_connections_service),
    _: User = Depends(require_permission("connections.read")),
) -> list[ConnectionResponse]:
    return [ConnectionResponse.model_validate(c) for c in service.list_connections(is_active=is_active)]


@router.post("/connections", response_model=ConnectionResponse, status_code=201)
def create_connection(
    payload: ConnectionCreateRequest,
    service: ConnectionsService = Depends(get_connections_service),
    current_user: User = Depends(require_permission("connections.manage")),
) -> ConnectionResponse:
    connection = service.create_connection(
        actor=current_user,
        data_source_id=payload.data_source_id,
        connection_type_id=payload.connection_type_id,
        name=payload.name,
        environment=payload.environment,
        host=payload.host,
        port=payload.port,
        database_name=payload.database_name,
        service_name=payload.service_name,
        username=payload.username,
        credential=payload.credential.model_dump(),
        config=payload.config,
    )
    return ConnectionResponse.model_validate(connection)


@router.get("/connections/{connection_id}", response_model=ConnectionResponse)
def get_connection(
    connection_id: uuid.UUID,
    service: ConnectionsService = Depends(get_connections_service),
    _: User = Depends(require_permission("connections.read")),
) -> ConnectionResponse:
    return ConnectionResponse.model_validate(service.get_connection(connection_id))


@router.put("/connections/{connection_id}", response_model=ConnectionResponse)
def update_connection(
    connection_id: uuid.UUID,
    payload: ConnectionUpdateRequest,
    service: ConnectionsService = Depends(get_connections_service),
    current_user: User = Depends(require_permission("connections.manage")),
) -> ConnectionResponse:
    connection = service.update_connection(
        actor=current_user,
        connection_id=connection_id,
        name=payload.name,
        environment=payload.environment,
        host=payload.host,
        port=payload.port,
        database_name=payload.database_name,
        service_name=payload.service_name,
        username=payload.username,
        credential=payload.credential.model_dump() if payload.credential else None,
        config=payload.config,
    )
    return ConnectionResponse.model_validate(connection)


@router.delete("/connections/{connection_id}", response_model=ConnectionResponse)
def deactivate_connection(
    connection_id: uuid.UUID,
    service: ConnectionsService = Depends(get_connections_service),
    current_user: User = Depends(require_permission("connections.manage")),
) -> ConnectionResponse:
    return ConnectionResponse.model_validate(service.deactivate_connection(actor=current_user, connection_id=connection_id))


@router.post("/connections/{connection_id}/reactivate", response_model=ConnectionResponse)
def reactivate_connection(
    connection_id: uuid.UUID,
    service: ConnectionsService = Depends(get_connections_service),
    current_user: User = Depends(require_permission("connections.manage")),
) -> ConnectionResponse:
    return ConnectionResponse.model_validate(service.reactivate_connection(actor=current_user, connection_id=connection_id))


@router.post("/connections/{connection_id}/test", response_model=ConnectionResponse)
def test_connection(
    connection_id: uuid.UUID,
    service: ConnectionsService = Depends(get_connections_service),
    current_user: User = Depends(require_permission("connections.manage")),
) -> ConnectionResponse:
    return ConnectionResponse.model_validate(service.test_connection(actor=current_user, connection_id=connection_id))


@router.post("/connections/{connection_id}/discover", response_model=JobResponse, status_code=202)
def discover_connection(
    connection_id: uuid.UUID,
    connections_service: ConnectionsService = Depends(get_connections_service),
    jobs_service: JobsService = Depends(get_jobs_service),
    current_user: User = Depends(require_permission("discovery.run")),
) -> JobResponse:
    connections_service.get_connection(connection_id)  # 404 if the connection doesn't exist

    existing = jobs_service.find_in_flight(
        job_type="DISCOVERY_RUN", entity_type="CONNECTION", entity_id=connection_id
    )
    if existing is not None:
        raise DiscoveryAlreadyRunningError(
            f"A discovery job ({existing.id}) is already {existing.status} for this connection"
        )

    job = jobs_service.create(
        job_type="DISCOVERY_RUN",
        entity_type="CONNECTION",
        entity_id=connection_id,
        created_by=current_user.id,
    )
    run_discovery.delay(str(job.id))
    return JobResponse.model_validate(job)
