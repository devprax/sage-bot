"""Resolving a Slack team to an enabled workspace, as the restricted runtime role.

The lookup goes through `resolve_slack_workspace(text)`, a SECURITY DEFINER function owned by
the migration owner. Fixtures are in conftest.py.
"""

import asyncio
import logging
import time
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any, LiteralString
from urllib.parse import urlsplit
from uuid import UUID

import psycopg
import pytest
from alembic import command
from pg_support import TenantDatabase, grant_runtime_access, url_with
from psycopg import errors, sql

from sage_bot.api.readiness import postgres_check
from sage_bot.db import migrate
from sage_bot.db.workspaces import (
    APPLICATION_NAME,
    PostgresWorkspaceProvisioning,
    PostgresWorkspaceResolver,
)
from sage_bot.workspaces import WorkspaceLookupUnavailable, WorkspaceResolver

TEAM_A, TEAM_B, TEAM_UNKNOWN = "TALPHA0001", "TBRAVO0002", "TUNKNOWN03"
RESOLVER = "resolve_slack_workspace(text)"
UNSAFE = "readiness: runtime database role is unsafe"


def _ready(url: str) -> bool:
    check = postgres_check(url, timeout_seconds=5, expected_revision=migrate.expected_revision())
    return asyncio.run(check())


def _register(url: str, team: str, *, enabled: bool) -> UUID:
    workspace = asyncio.run(PostgresWorkspaceProvisioning(url).register(team)).workspace
    if enabled:
        with psycopg.connect(url, autocommit=True) as owner:
            owner.execute(
                "UPDATE slack_workspaces SET status = 'enabled' WHERE id = %s", (workspace.id,)
            )
    return workspace.id


def _resolve(url: str, team: str, **options: Any) -> UUID | None:
    resolver: WorkspaceResolver = PostgresWorkspaceResolver(url, **options)
    return asyncio.run(resolver.resolve_enabled(team))


def _statuses(url: str) -> list[tuple[str, str]]:
    with psycopg.connect(url) as connection:
        rows = connection.execute(
            "SELECT slack_team_id, status FROM slack_workspaces ORDER BY slack_team_id"
        ).fetchall()
    return [(row[0], row[1]) for row in rows]


def _admin(database: TenantDatabase, statement: LiteralString, **names: str) -> None:
    identifiers = {key: sql.Identifier(value) for key, value in names.items()}
    with psycopg.connect(database.admin_url, autocommit=True) as admin:
        admin.execute(sql.SQL(statement).format(**identifiers))


def _runtime_backends(database: TenantDatabase) -> int:
    with psycopg.connect(database.admin_url, autocommit=True) as admin:
        row = admin.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE usename = %s AND application_name = %s",
            (database.runtime_role, APPLICATION_NAME),
        ).fetchone()
    assert row is not None
    count = row[0]
    assert isinstance(count, int)
    return count


def _wait_for_no_runtime_backends(database: TenantDatabase, seconds: float = 5.0) -> None:
    deadline = time.monotonic() + seconds
    while _runtime_backends(database):
        assert time.monotonic() < deadline, "a lookup's backend outlived the lookup"
        time.sleep(0.05)


@contextmanager
def _workspaces_locked(database: TenantDatabase) -> Generator[None]:
    """Hold a lock that makes every read of slack_workspaces wait."""
    with psycopg.connect(database.owner_url) as owner:
        owner.execute("LOCK TABLE slack_workspaces IN ACCESS EXCLUSIVE MODE")
        try:
            yield
        finally:
            owner.rollback()


# Resolution


def test_enabled_team_resolves_and_unknown_or_disabled_teams_do_not(
    migrated: TenantDatabase,
) -> None:
    enabled = _register(migrated.owner_url, TEAM_A, enabled=True)
    _register(migrated.owner_url, TEAM_B, enabled=False)
    before = _statuses(migrated.owner_url)
    # As the restricted runtime role, with no tenant binding.
    assert _resolve(migrated.runtime_url, TEAM_A) == enabled
    assert _resolve(migrated.runtime_url, TEAM_B) is None
    assert _resolve(migrated.runtime_url, TEAM_UNKNOWN) is None
    # It never registers or enables a workspace.
    assert _statuses(migrated.owner_url) == before


def test_the_function_returns_only_the_workspace_id(migrated: TenantDatabase) -> None:
    with psycopg.connect(migrated.admin_url) as admin:
        row = admin.execute(
            """SELECT p.prorettype::regtype::text, p.proretset, p.prosecdef, p.provolatile,
                      p.proisstrict, p.proconfig, pg_get_userbyid(p.proowner), p.prokind
               FROM pg_proc p WHERE p.oid = %s::regprocedure""",
            (RESOLVER,),
        ).fetchone()
    assert row == (
        "uuid",
        False,
        True,
        "s",
        True,
        ["search_path=pg_catalog, pg_temp"],
        migrated.owner_role,
        "f",
    )


