from fastapi.testclient import TestClient

from app.main import app


def test_healthz() -> None:
    client = TestClient(app)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_submit_placeholder_does_not_echo_url() -> None:
    client = TestClient(app)
    submitted_url = "https://rapidgator.example/private/file?token=secret"
    response = client.post("/submit", data={"url": submitted_url})
    assert response.status_code == 200
    assert submitted_url not in response.text
