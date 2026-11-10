"""Workspace persistence and tenant isolation against a real PostgreSQL server.

Each test gets a database owned by a non-superuser role (the trusted provisioning and migration
role) and a separate runtime role with no ownership, superuser or BYPASSRLS. Fixtures are in
conftest.py.
"""

import asyncio
import time
from typing import Any, LiteralString
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from pg_support import TenantDatabase, grant_runtime_access
from psycopg import errors, sql

from sage_bot.api.readiness import postgres_check
from sage_bot.db import migrate
from sage_bot.db.workspaces import (
    PostgresTenantWorkspaces,
    PostgresWorkspaceProvisioning,
    tenant_transaction,
)
from sage_bot.workspaces import Registration, Workspace, WorkspaceStatus

TEAM_A, TEAM_B = "TALPHA0001", "TBRAVO0002"


def _register(url: str, team: str) -> tuple[Workspace, bool]:
    registration = asyncio.run(PostgresWorkspaceProvisioning(url).register(team))
    return registration.workspace, registration.created


def _ready(url: str) -> bool:
    check = postgres_check(url, timeout_seconds=5, expected_revision=migrate.expected_revision())
    return asyncio.run(check())


def _rows(url: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(url) as connection:
        return connection.execute(
            "SELECT slack_team_id, status FROM slack_workspaces ORDER BY slack_team_id"
        ).fetchall()


# Migration and readiness


def test_fresh_migration_reaches_head_and_is_ready(tenant_database: TenantDatabase) -> None:
    assert _ready(tenant_database.runtime_url) is False
    migrate.upgrade(tenant_database.owner_url)
    grant_runtime_access(tenant_database.owner_url, tenant_database.runtime_role)
    assert _ready(tenant_database.runtime_url) is True
    assert _rows(tenant_database.owner_url) == []


def test_upgrade_from_the_baseline_and_readiness_around_it(
    tenant_database: TenantDatabase,
) -> None:
    config = migrate.alembic_config(tenant_database.owner_url)
    command.upgrade(config, "0001")
    with psycopg.connect(tenant_database.owner_url, autocommit=True) as owner:
        owner.execute(
            sql.SQL("GRANT SELECT ON alembic_version TO {}").format(
                sql.Identifier(tenant_database.runtime_role)
            )
        )
    # At the merged baseline the database is behind this release, so it is not ready.
    assert _ready(tenant_database.runtime_url) is False
    migrate.upgrade(tenant_database.owner_url)
    grant_runtime_access(tenant_database.owner_url, tenant_database.runtime_role)
    assert _ready(tenant_database.runtime_url) is True
    workspace, created = _register(tenant_database.owner_url, TEAM_A)
    assert created is True
    assert workspace.status is WorkspaceStatus.DISABLED
    # Downgrading back to the baseline removes the table and readiness follows.
    command.downgrade(config, "0001")
    assert _ready(tenant_database.runtime_url) is False
    with psycopg.connect(tenant_database.owner_url) as connection:
        assert connection.execute("SELECT to_regclass('slack_workspaces')").fetchone() == (None,)


def test_workspace_table_is_protected_by_row_level_security(migrated: TenantDatabase) -> None:
    with psycopg.connect(migrated.admin_url) as connection:
        row = connection.execute(
            """SELECT c.relrowsecurity, c.relforcerowsecurity, pg_get_userbyid(c.relowner)
               FROM pg_class c WHERE c.oid = 'slack_workspaces'::regclass"""
        ).fetchone()
        policies = connection.execute(
            "SELECT policyname, cmd FROM pg_policies WHERE tablename = 'slack_workspaces'"
        ).fetchall()
    assert row is not None
    enabled, forced, owner = row
    assert enabled is True
    # Not forced: the owning role is the trusted provisioning role (see sage_bot.db.workspaces).
    assert forced is False
    assert owner == migrated.owner_role
    assert policies == [("tenant_isolation", "ALL")]


# Registration


def test_registration_creates_a_disabled_workspace_that_can_be_retrieved(
    migrated: TenantDatabase,
) -> None:
    provisioning = PostgresWorkspaceProvisioning(migrated.owner_url)
    registration = asyncio.run(provisioning.register(TEAM_A))
    workspace = registration.workspace
    assert registration.created is True
    assert isinstance(workspace.id, UUID)
    assert workspace.slack_team_id == TEAM_A
    assert workspace.status is WorkspaceStatus.DISABLED
    assert workspace.created_at.tzinfo is not None
    assert asyncio.run(provisioning.find_by_slack_team(TEAM_A)) == workspace
    assert asyncio.run(provisioning.find_by_slack_team(TEAM_B)) is None
    tenant = PostgresTenantWorkspaces(migrated.runtime_url)
    assert asyncio.run(tenant.get(workspace.id)) == workspace


def test_repeated_registration_keeps_one_identity_and_never_changes_status(
    migrated: TenantDatabase,
) -> None:
    first, created = _register(migrated.owner_url, TEAM_A)
    again, created_again = _register(migrated.owner_url, TEAM_A)
    assert (created, created_again) == (True, False)
    assert again == first
    # An operator enables the workspace; registering again must neither reset nor change it.
    with psycopg.connect(migrated.owner_url) as connection:
        connection.execute(
            "UPDATE slack_workspaces SET status = 'enabled' WHERE id = %s", (first.id,)
        )
    enabled, _ = _register(migrated.owner_url, TEAM_A)
    assert (enabled.id, enabled.status) == (first.id, WorkspaceStatus.ENABLED)
    other, _ = _register(migrated.owner_url, TEAM_B)
    assert other.id != first.id
    assert other.status is WorkspaceStatus.DISABLED
    assert _rows(migrated.owner_url) == [(TEAM_A, "enabled"), (TEAM_B, "disabled")]


# Registration must behave the same whatever isolation level the server defaults to.
ISOLATION_LEVELS = pytest.mark.parametrize(
    "isolation", ["read committed", "repeatable read", "serializable"]
)


def _default_isolation(database: TenantDatabase, isolation: str) -> None:
    with psycopg.connect(database.owner_url, autocommit=True) as owner:
        owner.execute(
            sql.SQL("ALTER DATABASE {} SET default_transaction_isolation = {}").format(
                sql.Identifier(database.name), sql.Literal(isolation)
            )
        )


@ISOLATION_LEVELS
def test_concurrent_registration_creates_exactly_one_workspace(
    migrated: TenantDatabase, isolation: str
) -> None:
    _default_isolation(migrated, isolation)
    provisioning = PostgresWorkspaceProvisioning(migrated.owner_url)

    async def register_many() -> list[tuple[Workspace, bool]]:
        # Let every attempt finish: a cancelled connection attempt lingers on the server.
        outcomes = await asyncio.gather(
            *(provisioning.register(TEAM_A) for _ in range(16)), return_exceptions=True
        )
        registrations = [r for r in outcomes if isinstance(r, Registration)]
        assert len(registrations) == len(outcomes), outcomes
        return [(r.workspace, r.created) for r in registrations]

    results = asyncio.run(register_many())
    assert sum(created for _, created in results) == 1
    assert len({workspace for workspace, _ in results}) == 1
    assert _rows(migrated.owner_url) == [(TEAM_A, "disabled")]


@ISOLATION_LEVELS
def test_registration_racing_an_uncommitted_insert_returns_the_committed_identity(
    migrated: TenantDatabase, isolation: str
) -> None:
    """Registrations that block on a concurrent insert return that row once it commits."""
    _default_isolation(migrated, isolation)
    provisioning = PostgresWorkspaceProvisioning(migrated.owner_url)

    async def race(blocker: psycopg.Connection, monitor: psycopg.Connection) -> list[bool]:
        tasks = [asyncio.create_task(provisioning.register(TEAM_A)) for _ in range(4)]
        deadline = time.monotonic() + 10
        waiting = 0
        while time.monotonic() < deadline:
            row = monitor.execute(
                """SELECT count(*) FROM pg_stat_activity
                   WHERE datname = current_database() AND wait_event_type = 'Lock'"""
            ).fetchone()
            waiting = row[0] if row is not None else 0
            if waiting == len(tasks):
                break
            await asyncio.sleep(0.02)
        blocker.commit()
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        assert waiting == len(tasks), "registrations did not block on the uncommitted insert"
        registrations = [r for r in outcomes if isinstance(r, Registration)]
        assert len(registrations) == len(outcomes), outcomes
        assert {r.workspace.id for r in registrations} == {blocked_id}
        return [r.created for r in registrations]

    with (
        psycopg.connect(migrated.owner_url) as blocker,
        psycopg.connect(migrated.owner_url, autocommit=True) as monitor,
    ):
        row = blocker.execute(
            "INSERT INTO slack_workspaces (slack_team_id) VALUES (%s) RETURNING id", (TEAM_A,)
        ).fetchone()
        assert row is not None
        blocked_id = row[0]
        assert asyncio.run(race(blocker, monitor)) == [False] * 4
    assert _rows(migrated.owner_url) == [(TEAM_A, "disabled")]


def test_status_defaults_to_disabled_and_is_constrained(migrated: TenantDatabase) -> None:
    with psycopg.connect(migrated.owner_url, autocommit=True) as connection:
        row = connection.execute(
            "INSERT INTO slack_workspaces (slack_team_id) VALUES (%s) RETURNING status", (TEAM_A,)
        ).fetchone()
        assert row == ("disabled",)
        with pytest.raises(errors.CheckViolation):
            connection.execute(
                "INSERT INTO slack_workspaces (slack_team_id, status) VALUES (%s, 'active')",
                (TEAM_B,),
            )
        with pytest.raises(errors.CheckViolation):
            connection.execute("INSERT INTO slack_workspaces (slack_team_id) VALUES ('not-a-team')")


# Tenant isolation, as the restricted runtime role


def _as_runtime(migrated: TenantDatabase, workspace_id: UUID | None) -> psycopg.Connection:
    connection = psycopg.connect(migrated.runtime_url)
    if workspace_id is not None:
        connection.execute("SELECT set_config('sage.workspace_id', %s, true)", (str(workspace_id),))
    return connection


def test_runtime_role_cannot_bypass_row_level_security(migrated: TenantDatabase) -> None:
    with psycopg.connect(migrated.admin_url) as connection:
        row = connection.execute(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = %s",
            (migrated.runtime_role,),
        ).fetchone()
    assert row == (False, False)


def test_tenant_sees_only_its_own_workspace(migrated: TenantDatabase) -> None:
    a, _ = _register(migrated.owner_url, TEAM_A)
    b, _ = _register(migrated.owner_url, TEAM_B)
    with _as_runtime(migrated, a.id) as connection:
        assert connection.execute("SELECT id FROM slack_workspaces").fetchall() == [(a.id,)]
        assert (
            connection.execute("SELECT id FROM slack_workspaces WHERE id = %s", (b.id,)).fetchall()
            == []
        )
        assert (
            connection.execute(
                "SELECT id FROM slack_workspaces WHERE slack_team_id = %s", (TEAM_B,)
            ).fetchall()
            == []
        )
    tenant = PostgresTenantWorkspaces(migrated.runtime_url)
    assert asyncio.run(tenant.get(a.id)) == a
    assert asyncio.run(tenant.get(b.id)) == b
    assert asyncio.run(tenant.get(uuid4())) is None


def test_no_tenant_context_sees_no_workspaces(migrated: TenantDatabase) -> None:
    _register(migrated.owner_url, TEAM_A)
    with _as_runtime(migrated, None) as connection:
        assert connection.execute("SELECT count(*) FROM slack_workspaces").fetchone() == (0,)
        connection.execute("SELECT set_config('sage.workspace_id', '', true)")
        assert connection.execute("SELECT count(*) FROM slack_workspaces").fetchone() == (0,)


def test_runtime_role_cannot_write_workspaces(migrated: TenantDatabase) -> None:
    a, _ = _register(migrated.owner_url, TEAM_A)
    statements: list[tuple[LiteralString, tuple[object, ...]]] = [
        ("UPDATE slack_workspaces SET status = 'enabled' WHERE id = %s", (a.id,)),
        ("DELETE FROM slack_workspaces WHERE id = %s", (a.id,)),
        ("INSERT INTO slack_workspaces (id, slack_team_id) VALUES (%s, %s)", (a.id, TEAM_B)),
    ]
    for statement, params in statements:
        with (
            _as_runtime(migrated, a.id) as connection,
            pytest.raises(errors.InsufficientPrivilege),
        ):
            connection.execute(statement, params)
    assert _rows(migrated.owner_url) == [(TEAM_A, "disabled")]


def test_isolation_holds_even_if_the_runtime_role_is_granted_writes(
    migrated: TenantDatabase,
) -> None:
    """Row-level security, not only missing privileges, rejects cross-workspace writes."""
    a, _ = _register(migrated.owner_url, TEAM_A)
    b, _ = _register(migrated.owner_url, TEAM_B)
    with psycopg.connect(migrated.owner_url, autocommit=True) as owner:
        owner.execute(
            sql.SQL("GRANT INSERT, UPDATE, DELETE ON slack_workspaces TO {}").format(
                sql.Identifier(migrated.runtime_role)
            )
        )
    with _as_runtime(migrated, a.id) as connection:
        cursor = connection.execute(
            "UPDATE slack_workspaces SET status = 'enabled' WHERE id = %s", (b.id,)
        )
        assert cursor.rowcount == 0
        assert (
            connection.execute("DELETE FROM slack_workspaces WHERE id = %s", (b.id,)).rowcount == 0
        )
    with _as_runtime(migrated, a.id) as connection, pytest.raises(errors.InsufficientPrivilege):
        connection.execute(
            "INSERT INTO slack_workspaces (id, slack_team_id) VALUES (%s, %s)",
            (uuid4(), "TCHARLIE003"),
        )
    with _as_runtime(migrated, a.id) as connection, pytest.raises(errors.InsufficientPrivilege):
        connection.execute("UPDATE slack_workspaces SET id = %s WHERE id = %s", (b.id, a.id))
    assert _rows(migrated.owner_url) == [(TEAM_A, "disabled"), (TEAM_B, "disabled")]


@pytest.mark.parametrize(
    "statement",
    [
        "ALTER TABLE slack_workspaces DISABLE ROW LEVEL SECURITY",
        "ALTER TABLE slack_workspaces NO FORCE ROW LEVEL SECURITY",
        "DROP POLICY tenant_isolation ON slack_workspaces",
        "CREATE POLICY open_access ON slack_workspaces USING (true)",
    ],
)
def test_runtime_role_cannot_alter_the_isolation_controls(
    migrated: TenantDatabase, statement: LiteralString
) -> None:
    with _as_runtime(migrated, None) as connection, pytest.raises(errors.InsufficientPrivilege):
        connection.execute(statement)


def test_runtime_role_cannot_switch_row_level_security_off(migrated: TenantDatabase) -> None:
    _register(migrated.owner_url, TEAM_A)
    with _as_runtime(migrated, None) as connection, pytest.raises(errors.InsufficientPrivilege):
        connection.execute("SET row_security = off")
        connection.execute("SELECT count(*) FROM slack_workspaces")


def test_runtime_role_cannot_provision_workspaces(migrated: TenantDatabase) -> None:
    provisioning = PostgresWorkspaceProvisioning(migrated.runtime_url)
    with pytest.raises(errors.InsufficientPrivilege):
        asyncio.run(provisioning.register(TEAM_A))
    _register(migrated.owner_url, TEAM_A)
    # Without a tenant, the runtime role cannot read a team's workspace from the table either;
    # it resolves enabled teams only through resolve_slack_workspace.
    assert asyncio.run(provisioning.find_by_slack_team(TEAM_A)) is None


# The tenant transaction helper


def test_tenant_context_ends_with_the_transaction(migrated: TenantDatabase) -> None:
    a, _ = _register(migrated.owner_url, TEAM_A)

    async def scenario() -> tuple[object, object]:
        async with await psycopg.AsyncConnection.connect(migrated.runtime_url) as connection:
            async with tenant_transaction(connection, a.id):
                inside = await (
                    await connection.execute("SELECT current_setting('sage.workspace_id', true)")
                ).fetchone()
            after = await (
                await connection.execute("SELECT current_setting('sage.workspace_id', true)")
            ).fetchone()
            await connection.rollback()
            return inside, after

    inside, after = asyncio.run(scenario())
    assert inside == (str(a.id),)
    assert after in {("",), (None,)}


def test_tenant_transaction_refuses_a_connection_already_bound_to_a_workspace(
    migrated: TenantDatabase,
) -> None:
    a, _ = _register(migrated.owner_url, TEAM_A)
    b, _ = _register(migrated.owner_url, TEAM_B)

    async def scenario() -> None:
        async with await psycopg.AsyncConnection.connect(
            migrated.runtime_url, autocommit=True
        ) as connection:
            await connection.execute(
                "SELECT set_config('sage.workspace_id', %s, false)", (str(b.id),)
            )
            async with tenant_transaction(connection, a.id):
                pass

    with pytest.raises(RuntimeError, match="already bound"):
        asyncio.run(scenario())


def test_tenant_transaction_refuses_to_join_an_open_transaction(migrated: TenantDatabase) -> None:
    a, _ = _register(migrated.owner_url, TEAM_A)

    async def scenario() -> None:
        async with await psycopg.AsyncConnection.connect(migrated.runtime_url) as connection:
            await connection.execute("SELECT 1")  # opens a transaction
            async with tenant_transaction(connection, a.id):
                pass

    with pytest.raises(RuntimeError, match="open transaction"):
        asyncio.run(scenario())
