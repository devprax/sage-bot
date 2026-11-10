"""Readiness and schema migrations against a real PostgreSQL server.

Fixtures are in conftest.py. Each test that migrates gets its own temporary database.
"""

import asyncio
import base64
import logging
import os
import subprocess
import sys
import time
from typing import Any
from urllib.parse import urlsplit

import psycopg
import pytest
from alembic import command
from fastapi.testclient import TestClient
from pg_support import url_with

from sage_bot.api.app import create_app
from sage_bot.api.readiness import postgres_check, readiness_from_environment
from sage_bot.db import migrate
from sage_bot.db.migrate import MIGRATION_LOCK_KEY

ENCRYPTION_KEY = base64.b64encode(bytes(range(32))).decode()
# Deliberately wrong credential for rejection tests; never a real password.
WRONG_PASSWORD = "wrong-password-for-test"  # noqa: S105


def _ready(url: str) -> bool:
    check = postgres_check(url, timeout_seconds=5, expected_revision=migrate.expected_revision())
    return asyncio.run(check())


def _recorded(url: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(url) as connection:
        return connection.execute("SELECT version_num FROM alembic_version").fetchall()


def test_unmigrated_database_is_not_ready(
    fresh_database: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        assert _ready(fresh_database) is False
    assert "database check failed (UndefinedTable)" in caplog.text


def test_migrated_database_is_ready_and_migrating_again_changes_nothing(
    fresh_database: str,
) -> None:
    migrate.upgrade(fresh_database)
    assert _recorded(fresh_database) == [(migrate.expected_revision(),)]
    assert _ready(fresh_database) is True
    migrate.upgrade(fresh_database)
    assert _recorded(fresh_database) == [(migrate.expected_revision(),)]
    assert _ready(fresh_database) is True


def test_downgraded_database_is_not_ready(
    fresh_database: str, caplog: pytest.LogCaptureFixture
) -> None:
    migrate.upgrade(fresh_database)
    command.downgrade(migrate.alembic_config(fresh_database), "base")
    assert _recorded(fresh_database) == []
    with caplog.at_level(logging.WARNING):
        assert _ready(fresh_database) is False
    assert "database schema is not at the expected revision" in caplog.text


def test_database_at_another_revision_is_not_ready(fresh_database: str) -> None:
    migrate.upgrade(fresh_database)
    with psycopg.connect(fresh_database) as connection:
        connection.execute("UPDATE alembic_version SET version_num = 'ffffffffffff'")
    assert _ready(fresh_database) is False


@pytest.mark.parametrize("change", [{"password": WRONG_PASSWORD}, {"database": "sage_missing_db"}])
def test_rejected_connection_is_not_ready_and_not_logged(
    database_url: str, change: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        assert _ready(url_with(database_url, **change)) is False
    assert "database check failed" in caplog.text
    real_password = urlsplit(database_url).password
    assert real_password is not None
    assert real_password not in caplog.text
    assert WRONG_PASSWORD not in caplog.text


def _migrate_command(url: str) -> subprocess.CompletedProcess[str]:
    env = {
        **{k: v for k, v in os.environ.items() if not k.upper().startswith("SAGE_")},
        "DATABASE_URL": url,
        "SAGE_ENCRYPTION_KEY": ENCRYPTION_KEY,
    }
    return subprocess.run(  # noqa: S603 -- fixed argv: this interpreter + our module
        [sys.executable, "-m", "sage_bot.db.migrate"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )


def test_migrate_command_brings_a_fresh_database_to_ready(fresh_database: str) -> None:
    result = _migrate_command(fresh_database)
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"database schema is at revision {migrate.expected_revision()}\n"
    assert _ready(fresh_database) is True


def test_migrate_command_failure_reveals_no_credentials(database_url: str) -> None:
    result = _migrate_command(url_with(database_url, password=WRONG_PASSWORD))
    assert result.returncode == 1
    assert "migration failed (OperationalError)" in result.stderr
    output = result.stdout + result.stderr
    assert WRONG_PASSWORD not in output
    real_password = urlsplit(database_url).password
    assert real_password is not None
    assert real_password not in output


def test_service_is_ready_once_migrated(
    fresh_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", fresh_database)
    monkeypatch.setenv("SAGE_ENCRYPTION_KEY", ENCRYPTION_KEY)
    client = TestClient(create_app(readiness=readiness_from_environment()))
    assert client.get("/health/ready").status_code == 503
    migrate.upgrade(fresh_database)
    response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert client.head("/health/ready").status_code == 200


def test_concurrent_migrate_commands_both_succeed(fresh_database: str) -> None:
    env = {
        **{k: v for k, v in os.environ.items() if not k.upper().startswith("SAGE_")},
        "DATABASE_URL": fresh_database,
        "SAGE_ENCRYPTION_KEY": ENCRYPTION_KEY,
    }
    for _ in range(3):
        runners = [
            subprocess.Popen(  # noqa: S603 -- fixed argv: this interpreter + our module
                [sys.executable, "-m", "sage_bot.db.migrate"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            for _ in range(4)
        ]
        results = [runner.communicate(timeout=60) for runner in runners]
        assert [runner.returncode for runner in runners] == [0] * 4, results
        assert _recorded(fresh_database) == [(migrate.expected_revision(),)]
        command.downgrade(migrate.alembic_config(fresh_database), "base")
        with psycopg.connect(fresh_database, autocommit=True) as connection:
            connection.execute("DROP TABLE alembic_version")


def test_migrate_gives_up_when_another_runner_holds_the_lock(fresh_database: str) -> None:
    from sqlalchemy.exc import OperationalError

    with psycopg.connect(fresh_database, autocommit=True) as holder:
        holder.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK_KEY,))
        started = time.monotonic()
        with pytest.raises(OperationalError):
            migrate.upgrade(fresh_database, lock_timeout_seconds=1)
        assert time.monotonic() - started < 5
    # Once the holder is gone, migration proceeds.
    migrate.upgrade(fresh_database)
    assert _recorded(fresh_database) == [(migrate.expected_revision(),)]
