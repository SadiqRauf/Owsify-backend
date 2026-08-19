from fastapi.testclient import TestClient


def test_health_reports_database_connectivity(client: TestClient) -> None:
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "connected"
    assert body["version"]


def test_liveness_does_not_touch_the_database(client: TestClient) -> None:
    assert client.get("/api/v1/health/live").json() == {"status": "ok"}


def test_every_response_carries_a_request_id(client: TestClient) -> None:
    response = client.get("/api/v1/health/live")
    assert response.headers["X-Request-ID"]


def test_openapi_is_served_under_the_v1_prefix(client: TestClient) -> None:
    response = client.get("/api/v1/openapi.json")
    assert response.status_code == 200
    assert "/api/v1/auth/login" in response.json()["paths"]
