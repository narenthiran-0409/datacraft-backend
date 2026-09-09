import uuid

import pytest
from fastapi.testclient import TestClient

RANDOM_ID = str(uuid.uuid4())

# (method, path, json body or None) — routes requiring a specific permission.
# All of these should reject a token that lacks the permission with 403,
# and reject no token at all with 401.
PERMISSION_ROUTES = [
    ("GET", "/api/v1/users", None),
    ("POST", "/api/v1/users", {"email": "x@example.com", "password": "Str0ng!Passw0rd"}),
    ("GET", f"/api/v1/users/{RANDOM_ID}", None),
    ("PUT", f"/api/v1/users/{RANDOM_ID}", {"full_name": "X"}),
    ("POST", f"/api/v1/users/{RANDOM_ID}/reset-password", {}),
    ("GET", "/api/v1/roles", None),
    ("GET", "/api/v1/data-sources", None),
    ("POST", "/api/v1/data-sources", {"name": "X"}),
    ("GET", f"/api/v1/data-sources/{RANDOM_ID}", None),
    ("PUT", f"/api/v1/data-sources/{RANDOM_ID}", {"description": "X"}),
    ("DELETE", f"/api/v1/data-sources/{RANDOM_ID}", None),
    ("GET", "/api/v1/connection-types", None),
    ("GET", "/api/v1/connections", None),
    (
        "POST",
        "/api/v1/connections",
        {
            "data_source_id": RANDOM_ID,
            "connection_type_id": RANDOM_ID,
            "name": "X",
            "host": "localhost",
            "port": 5432,
            "username": "svc",
            "credential": {"username": "svc", "password": "pw"},
        },
    ),
    ("GET", f"/api/v1/connections/{RANDOM_ID}", None),
    ("PUT", f"/api/v1/connections/{RANDOM_ID}", {"host": "127.0.0.1"}),
    ("DELETE", f"/api/v1/connections/{RANDOM_ID}", None),
    ("POST", f"/api/v1/connections/{RANDOM_ID}/test", None),
]

AUTH_REQUIRED_ROUTES = [
    ("POST", "/api/v1/auth/logout", {"refresh_token": "irrelevant"}),
    ("GET", "/api/v1/auth/me", None),
    ("POST", "/api/v1/auth/change-password", {"current_password": "a", "new_password": "b"}),
]


@pytest.mark.parametrize("method,path,body", PERMISSION_ROUTES)
def test_route_rejects_missing_token(client: TestClient, method: str, path: str, body) -> None:
    response = client.request(method, path, json=body)
    assert response.status_code == 401


@pytest.mark.parametrize("method,path,body", PERMISSION_ROUTES)
def test_route_rejects_token_without_permission(
    client: TestClient, no_role_headers: dict, method: str, path: str, body
) -> None:
    response = client.request(method, path, json=body, headers=no_role_headers)
    assert response.status_code == 403


@pytest.mark.parametrize("method,path,body", AUTH_REQUIRED_ROUTES)
def test_auth_required_route_rejects_missing_token(client: TestClient, method: str, path: str, body) -> None:
    response = client.request(method, path, json=body)
    assert response.status_code == 401
