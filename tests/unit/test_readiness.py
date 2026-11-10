import asyncio
import base64
import logging
import os
import socket
import time
from collections.abc import Iterator
from types import CoroutineType

import pytest
from fake_postgres import FakePostgres, fake_postgres
from fastapi.testclient import TestClient

from sage_bot.api.app import create_app
from sage_bot.api.readiness import (
    ReadinessCheck,
    not_configured,
    postgres_check,
    readiness_from_environment,
)
from sage_bot.db.runtime_role import ROLE_CHECK, ROLE_CHECK_MARKER

# Synthetic credentials only; nothing here reaches a real database.
PASSWORD = "readiness-test-password"  # noqa: S105
ENCRYPTION_KEY = base64.b64encode(bytes(range(32))).decode()
REVISION = "0001"
SETTINGS_VARIABLES = {
    "SAGE_ENV",
    "DATABASE_URL",
    "SAGE_ENCRYPTION_KEY",
    "READINESS_TIMEOUT_SECONDS",
}


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.upper() in SETTINGS_VARIABLES:
            monkeypatch.delenv(name)


def _static(ready: bool) -> ReadinessCheck:
    async def check() -> bool:
        return ready

    return check


@pytest.mark.parametrize(
    ("ready", "status", "body"), [(True, 200, "ok"), (False, 503, "not_ready")]
)
def test_readiness_reports_the_check_result(ready: bool, status: int, body: str) -> None:
    client = TestClient(create_app(readiness=_static(ready)))
    response = client.get("/health/ready")
    assert response.status_code == status
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"status": body}
    head = client.head("/health/ready")
    assert head.status_code == status
    assert head.content == b""


def test_application_without_a_readiness_check_is_not_ready() -> None:
    client = TestClient(create_app())
    assert client.get("/health/ready").json() == {"status": "not_ready"}
    assert client.get("/health/live").status_code == 200


def test_a_failing_check_is_reported_as_not_ready_without_details() -> None:
    async def broken() -> bool:
        raise RuntimeError(f"postgresql://user:{PASSWORD}@db.internal/sage")

    response = TestClient(create_app(readiness=broken)).get("/health/ready")
    assert response.status_code == 503
    assert response.content == b'{"status":"not_ready"}'


def test_liveness_is_unaffected_by_readiness() -> None:
    response = TestClient(create_app(readiness=_static(False))).get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.fixture
