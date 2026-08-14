from fastapi.testclient import TestClient

# This test exercises real PostgreSQL and Redis connectivity, matching the
# services defined in docker-compose.yml. It requires the stack to be up
# (e.g. `docker compose up -d postgres redis` or the full stack) and is not
# mocked, since the point of Phase 1 is proving real connectivity.


def test_readyz_returns_200_when_dependencies_are_reachable(client: TestClient) -> None:
    response = client.get("/readyz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["dependencies"]["postgres"]["status"] == "up"
    assert body["dependencies"]["redis"]["status"] == "up"
