"""End-to-end flow: log in as the seeded administrator, create a data
source, create a connection pointing at a real reachable local Postgres
instance (this project's own dev database), test it and expect HEALTHY,
refresh the token, log out, and confirm the used refresh token is rejected.
"""
from urllib.parse import urlparse

from fastapi.testclient import TestClient

from app.core.config import settings


def test_full_flow(client: TestClient, admin_user) -> None:
    login_response = client.post(
        "/api/v1/auth/login", json={"email": "admin@example.com", "password": "Str0ng!Passw0rd"}
    )
    assert login_response.status_code == 200
    tokens = login_response.json()
    access_token = tokens["access_token"]
    refresh_token = tokens["refresh_token"]
    headers = {"Authorization": f"Bearer {access_token}"}

    me_response = client.get("/api/v1/auth/me", headers=headers)
    assert me_response.status_code == 200
    assert "data_sources.manage" in me_response.json()["permissions"]

    ds_response = client.post("/api/v1/data-sources", json={"name": "E2E Data Source"}, headers=headers)
    assert ds_response.status_code == 201
    data_source_id = ds_response.json()["id"]

    ct_response = client.get("/api/v1/connection-types", headers=headers)
    postgres_type_id = next(ct["id"] for ct in ct_response.json() if ct["code"] == "POSTGRESQL")

    db_url = urlparse(settings.DATABASE_URL.replace("postgresql+psycopg", "postgresql"))

    conn_response = client.post(
        "/api/v1/connections",
        headers=headers,
        json={
            "data_source_id": data_source_id,
            "connection_type_id": postgres_type_id,
            "name": "E2E Connection",
            "environment": "DEV",
            "host": db_url.hostname,
            "port": db_url.port or 5432,
            "database_name": db_url.path.lstrip("/"),
            "username": db_url.username,
            "credential": {"username": db_url.username, "password": db_url.password},
        },
    )
    assert conn_response.status_code == 201
    connection = conn_response.json()
    assert "credential_ref" not in connection
    connection_id = connection["id"]

    test_response = client.post(f"/api/v1/connections/{connection_id}/test", headers=headers)
    assert test_response.status_code == 200
    assert test_response.json()["status"] == "HEALTHY"

    refresh_response = client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert refresh_response.status_code == 200
    new_tokens = refresh_response.json()
    assert new_tokens["refresh_token"] != refresh_token

    reuse_response = client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert reuse_response.status_code == 401

    new_headers = {"Authorization": f"Bearer {new_tokens['access_token']}"}
    logout_response = client.post(
        "/api/v1/auth/logout", json={"refresh_token": new_tokens["refresh_token"]}, headers=new_headers
    )
    assert logout_response.status_code == 204

    reuse_after_logout = client.post("/api/v1/auth/refresh", json={"refresh_token": new_tokens["refresh_token"]})
    assert reuse_after_logout.status_code == 401
