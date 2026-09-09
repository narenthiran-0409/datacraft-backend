import uuid

from pydantic import BaseModel


class LineageNode(BaseModel):
    entity_type: str
    entity_id: uuid.UUID


class LineageEdge(BaseModel):
    parent_entity_type: str
    parent_entity_id: uuid.UUID
    child_entity_type: str
    child_entity_id: uuid.UUID
    relationship_type: str


class LineageGraphResponse(BaseModel):
    nodes: list[LineageNode]
    edges: list[LineageEdge]
