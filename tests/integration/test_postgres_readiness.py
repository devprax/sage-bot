"""Readiness against a real PostgreSQL server.

Set SAGE_TEST_DATABASE_URL to a disposable database to run these tests. They are skipped
locally without it, and fail in CI if it is missing, so they can never pass by being skipped.
"""

import asyncio
import base64
import logging
import os
from urllib.parse import urlsplit, urlunsplit

import pytest
from fastapi.testclient import TestClient

from sage_bot.api.app import create_app
from sage_bot.api.readiness import postgres_check, readiness_from_environment

ENCRYPTION_KEY = base64.b64encode(bytes(range(32))).decode()


@pytest.fixture
def database_url() -> str:
    url = os.environ.get("SAGE_TEST_DATABASE_URL", "")
    if not url:
        if os.environ.get("CI", "").lower() in {"1", "true", "yes"}:
            pytest.fail("SAGE_TEST_DATABASE_URL must be set in CI")
        pytest.skip("set SAGE_TEST_DATABASE_URL to run PostgreSQL integration tests")
    return url


def _with(url: str, *, password: str | None = None, database: str | None = None) -> str:
    parts = urlsplit(url)
    netloc = parts.netloc
    if password is not None:
        credentials, _, host = netloc.rpartition("@")
        netloc = f"{credentials.split(':')[0]}:{password}@{host}"
    path = f"/{database}" if database is not None else parts.path
    return urlunsplit(parts._replace(netloc=netloc, path=path))


def test_reachable_database_is_ready(database_url: str) -> None:
    assert asyncio.run(postgres_check(database_url, timeout_seconds=5)()) is True


@pytest.mark.parametrize(
    "change", [{"password": "wrong-password-for-test"}, {"database": "sage_missing_db"}]
)
def test_rejected_connection_is_not_ready_and_not_logged(
    database_url: str, change: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    url = _with(database_url, **change)
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(postgres_check(url, timeout_seconds=5)()) is False
    assert "database check failed" in caplog.text
    real_password = urlsplit(database_url).password
    assert real_password is not None
    assert real_password not in caplog.text
    assert "wrong-password-for-test" not in caplog.text


def test_service_is_ready_with_configured_settings(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("SAGE_ENCRYPTION_KEY", ENCRYPTION_KEY)
    client = TestClient(create_app(readiness=readiness_from_environment()))
    response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert client.head("/health/ready").status_code == 200