def test_direct_reads_stay_constrained_by_row_level_security(migrated: TenantDatabase) -> None:
    a = _register(migrated.owner_url, TEAM_A, enabled=True)
    b = _register(migrated.owner_url, TEAM_B, enabled=True)
    with psycopg.connect(migrated.runtime_url) as runtime:
        assert runtime.execute(
            "SELECT public.resolve_slack_workspace(%s)", (TEAM_A,)
        ).fetchone() == (a,)
        # Resolving gives the session no access to the rows themselves.
        assert runtime.execute("SELECT count(*) FROM slack_workspaces").fetchone() == (0,)
        runtime.execute("SELECT set_config('sage.workspace_id', %s, true)", (str(a),))
        assert runtime.execute("SELECT id FROM slack_workspaces").fetchall() == [(a,)]
        assert (
            runtime.execute("SELECT id FROM slack_workspaces WHERE id = %s", (b,)).fetchall() == []
        )


def test_public_cannot_execute_the_function(migrated: TenantDatabase) -> None:
    _register(migrated.owner_url, TEAM_A, enabled=True)
    with psycopg.connect(migrated.admin_url) as admin:
        row = admin.execute(
            """SELECT count(*) FROM pg_proc p, aclexplode(p.proacl) a
               WHERE p.oid = %s::regprocedure AND a.grantee = 0""",
            (RESOLVER,),
        ).fetchone()
        acl = admin.execute(
            "SELECT proacl IS NOT NULL FROM pg_proc WHERE oid = %s::regprocedure", (RESOLVER,)
        ).fetchone()
    assert row == (0,)
    assert acl == (True,)  # an explicit ACL, so PostgreSQL's PUBLIC default does not apply
    # A role that holds only what PUBLIC holds.
    outsider = f"{migrated.runtime_role}_outsider"
    password = "outsider-password"  # noqa: S105
    with psycopg.connect(migrated.admin_url, autocommit=True) as admin:
        admin.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(outsider), sql.Literal(password)
            )
        )
    try:
        url = url_with(migrated.runtime_url, user=outsider, password=password)
        with (
            psycopg.connect(url) as connection,
            pytest.raises(errors.InsufficientPrivilege, match="permission denied for function"),
        ):
            connection.execute("SELECT public.resolve_slack_workspace(%s)", (TEAM_A,))
        with pytest.raises(WorkspaceLookupUnavailable):
            _resolve(url, TEAM_A)
    finally:
        _admin(migrated, "DROP ROLE {role}", role=outsider)


def test_a_caller_search_path_cannot_substitute_objects(migrated: TenantDatabase) -> None:
    enabled = _register(migrated.owner_url, TEAM_A, enabled=True)
    _admin(migrated, "CREATE SCHEMA shadow")
    _admin(
        migrated,
        """CREATE TABLE shadow.slack_workspaces AS
           SELECT gen_random_uuid() AS id, slack_team_id, 'enabled'::text AS status
           FROM (VALUES ('TALPHA0001'), ('TUNKNOWN03')) AS teams (slack_team_id)""",
    )
    # A text equality operator that matches everything.
    _admin(
        migrated,
        """CREATE FUNCTION shadow.always(text, text) RETURNS boolean
           LANGUAGE sql IMMUTABLE AS 'SELECT true'""",
    )
    _admin(
        migrated,
        "CREATE OPERATOR shadow.= (LEFTARG = text, RIGHTARG = text, FUNCTION = shadow.always)",
    )
    _admin(migrated, "GRANT USAGE ON SCHEMA shadow TO {rt}", rt=migrated.runtime_role)
    _admin(migrated, "GRANT SELECT ON shadow.slack_workspaces TO {rt}", rt=migrated.runtime_role)
    url = f"{migrated.runtime_url}?options=-csearch_path%3Dshadow,public"
    assert _resolve(url, TEAM_A) == enabled
    assert _resolve(url, TEAM_UNKNOWN) is None
    # Nor can a temporary table, which the session's search path consults first for relations.
    with psycopg.connect(migrated.runtime_url) as runtime:
        runtime.execute(
            """CREATE TEMPORARY TABLE slack_workspaces AS
               SELECT gen_random_uuid() AS id, 'TUNKNOWN03'::text AS slack_team_id,
                      'enabled'::text AS status"""
        )
        assert runtime.execute(
            "SELECT public.resolve_slack_workspace(%s)", (TEAM_UNKNOWN,)
        ).fetchone() == (None,)
        assert runtime.execute(
            "SELECT public.resolve_slack_workspace(%s)", (TEAM_A,)
        ).fetchone() == (enabled,)


