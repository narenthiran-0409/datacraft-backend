import uuid

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.exceptions import LineageEntityNotFoundError
from app.db.models import LineageRecord

_CONFLICT_CONSTRAINT = "uq_lineage_records_edge"


class LineageService:
    """The ONLY writer of lineage_records. Called BY each of the 9
    instrumentation touch points — never calls into any other module's
    service itself, so it carries no risk of altering Phase 1-9 behavior
    by way of a transitive dependency.

    Every write is INSERT ... ON CONFLICT DO NOTHING against the frozen
    unique constraint (parent_entity_type, parent_entity_id,
    child_entity_type, child_entity_id, relationship_type) — idempotent by
    construction, no additional locking or pre-check, per the approved
    design (constraint 7)."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def record_edge(
        self,
        parent_entity_type: str,
        parent_entity_id: uuid.UUID,
        child_entity_type: str,
        child_entity_id: uuid.UUID,
        relationship_type: str,
    ) -> None:
        stmt = pg_insert(LineageRecord).values(
            parent_entity_type=parent_entity_type,
            parent_entity_id=parent_entity_id,
            child_entity_type=child_entity_type,
            child_entity_id=child_entity_id,
            relationship_type=relationship_type,
        )
        self._db.execute(stmt.on_conflict_do_nothing(constraint=_CONFLICT_CONSTRAINT))

    def record_edges_bulk(
        self, edges: list[tuple[str, uuid.UUID, str, uuid.UUID, str]]
    ) -> None:
        """Same idempotent insert, batched as ONE statement for the whole
        list — used by touch points 4 (REVIEW_RUN -> ISSUE, one bulk insert
        per review run creation) and 6 (CORRECTION -> APPROVAL_REQUEST, one
        bulk insert per submission), matching this project's established
        single-query-for-all-rows performance discipline. A no-op on an
        empty list (never emits a zero-row INSERT)."""
        if not edges:
            return
        values = [
            {
                "parent_entity_type": p_type,
                "parent_entity_id": p_id,
                "child_entity_type": c_type,
                "child_entity_id": c_id,
                "relationship_type": rel,
            }
            for p_type, p_id, c_type, c_id, rel in edges
        ]
        stmt = pg_insert(LineageRecord).values(values)
        self._db.execute(stmt.on_conflict_do_nothing(constraint=_CONFLICT_CONSTRAINT))

    def get_upstream(self, entity_type: str, entity_id: uuid.UUID) -> list[LineageRecord]:
        """Edges where the given entity is the CHILD — reads via
        idx_lineage_child."""
        return list(
            self._db.execute(
                select(LineageRecord).where(
                    LineageRecord.child_entity_type == entity_type,
                    LineageRecord.child_entity_id == entity_id,
                )
            ).scalars()
        )

    def get_downstream(self, entity_type: str, entity_id: uuid.UUID) -> list[LineageRecord]:
        """Edges where the given entity is the PARENT — reads via
        idx_lineage_parent."""
        return list(
            self._db.execute(
                select(LineageRecord).where(
                    LineageRecord.parent_entity_type == entity_type,
                    LineageRecord.parent_entity_id == entity_id,
                )
            ).scalars()
        )

    def get_both(self, entity_type: str, entity_id: uuid.UUID) -> list[LineageRecord]:
        return list(
            self._db.execute(
                select(LineageRecord).where(
                    or_(
                        (LineageRecord.child_entity_type == entity_type) & (LineageRecord.child_entity_id == entity_id),
                        (LineageRecord.parent_entity_type == entity_type) & (LineageRecord.parent_entity_id == entity_id),
                    )
                )
            ).scalars()
        )

    def get_edges_for_direction(
        self, entity_type: str, entity_id: uuid.UUID, direction: str
    ) -> list[LineageRecord]:
        """API-facing read: 404s if the entity has no edges in EITHER
        direction, regardless of which single direction was requested —
        per the approved API spec's 404 condition."""
        both = self.get_both(entity_type, entity_id)
        if not both:
            raise LineageEntityNotFoundError(
                f"No lineage edges found for {entity_type}/{entity_id} in either direction"
            )
        if direction == "up":
            return self.get_upstream(entity_type, entity_id)
        if direction == "down":
            return self.get_downstream(entity_type, entity_id)
        return both