def refused_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def silent_port() -> Iterator[int]:
    """A port that accepts TCP connections but never answers the PostgreSQL handshake."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        yield int(listener.getsockname()[1])


def _url(port: int) -> str:
    return f"postgresql://sage:{PASSWORD}@127.0.0.1:{port}/sage"


def test_unreachable_database_is_not_ready(
    refused_port: int, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        assert (
            asyncio.run(
                postgres_check(_url(refused_port), timeout_seconds=2, expected_revision=REVISION)()
            )
            is False
        )
    assert "database check failed" in caplog.text
    assert PASSWORD not in caplog.text


def test_unresponsive_database_is_not_ready_within_the_timeout(silent_port: int) -> None:
    check = postgres_check(_url(silent_port), timeout_seconds=0.5, expected_revision=REVISION)
    started = time.monotonic()
    # The outer bound turns a missing timeout into a failure instead of a hung test.
    assert asyncio.run(asyncio.wait_for(check(), timeout=5)) is False
    assert time.monotonic() - started < 3


def test_missing_settings_make_the_service_not_ready(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        check = readiness_from_environment()
    assert check is not_configured
    assert asyncio.run(check()) is False
    assert "settings are missing or invalid" in caplog.text


def test_invalid_settings_are_not_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{PASSWORD}.db")
    monkeypatch.setenv("SAGE_ENCRYPTION_KEY", ENCRYPTION_KEY)
    with caplog.at_level(logging.WARNING):
        assert readiness_from_environment() is not_configured
    assert PASSWORD not in caplog.text
    assert ENCRYPTION_KEY not in caplog.text


def test_valid_settings_check_the_configured_database(
    monkeypatch: pytest.MonkeyPatch, refused_port: int
) -> None:
    monkeypatch.setenv("DATABASE_URL", _url(refused_port))
    monkeypatch.setenv("SAGE_ENCRYPTION_KEY", ENCRYPTION_KEY)
    monkeypatch.setenv("READINESS_TIMEOUT_SECONDS", "1")
    check = readiness_from_environment()
    assert check is not not_configured
    response = TestClient(create_app(readiness=check)).get("/health/ready")
    assert response.status_code == 503


class _FakeCursor:
    def __init__(self, query: str) -> None:
        self._row = ("",) if ROLE_CHECK_MARKER in query else (REVISION,)

    async def fetchall(self) -> list[tuple[str]]:
        return [self._row]

    async def fetchone(self) -> tuple[str]:
        return self._row


class _FakeConnection:
    async def __aenter__(self) -> "_FakeConnection":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def execute(self, query: str) -> _FakeCursor:
        return _FakeCursor(query)

    async def close(self) -> None:
        return None


def test_concurrent_probes_share_one_database_check(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[dict[str, object]] = []

    async def slow_connect(conninfo: str, **options: object) -> _FakeConnection:
        attempts.append(options)
        await asyncio.sleep(0.2)
        return _FakeConnection()

    monkeypatch.setattr("psycopg.AsyncConnection.connect", slow_connect)
    check = postgres_check(_url(5432), timeout_seconds=2, expected_revision=REVISION)

    async def flood() -> list[bool]:
        return await asyncio.gather(*(check() for _ in range(50)))

    assert asyncio.run(flood()) == [True] * 50
    assert len(attempts) == 1, "a probe flood must not open one connection per request"
    assert attempts[0]["connect_timeout"] == 2
    assert attempts[0]["application_name"] == "sage-bot-readiness"
    # Not cached: the next probe checks again.
    assert asyncio.run(check()) is True
    assert len(attempts) == 2


def test_only_the_error_type_is_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "sentinel-secret-in-error"  # noqa: S105

    async def failing_connect(conninfo: str, **options: object) -> _FakeConnection:
        raise ConnectionError(f"could not connect to {conninfo} ({secret})")

    monkeypatch.setattr("psycopg.AsyncConnection.connect", failing_connect)
    with caplog.at_level(logging.WARNING):
        assert (
            asyncio.run(postgres_check(_url(5432), timeout_seconds=1, expected_revision=REVISION)())
            is False
        )
    assert "database check failed (ConnectionError)" in caplog.text
    assert secret not in caplog.text
    assert PASSWORD not in caplog.text


def test_a_probe_left_on_a_closed_event_loop_is_not_reused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def slow_connect(conninfo: str, **options: object) -> _FakeConnection:
        await asyncio.sleep(0.2)
        return _FakeConnection()

    monkeypatch.setattr("psycopg.AsyncConnection.connect", slow_connect)
    check = postgres_check(_url(5432), timeout_seconds=2, expected_revision=REVISION)

    first = asyncio.new_event_loop()
    try:
        with pytest.raises(TimeoutError):
            # The caller gives up; the shared probe stays pending on this loop.
            first.run_until_complete(asyncio.wait_for(check(), timeout=0.01))
    finally:
        first.close()

    assert asyncio.run(asyncio.wait_for(check(), timeout=5)) is True


def _probe_tasks() -> list[asyncio.Task[object]]:
    def probe_code(task: asyncio.Task[object]) -> bool:
        coroutine = task.get_coro()
        if not isinstance(coroutine, CoroutineType):
            return False
        filename = coroutine.cr_code.co_filename
        return filename.endswith("readiness.py") or "/psycopg/" in filename

    return [task for task in asyncio.all_tasks() if not task.done() and probe_code(task)]


def test_a_stalled_probe_leaves_no_task_behind(stalling_database: FakePostgres) -> None:
    check = postgres_check(
        _url(stalling_database.port), timeout_seconds=0.3, expected_revision=REVISION
    )

    async def probe_and_count() -> tuple[bool, int]:
        before = asyncio.all_tasks()
        result = await check()
        return result, len(asyncio.all_tasks() - before)

    assert asyncio.run(asyncio.wait_for(probe_and_count(), timeout=5)) == (False, 0)


@pytest.fixture
def stalling_database() -> Iterator[FakePostgres]:
    with fake_postgres() as server:
        yield server


def test_query_stalled_after_the_handshake_is_bounded_and_recovers(
    stalling_database: FakePostgres, caplog: pytest.LogCaptureFixture
) -> None:
    timeout = 0.5
    check = postgres_check(
        _url(stalling_database.port), timeout_seconds=timeout, expected_revision=REVISION
    )
    leftover_tasks: list[int] = []

    async def counted_check() -> bool:
        result = await check()
        # Nothing the probe started may still be running once it has answered. The test
        # client runs its own tasks, so count only those executing readiness or psycopg code.
        leftover_tasks.append(len(_probe_tasks()))
        return result

    client = TestClient(create_app(readiness=counted_check))
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            started = time.monotonic()
            response = client.get("/health/ready")
            elapsed = time.monotonic() - started
            assert response.status_code == 503
            assert response.json() == {"status": "not_ready"}
            assert elapsed < timeout + 1.0, f"503 took {elapsed:.2f}s"
            # The stalled connection is closed promptly, not left for the server to drop.
            assert stalling_database.wait_until_closed(timeout=1.0)

    assert leftover_tasks == [0, 0, 0]
    # The deadline is enforced by closing the connection, not by psycopg's cancel protocol,
    # whose extra waiting would not be bounded by the readiness timeout.
    assert stalling_database.cancel_requests == 0
    assert [r.getMessage() for r in caplog.records if r.name.startswith("psycopg")] == []
    assert caplog.text.count("database check failed (TimeoutError)") == 3

    stalling_database.stall = False
    recovered = client.get("/health/ready")
    assert recovered.status_code == 200
    assert recovered.json() == {"status": "ok"}
    assert stalling_database.wait_until_closed(timeout=1.0)


@pytest.fixture
def answering_database() -> Iterator[FakePostgres]:
    with fake_postgres() as server:
        server.stall = False
        yield server


def test_database_at_the_expected_revision_is_ready(answering_database: FakePostgres) -> None:
    check = postgres_check(
        _url(answering_database.port), timeout_seconds=2, expected_revision=REVISION
    )
    assert asyncio.run(check()) is True


@pytest.mark.parametrize("recorded", ["0000", "ffffffffffff", ""])
def test_database_at_another_revision_is_not_ready(
    answering_database: FakePostgres, recorded: str, caplog: pytest.LogCaptureFixture
) -> None:
    answering_database.revision = recorded
    check = postgres_check(
        _url(answering_database.port), timeout_seconds=2, expected_revision=REVISION
    )
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(check()) is False
    assert "database schema is not at the expected revision" in caplog.text
    assert recorded == "" or recorded not in caplog.text


@pytest.mark.parametrize("problems", ["superuser", "owns_relation,schema_create"])
def test_unsafe_runtime_role_is_not_ready(
    answering_database: FakePostgres, problems: str, caplog: pytest.LogCaptureFixture
) -> None:
    answering_database.role_problems = problems
    check = postgres_check(
        _url(answering_database.port), timeout_seconds=2, expected_revision=REVISION
    )
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(check()) is False
    assert f"readiness: runtime database role is unsafe ({problems})" in caplog.text
    assert PASSWORD not in caplog.text


def test_role_check_problems_outside_the_known_codes_are_not_logged(
    answering_database: FakePostgres, caplog: pytest.LogCaptureFixture
) -> None:
    answering_database.role_problems = f"superuser,{PASSWORD}"
    check = postgres_check(
        _url(answering_database.port), timeout_seconds=2, expected_revision=REVISION
    )
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(check()) is False
    assert "runtime database role is unsafe (superuser,unknown)" in caplog.text
    assert PASSWORD not in caplog.text


def test_the_role_check_query_carries_its_marker() -> None:
    assert ROLE_CHECK.startswith(f"/* {ROLE_CHECK_MARKER} */")
