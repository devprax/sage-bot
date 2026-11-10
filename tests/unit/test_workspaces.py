import asyncio
import socket
import time
from datetime import UTC, datetime
from importlib.resources import files
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from alembic.script import ScriptDirectory
from fake_postgres import fake_postgres

from sage_bot.db import migrate
from sage_bot.db.workspaces import (
    PostgresTenantWorkspaces,
    PostgresWorkspaceProvisioning,
    PostgresWorkspaceResolver,
    tenant_transaction,
)
from sage_bot.workspaces import (
    Registration,
    TenantWorkspaces,
    Workspace,
    WorkspaceLookupUnavailable,
    WorkspaceProvisioning,
    WorkspaceResolver,
    WorkspaceStatus,
    validate_slack_team_id,
)


@pytest.mark.parametrize("team", ["T0", "T024BE7LD", "TABCDEFGHIJKLMNOPQRSTUVWXYZ01234"])
def test_valid_slack_team_ids_are_accepted(team: str) -> None:
    assert validate_slack_team_id(team) == team


@pytest.mark.parametrize(
    "team",
    [
        "",
        "T",
        "t024be7ld",
        "E024BE7LD",
        "U024BE7LD",
        " T024BE7LD",
        "T024BE7LD\n",
        "T024-BE7LD",
        "TABCDEFGHIJKLMNOPQRSTUVWXYZ012345",
    ],
)
def test_invalid_slack_team_ids_are_rejected_without_echoing_them(team: str) -> None:
    with pytest.raises(ValueError, match="invalid Slack team ID") as raised:
        validate_slack_team_id(team)
    if team.strip():
        assert team.strip() not in str(raised.value)


def test_registration_rejects_an_invalid_team_before_connecting() -> None:
    # The URL points nowhere; validation must fail first.
    provisioning = PostgresWorkspaceProvisioning("postgresql://nobody@127.0.0.1:1/none")
    with pytest.raises(ValueError, match="invalid Slack team ID"):
        asyncio.run(provisioning.register("not-a-team"))
    with pytest.raises(ValueError, match="invalid Slack team ID"):
        asyncio.run(provisioning.find_by_slack_team("not-a-team"))


def test_workspace_statuses_are_disabled_and_enabled() -> None:
    assert WorkspaceStatus.DISABLED.value == "disabled"
    assert {status.value for status in WorkspaceStatus} == {"disabled", "enabled"}


def test_workspace_records_are_immutable_values() -> None:
    workspace = Workspace(
        id=uuid4(),
        slack_team_id="T024BE7LD",
        status=WorkspaceStatus.DISABLED,
        created_at=datetime.now(UTC),
    )
    registration = Registration(workspace=workspace, created=True)
    with pytest.raises(AttributeError):
        workspace.status = WorkspaceStatus.ENABLED  # ty: ignore[invalid-assignment]
    assert registration.workspace is workspace


def test_postgres_implementations_satisfy_the_interfaces() -> None:
    provisioning: WorkspaceProvisioning = PostgresWorkspaceProvisioning("postgresql://x@h/d")
    tenant: TenantWorkspaces = PostgresTenantWorkspaces("postgresql://x@h/d")
    resolver: WorkspaceResolver = PostgresWorkspaceResolver("postgresql://x@h/d")
    assert provisioning is not None
    assert tenant is not None
    assert resolver is not None


def test_tenant_transaction_requires_a_uuid() -> None:
    async def scenario() -> None:
        connection: Any = None
        workspace_id: Any = "not-a-uuid"
        async with tenant_transaction(connection, workspace_id):
            pass

    with pytest.raises(TypeError, match="UUID"):
        asyncio.run(scenario())


def test_workspace_migrations_follow_the_baseline() -> None:
    scripts = ScriptDirectory.from_config(migrate.alembic_config())
    workspaces = scripts.get_revision("0002")
    assert workspaces is not None
    assert workspaces.down_revision == "0001"
    resolution = scripts.get_revision(migrate.expected_revision())
    assert resolution is not None
    assert resolution.revision == "0003"
    assert resolution.down_revision == "0002"


def _refuse_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    async def connect(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the lookup connected")

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", connect)


@pytest.mark.parametrize("team", ["", "t123", "T' OR true --", "U12345", "T" + "A" * 32])
def test_resolver_rejects_a_malformed_team_before_connecting(
    team: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse_connections(monkeypatch)
    resolver = PostgresWorkspaceResolver("postgresql://sage@127.0.0.1:1/sage")
    with pytest.raises(ValueError, match="invalid Slack team ID") as error:
        asyncio.run(resolver.resolve_enabled(team))
    assert str(error.value) == "invalid Slack team ID"


@pytest.mark.parametrize("seconds", [0, -1, float("nan"), float("inf")])
def test_resolver_requires_a_positive_finite_timeout(seconds: float) -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        PostgresWorkspaceResolver("postgresql://sage@127.0.0.1:1/sage", timeout_seconds=seconds)


def test_an_unreachable_database_is_unavailable_without_details() -> None:
    url = "postgresql://sage:unit-secret@127.0.0.1:1/sage"
    resolver = PostgresWorkspaceResolver(url, timeout_seconds=1)
    with pytest.raises(WorkspaceLookupUnavailable) as failure:
        asyncio.run(resolver.resolve_enabled("TALPHA0001"))
    assert str(failure.value) == "workspace lookup failed (OperationalError)"
    assert "unit-secret" not in str(failure.value)
    assert failure.value.__cause__ is None
    assert failure.value.__context__ is None


def test_the_resolution_migration_ships_inside_the_package() -> None:
    versions = files("sage_bot.db") / "migrations" / "versions"
    assert (versions / "0003_resolve_slack_workspace.py").is_file()


def test_a_lookup_stalled_after_the_handshake_is_bounded_without_cancelling() -> None:
    with fake_postgres() as server:
        resolver = PostgresWorkspaceResolver(
            f"postgresql://sage:unit-secret@127.0.0.1:{server.port}/sage", timeout_seconds=0.5
        )

        async def lookup() -> tuple[float, int]:
            before = asyncio.all_tasks()
            started = time.monotonic()
            with pytest.raises(WorkspaceLookupUnavailable, match="TimeoutError"):
                await resolver.resolve_enabled("TALPHA0001")
            return time.monotonic() - started, len(asyncio.all_tasks() - before)

        elapsed, leftover = asyncio.run(asyncio.wait_for(lookup(), timeout=5))
        assert elapsed < 1.5
        assert leftover == 0
        # Closed promptly, and never through psycopg's cancel protocol, whose waiting the
        # deadline would not bound.
        assert server.wait_until_closed(timeout=1.0)
        assert server.cancel_requests == 0


def test_a_lookup_that_cannot_connect_is_bounded() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)  # accepts at the TCP level, never answers the startup message
        port = listener.getsockname()[1]
        resolver = PostgresWorkspaceResolver(
            f"postgresql://sage:unit-secret@127.0.0.1:{port}/sage", timeout_seconds=0.5
        )
        started = time.monotonic()
        with pytest.raises(WorkspaceLookupUnavailable, match="TimeoutError"):
            asyncio.run(asyncio.wait_for(resolver.resolve_enabled("TALPHA0001"), timeout=5))
        assert time.monotonic() - started < 1.5