# Failures


def test_invalid_input_is_rejected_before_connecting(migrated: TenantDatabase) -> None:
    with pytest.raises(ValueError, match="invalid Slack team ID"):
        _resolve(migrated.runtime_url, "T' OR true --")
    assert _runtime_backends(migrated) == 0


def test_database_failures_are_unavailable_not_unknown(
    migrated: TenantDatabase, caplog: pytest.LogCaptureFixture
) -> None:
    wrong = url_with(migrated.runtime_url, password="wrong-password-for-test")  # noqa: S106
    with caplog.at_level(logging.DEBUG), pytest.raises(WorkspaceLookupUnavailable) as failure:
        _resolve(wrong, TEAM_A)
    assert str(failure.value) == "workspace lookup failed (OperationalError)"
    assert failure.value.__cause__ is None
    assert failure.value.__context__ is None
    for secret in ("wrong-password-for-test", wrong, migrated.runtime_url):
        assert secret not in str(failure.value)
        assert secret not in caplog.text


def test_a_lookup_without_the_grant_is_unavailable(tenant_database: TenantDatabase) -> None:
    migrate.upgrade(tenant_database.owner_url)
    with pytest.raises(WorkspaceLookupUnavailable, match="InsufficientPrivilege"):
        _resolve(tenant_database.runtime_url, TEAM_A)


def test_a_stalled_lookup_times_out_and_releases_its_backend(migrated: TenantDatabase) -> None:
    _register(migrated.owner_url, TEAM_A, enabled=True)
    with _workspaces_locked(migrated):
        started = time.monotonic()
        with pytest.raises(WorkspaceLookupUnavailable, match="TimeoutError"):
            _resolve(migrated.runtime_url, TEAM_A, timeout_seconds=0.5)
        assert time.monotonic() - started < 2.0
        # The server abandons the query too, though the lock is still held.
        _wait_for_no_runtime_backends(migrated)


def test_a_lower_deployment_statement_timeout_is_kept(migrated: TenantDatabase) -> None:
    _register(migrated.owner_url, TEAM_A, enabled=True)
    _admin(migrated, "ALTER ROLE {rt} SET statement_timeout = 100", rt=migrated.runtime_role)
    with _workspaces_locked(migrated):
        started = time.monotonic()
        with pytest.raises(WorkspaceLookupUnavailable, match="QueryCanceled"):
            _resolve(migrated.runtime_url, TEAM_A, timeout_seconds=5)
        assert time.monotonic() - started < 1.5


def test_a_cancelled_lookup_releases_its_connection(migrated: TenantDatabase) -> None:
    _register(migrated.owner_url, TEAM_A, enabled=True)
    resolver = PostgresWorkspaceResolver(migrated.runtime_url, timeout_seconds=30)

    async def cancel_while_waiting() -> None:
        lookup = asyncio.create_task(resolver.resolve_enabled(TEAM_A))
        # Polls the server's state; there is no in-process event to wait on.
        while not _runtime_backends(migrated):  # noqa: ASYNC110
            await asyncio.sleep(0.02)
        lookup.cancel()
        with pytest.raises(asyncio.CancelledError):
            await lookup

    with _workspaces_locked(migrated):
        asyncio.run(cancel_while_waiting())
    _wait_for_no_runtime_backends(migrated)


def test_a_completed_lookup_leaves_no_connection(migrated: TenantDatabase) -> None:
    _register(migrated.owner_url, TEAM_A, enabled=True)
    _resolve(migrated.runtime_url, TEAM_A)
    _resolve(migrated.runtime_url, TEAM_UNKNOWN)
    _wait_for_no_runtime_backends(migrated, seconds=1.0)


def test_url_options_are_kept(migrated: TenantDatabase) -> None:
    """The lookup sets its timeout per session; it does not replace the URL's options."""
    _register(migrated.owner_url, TEAM_A, enabled=True)
    _admin(migrated, "CREATE SCHEMA elsewhere")
    _admin(migrated, "GRANT USAGE ON SCHEMA elsewhere TO {rt}", rt=migrated.runtime_role)
    # A search path without the application schema: the function is not found.
    url = f"{migrated.runtime_url}?options=-csearch_path%3Delsewhere"
    with pytest.raises(WorkspaceLookupUnavailable, match="UndefinedFunction"):
        _resolve(url, TEAM_A)


