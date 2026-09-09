import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.conftest import _create_user_with_role, auth_headers

RANDOM_ID = str(uuid.uuid4())

PERMISSION_ROUTES = [
    ("GET", f"/api/v1/lineage/CONNECTION/{RANDOM_ID}", None),
]


@pytest.mark.parametrize("method,path,body", PERMISSION_ROUTES)
def test_phase10_route_rejects_missing_token(client: TestClient, method: str, path: str, body) -> None:
    response = client.request(method, path, json=body)
    assert response.status_code == 401


@pytest.mark.parametrize("method,path,body", PERMISSION_ROUTES)
def test_phase10_route_rejects_token_without_permission(
    client: TestClient, no_role_headers: dict, method: str, path: str, body
) -> None:
    response = client.request(method, path, json=body, headers=no_role_headers)
    assert response.status_code == 403


@pytest.fixture
def analyst_headers(db: Session) -> dict:
    user = _create_user_with_role(db, "analyst", email="lineage_analyst@example.com")
    return auth_headers(user)


def test_unknown_entity_returns_404(client: TestClient, admin_headers: dict) -> None:
    response = client.get(f"/api/v1/lineage/CONNECTION/{RANDOM_ID}", headers=admin_headers)
    assert response.status_code == 404


def test_analyst_can_read_lineage(client: TestClient, analyst_headers: dict, db: Session) -> None:
    import uuid as uuid_module
    from app.modules.lineage.service import LineageService

    parent_id, child_id = uuid_module.uuid4(), uuid_module.uuid4()
    LineageService(db).record_edge("DATA_SOURCE", parent_id, "CONNECTION", child_id, "DERIVED_FROM")
    db.commit()

    response = client.get(f"/api/v1/lineage/CONNECTION/{child_id}", headers=analyst_headers)
    assert response.status_code == 200
    body = response.json()
    assert len(body["edges"]) == 1
    assert body["edges"][0]["relationship_type"] == "DERIVED_FROM"
    assert {n["entity_id"] for n in body["nodes"]} == {str(parent_id), str(child_id)}


def test_direction_query_param_filters_edges(client: TestClient, admin_headers: dict, db: Session) -> None:
    import uuid as uuid_module
    from app.modules.lineage.service import LineageService

    upstream_id, middle_id, downstream_id = uuid_module.uuid4(), uuid_module.uuid4(), uuid_module.uuid4()
    service = LineageService(db)
    service.record_edge("DATA_SOURCE", upstream_id, "CONNECTION", middle_id, "DERIVED_FROM")
    service.record_edge("CONNECTION", middle_id, "SCHEMA", downstream_id, "DERIVED_FROM")
    db.commit()

    up = client.get(f"/api/v1/lineage/CONNECTION/{middle_id}", headers=admin_headers, params={"direction": "up"})
    assert up.status_code == 200
    assert len(up.json()["edges"]) == 1
    assert up.json()["edges"][0]["parent_entity_id"] == str(upstream_id)

    down = client.get(f"/api/v1/lineage/CONNECTION/{middle_id}", headers=admin_headers, params={"direction": "down"})
    assert down.status_code == 200
    assert len(down.json()["edges"]) == 1
    assert down.json()["edges"][0]["child_entity_id"] == str(downstream_id)

    both = client.get(f"/api/v1/lineage/CONNECTION/{middle_id}", headers=admin_headers, params={"direction": "both"})
    assert both.status_code == 200
    assert len(both.json()["edges"]) == 2
