from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.conftest import _create_user_with_role, auth_headers

NOW = datetime.now(timezone.utc)
FROM = (NOW - timedelta(days=7)).isoformat()
TO = NOW.isoformat()

REPORT_ROUTES = [
    ("GET", "/api/v1/reports/quality-trend", {"from": FROM, "to": TO}),
    ("GET", "/api/v1/reports/rule-effectiveness", {"from": FROM, "to": TO}),
    ("GET", "/api/v1/reports/quality-by-dataset", {}),
    ("GET", "/api/v1/reports/review-performance", {"from": FROM, "to": TO}),
    ("GET", "/api/v1/reports/approval-metrics", {"from": FROM, "to": TO}),
]


@pytest.mark.parametrize("method,path,params", REPORT_ROUTES)
def test_phase11_route_rejects_missing_token(client: TestClient, method: str, path: str, params: dict) -> None:
    response = client.request(method, path, params=params)
    assert response.status_code == 401


@pytest.mark.parametrize("method,path,params", REPORT_ROUTES)
def test_phase11_route_rejects_token_without_permission(
    client: TestClient, no_role_headers: dict, method: str, path: str, params: dict
) -> None:
    response = client.request(method, path, params=params, headers=no_role_headers)
    assert response.status_code == 403


@pytest.mark.parametrize("method,path,params", REPORT_ROUTES)
def test_phase11_route_authorized_returns_200(client: TestClient, admin_headers: dict, method: str, path: str, params: dict) -> None:
    response = client.request(method, path, params=params, headers=admin_headers)
    assert response.status_code == 200


@pytest.mark.parametrize("role", ["analyst", "reviewer", "approver", "publisher", "administrator"])
def test_all_five_roles_can_access_reports(client: TestClient, db: Session, role: str) -> None:
    user = _create_user_with_role(db, role, email=f"reports_{role}@example.com")
    headers = auth_headers(user)
    response = client.get("/api/v1/reports/quality-by-dataset", headers=headers)
    assert response.status_code == 200


@pytest.mark.parametrize(
    "path", ["/api/v1/reports/quality-trend", "/api/v1/reports/rule-effectiveness", "/api/v1/reports/review-performance", "/api/v1/reports/approval-metrics"]
)
def test_from_after_to_returns_422(client: TestClient, admin_headers: dict, path: str) -> None:
    response = client.get(path, params={"from": TO, "to": FROM}, headers=admin_headers)
    assert response.status_code == 422


@pytest.mark.parametrize(
    "path", ["/api/v1/reports/quality-trend", "/api/v1/reports/rule-effectiveness", "/api/v1/reports/review-performance", "/api/v1/reports/approval-metrics"]
)
def test_missing_required_date_params_returns_422(client: TestClient, admin_headers: dict, path: str) -> None:
    response = client.get(path, headers=admin_headers)
    assert response.status_code == 422


@pytest.mark.parametrize(
    "path", ["/api/v1/reports/quality-trend", "/api/v1/reports/rule-effectiveness"]
)
def test_malformed_dataset_id_returns_422(client: TestClient, admin_headers: dict, path: str) -> None:
    response = client.get(path, params={"from": FROM, "to": TO, "dataset_id": "not-a-uuid"}, headers=admin_headers)
    assert response.status_code == 422


def test_no_quality_by_column_endpoint_exists(client: TestClient, admin_headers: dict) -> None:
    response = client.get("/api/v1/reports/quality-by-column", headers=admin_headers)
    assert response.status_code == 404


def test_no_quality_by_source_endpoint_exists(client: TestClient, admin_headers: dict) -> None:
    response = client.get("/api/v1/reports/quality-by-source", headers=admin_headers)
    assert response.status_code == 404
