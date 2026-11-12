from fastapi.testclient import TestClient

from financial_analyst.main import APP_VERSION, create_app


def test_health_returns_ok(settings):
    client = TestClient(create_app(settings))

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": APP_VERSION}