def test_a_function_planted_earlier_on_the_search_path_is_reported(
    tenant_database: TenantDatabase, caplog: pytest.LogCaptureFixture
) -> None:
    """With the application schema behind another one on the runtime's search path, a role
    that can create functions there could substitute the call. Readiness reports it."""
    database = tenant_database
    owner_url = f"{database.owner_url}?options=-csearch_path%3Dsage"
    with psycopg.connect(database.owner_url, autocommit=True) as owner:
        owner.execute("CREATE SCHEMA sage")
    migrate.upgrade(owner_url)
    grant_runtime_access(owner_url, database.runtime_role)
    with psycopg.connect(owner_url, autocommit=True) as owner:
        owner.execute(
            sql.SQL("GRANT USAGE ON SCHEMA sage TO {}").format(
                sql.Identifier(database.runtime_role)
            )
        )
    name = urlsplit(database.runtime_url).path.lstrip("/")
    _admin(
        database,
        "ALTER ROLE {rt} IN DATABASE {db} SET search_path = public, sage",
        rt=database.runtime_role,
        db=name,
    )
    enabled = _register(owner_url, TEAM_A, enabled=True)
    assert _resolve(database.runtime_url, TEAM_A) == enabled
    assert _ready(database.runtime_url) is True
    _admin(
        database,
        """CREATE FUNCTION public.resolve_slack_workspace(text) RETURNS uuid
           LANGUAGE sql AS $$SELECT '00000000-0000-0000-0000-0000000000ff'::uuid$$""",
    )
    # The substitute answers the application's call...
    assert _resolve(database.runtime_url, TEAM_UNKNOWN) == UUID(int=0xFF)
    # ...and readiness reports it.
    with caplog.at_level(logging.WARNING):
        assert _ready(database.runtime_url) is False
    assert "resolver_shadowed" in caplog.text.split(UNSAFE, 1)[1].splitlines()[0]


def test_the_migration_refuses_tables_split_across_schemas(
    tenant_database: TenantDatabase,
) -> None:
    command.upgrade(migrate.alembic_config(tenant_database.owner_url), "0002")
    with psycopg.connect(tenant_database.owner_url, autocommit=True) as owner:
        owner.execute("CREATE SCHEMA elsewhere")
        owner.execute("CREATE TABLE elsewhere.slack_workspaces (id uuid)")
    url = f"{tenant_database.owner_url}?options=-csearch_path%3Delsewhere,public"
    with pytest.raises(Exception, match="not in the schema that alembic_version is in"):
        migrate.upgrade(url)
    with psycopg.connect(tenant_database.admin_url) as admin:
        assert admin.execute(
            "SELECT count(*) FROM pg_proc WHERE proname = 'resolve_slack_workspace'"
        ).fetchone() == (0,)


# Migration and readiness


def test_upgrade_from_0002_and_readiness_around_the_grant(tenant_database: TenantDatabase) -> None:
    database = tenant_database
    config = migrate.alembic_config(database.owner_url)
    command.upgrade(config, "0002")
    with psycopg.connect(database.owner_url, autocommit=True) as owner:
        owner.execute(
            sql.SQL("GRANT SELECT ON alembic_version, slack_workspaces TO {}").format(
                sql.Identifier(database.runtime_role)
            )
        )
    enabled = _register(database.owner_url, TEAM_A, enabled=True)
    # Behind this release: not ready, and there is nothing to resolve with.
    assert _ready(database.runtime_url) is False
    with pytest.raises(WorkspaceLookupUnavailable, match="UndefinedFunction"):
        _resolve(database.runtime_url, TEAM_A)
    # At head, but without the documented EXECUTE grant: not ready.
    migrate.upgrade(database.owner_url)
    assert _ready(database.runtime_url) is False
    # With the documented grant: ready, and the existing workspace resolves.
    grant_runtime_access(database.owner_url, database.runtime_role)
    assert _ready(database.runtime_url) is True
    assert _resolve(database.runtime_url, TEAM_A) == enabled
    # Downgrading removes the function; the workspace remains.
    command.downgrade(config, "0002")
    with psycopg.connect(database.admin_url) as admin:
        assert admin.execute("SELECT to_regprocedure(%s)", (RESOLVER,)).fetchone() == (None,)
    assert _statuses(database.owner_url) == [(TEAM_A, "enabled")]


def test_readiness_without_the_execute_grant_reports_the_code(
    tenant_database: TenantDatabase, caplog: pytest.LogCaptureFixture
) -> None:
    migrate.upgrade(tenant_database.owner_url)
    with psycopg.connect(tenant_database.owner_url, autocommit=True) as owner:
        owner.execute(
            sql.SQL("GRANT SELECT ON alembic_version, slack_workspaces TO {}").format(
                sql.Identifier(tenant_database.runtime_role)
            )
        )
    with caplog.at_level(logging.WARNING):
        assert _ready(tenant_database.runtime_url) is False
    assert "cannot_resolve_workspaces" in caplog.text.split(UNSAFE, 1)[1].splitlines()[0]
