"""Unit-adjacent tests for LineageService against real local Postgres (no
other module involved) — proving record_edge/record_edges_bulk idempotency
and directional traversal in isolation before any of the 9 touch points are
wired in."""
import uuid

from sqlalchemy.orm import Session

from app.db.models import LineageRecord
from app.modules.lineage.service import LineageService


def test_record_edge_creates_row(db: Session) -> None:
    parent_id, child_id = uuid.uuid4(), uuid.uuid4()
    LineageService(db).record_edge("CONNECTION", parent_id, "SCHEMA", child_id, "DERIVED_FROM")
    db.commit()

    row = db.query(LineageRecord).filter_by(parent_entity_id=parent_id, child_entity_id=child_id).one()
    assert row.parent_entity_type == "CONNECTION"
    assert row.child_entity_type == "SCHEMA"
    assert row.relationship_type == "DERIVED_FROM"


def test_record_edge_idempotent_on_conflict_no_error_no_duplicate(db: Session) -> None:
    parent_id, child_id = uuid.uuid4(), uuid.uuid4()
    service = LineageService(db)
    service.record_edge("CONNECTION", parent_id, "SCHEMA", child_id, "DERIVED_FROM")
    db.commit()
    service.record_edge("CONNECTION", parent_id, "SCHEMA", child_id, "DERIVED_FROM")  # no error
    db.commit()

    rows = db.query(LineageRecord).filter_by(parent_entity_id=parent_id, child_entity_id=child_id).all()
    assert len(rows) == 1


def test_record_edges_bulk_creates_all_rows(db: Session) -> None:
    parent_id = uuid.uuid4()
    child_ids = [uuid.uuid4() for _ in range(3)]
    edges = [("REVIEW_RUN", parent_id, "ISSUE", cid, "DERIVED_FROM") for cid in child_ids]
    LineageService(db).record_edges_bulk(edges)
    db.commit()

    rows = db.query(LineageRecord).filter_by(parent_entity_id=parent_id).all()
    assert len(rows) == 3
    assert {r.child_entity_id for r in rows} == set(child_ids)


def test_record_edges_bulk_idempotent(db: Session) -> None:
    parent_id = uuid.uuid4()
    child_ids = [uuid.uuid4() for _ in range(2)]
    edges = [("REVIEW_RUN", parent_id, "ISSUE", cid, "DERIVED_FROM") for cid in child_ids]
    service = LineageService(db)
    service.record_edges_bulk(edges)
    db.commit()
    service.record_edges_bulk(edges)  # no error, no duplicates
    db.commit()

    rows = db.query(LineageRecord).filter_by(parent_entity_id=parent_id).all()
    assert len(rows) == 2


def test_record_edges_bulk_empty_list_is_noop(db: Session) -> None:
    LineageService(db).record_edges_bulk([])
    db.commit()  # should not raise


def test_get_upstream_returns_edges_where_entity_is_child(db: Session) -> None:
    parent_id, child_id = uuid.uuid4(), uuid.uuid4()
    other_child_id = uuid.uuid4()
    service = LineageService(db)
    service.record_edge("DATA_SOURCE", parent_id, "CONNECTION", child_id, "DERIVED_FROM")
    service.record_edge("DATA_SOURCE", parent_id, "CONNECTION", other_child_id, "DERIVED_FROM")
    db.commit()

    upstream = service.get_upstream("CONNECTION", child_id)
    assert len(upstream) == 1
    assert upstream[0].parent_entity_id == parent_id


def test_get_downstream_returns_edges_where_entity_is_parent(db: Session) -> None:
    parent_id, child_id_a, child_id_b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    service = LineageService(db)
    service.record_edge("DATA_SOURCE", parent_id, "CONNECTION", child_id_a, "DERIVED_FROM")
    service.record_edge("DATA_SOURCE", parent_id, "CONNECTION", child_id_b, "DERIVED_FROM")
    db.commit()

    downstream = service.get_downstream("DATA_SOURCE", parent_id)
    assert len(downstream) == 2
    assert {e.child_entity_id for e in downstream} == {child_id_a, child_id_b}


def test_get_both_combines_upstream_and_downstream(db: Session) -> None:
    upstream_parent_id = uuid.uuid4()
    middle_id = uuid.uuid4()
    downstream_child_id = uuid.uuid4()
    service = LineageService(db)
    service.record_edge("DATA_SOURCE", upstream_parent_id, "CONNECTION", middle_id, "DERIVED_FROM")
    service.record_edge("CONNECTION", middle_id, "SCHEMA", downstream_child_id, "DERIVED_FROM")
    db.commit()

    both = service.get_both("CONNECTION", middle_id)
    assert len(both) == 2


def test_get_both_returns_empty_for_unknown_entity(db: Session) -> None:
    assert LineageService(db).get_both("CONNECTION", uuid.uuid4()) == []
