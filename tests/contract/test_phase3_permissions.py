import uuid

import pytest
from fastapi.testclient import TestClient

RANDOM_ID = str(uuid.uuid4())

# (method, path, json body or None) — Phase 3 routes requiring a specific
# permission. All should reject a token lacking the permission with 403,
# and reject no token at all with 401.
PERMISSION_ROUTES = [
    ("POST", f"/api/v1/connections/{RANDOM_ID}/discover", None),
    ("GET", f"/api/v1/jobs/{RANDOM_ID}", None),
    ("POST", f"/api/v1/jobs/{RANDOM_ID}/cancel", None),
    ("GET", f"/api/v1/schemas?connection_id={RANDOM_ID}", None),
    ("GET", "/api/v1/datasets", None),
    ("GET", f"/api/v1/datasets/{RANDOM_ID}", None),
    ("GET", f"/api/v1/datasets/{RANDOM_ID}/columns", None),
    ("PUT", f"/api/v1/datasets/{RANDOM_ID}/key-columns", {"columns": [{"column_id": RANDOM_ID, "ordinal": 0}]}),
    ("PATCH", f"/api/v1/datasets/{RANDOM_ID}", {"is_active": False}),
    ("GET", f"/api/v1/datasets/{RANDOM_ID}/preview", None),
]


@pytest.mark.parametrize("method,path,body", PERMISSION_ROUTES)
def test_phase3_route_rejects_missing_token(client: TestClient, method: str, path: str, body) -> None:
    response = client.request(method, path, json=body)
    assert response.status_code == 401


@pytest.mark.parametrize("method,path,body", PERMISSION_ROUTES)
def test_phase3_route_rejects_token_without_permission(
    client: TestClient, no_role_headers: dict, method: str, path: str, body
) -> None:
    response = client.request(method, path, json=body, headers=no_role_headers)
    assert response.status_code == 403
