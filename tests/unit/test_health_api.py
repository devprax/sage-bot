import asyncio
import os
import socket

import pytest
from fastapi.testclient import TestClient

from sage_bot.api.app import create_app

CREDENTIAL_VARIABLES = {"DATABASE_URL", "SAGE_ENCRYPTION_KEY"}


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def test_liveness_reports_ok_as_json(client: TestClient) -> None:
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"status": "ok"}


def test_liveness_exposes_nothing_beyond_its_status(client: TestClient) -> None:
    response = client.get("/health/live")
    assert response.content == b'{"status":"ok"}'
    assert set(response.headers) == {"content-type", "content-length"}


def test_liveness_answers_head_probes_without_a_body(client: TestClient) -> None:
    response = client.head("/health/live")
    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["content-length"] == str(len(b'{"status":"ok"}'))


@pytest.mark.parametrize(
    "path", ["/docs", "/redoc", "/openapi.json", "/health", "/", "/health/live/"]
)
def test_no_other_routes_are_served(client: TestClient, path: str) -> None:
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}
    assert "location" not in response.headers


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_liveness_rejects_other_methods(client: TestClient, method: str) -> None:
    response = client.request(method, "/health/live")
    assert response.status_code == 405
    assert response.json() == {"detail": "Method Not Allowed"}
    assert set(response.headers["allow"].split(", ")) == {"GET", "HEAD"}


def test_liveness_needs_no_credentials_or_external_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in list(os.environ):
        if name.upper() in CREDENTIAL_VARIABLES or name.upper().startswith("SAGE_"):
            monkeypatch.delenv(name)

    def no_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("liveness must not contact external services")

    for owner, name in (
        (socket.socket, "connect"),
        (socket.socket, "connect_ex"),
        (socket, "create_connection"),
        (socket, "getaddrinfo"),
        (socket, "gethostbyname"),
        (asyncio.BaseEventLoop, "create_connection"),
        (asyncio.BaseEventLoop, "getaddrinfo"),
        (asyncio.BaseEventLoop, "sock_connect"),
    ):
        monkeypatch.setattr(owner, name, no_network)

    response = TestClient(create_app()).get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
