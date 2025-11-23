import logging

from fastapi.testclient import TestClient

from app.main import app


def test_request_logging(caplog) -> None:
    caplog.set_level(logging.INFO, logger="app.request")
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    assert "request handled" in caplog.text
