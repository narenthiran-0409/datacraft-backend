import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import require_permission
from app.db.models import User
from app.modules.lineage.schemas import LineageEdge, LineageGraphResponse, LineageNode
from app.modules.lineage.service import LineageService

router = APIRouter(tags=["lineage"])


def get_lineage_service(db: Session = Depends(get_db)) -> LineageService:
    return LineageService(db)


@router.get("/lineage/{entity_type}/{entity_id}", response_model=LineageGraphResponse)
def get_lineage(
    entity_type: str,
    entity_id: uuid.UUID,
    direction: Literal["up", "down", "both"] = Query(default="both"),
    service: LineageService = Depends(get_lineage_service),
    _: User = Depends(require_permission("lineage.read")),
) -> LineageGraphResponse:
    records = service.get_edges_for_direction(entity_type, entity_id, direction)

    nodes: dict[tuple[str, uuid.UUID], LineageNode] = {
        (entity_type, entity_id): LineageNode(entity_type=entity_type, entity_id=entity_id)
    }
    edges = []
    for r in records:
        nodes.setdefault(
            (r.parent_entity_type, r.parent_entity_id),
            LineageNode(entity_type=r.parent_entity_type, entity_id=r.parent_entity_id),
        )
        nodes.setdefault(
            (r.child_entity_type, r.child_entity_id),
            LineageNode(entity_type=r.child_entity_type, entity_id=r.child_entity_id),
        )
        edges.append(
            LineageEdge(
                parent_entity_type=r.parent_entity_type, parent_entity_id=r.parent_entity_id,
                child_entity_type=r.child_entity_type, child_entity_id=r.child_entity_id,
                relationship_type=r.relationship_type,
            )
        )

    return LineageGraphResponse(nodes=list(nodes.values()), edges=edges)
