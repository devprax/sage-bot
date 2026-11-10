"""Runtime and migration credentials against a real PostgreSQL server.

The migration role owns the database and is not a superuser; the runtime role is a separate
login role that holds only the documented grant. Fixtures are in conftest.py.
"""

import asyncio
import base64
import logging
import os
import subprocess
import sys
from collections.abc import Callable
from typing import LiteralString
from urllib.parse import urlsplit

import psycopg
import pytest
from fastapi.testclient import TestClient
from pg_support import TenantDatabase, url_with
from psycopg import sql

from sage_bot.api.app import create_app
from sage_bot.api.readiness import not_configured, postgres_check, readiness_from_environment
from sage_bot.db import migrate

ENCRYPTION_KEY = base64.b64encode(bytes(range(32))).decode()
WRONG_PASSWORD = "wrong-password-for-test"  # noqa: S105
UNSAFE = "readiness: runtime database role is unsafe"


def _ready(url: str) -> bool:
    check = postgres_check(url, timeout_seconds=5, expected_revision=migrate.expected_revision())
    return asyncio.run(check())


def _secrets(*urls: str) -> list[str]:
    values: list[str] = []
    for url in urls:
        password = urlsplit(url).password
        assert password is not None
        values += [url, password]
    return values


def _migrate_command(**variables: str) -> subprocess.CompletedProcess[str]:
    inherited = {
        name: value
        for name, value in os.environ.items()
        if not name.upper().startswith("SAGE_")
        and name.upper() not in {"DATABASE_URL", "MIGRATION_DATABASE_URL"}
    }
    return subprocess.run(  # noqa: S603 -- fixed argv: this interpreter + our module
        [sys.executable, "-m", "sage_bot.db.migrate"],
        capture_output=True,
        text=True,
        env={**inherited, **variables},
        timeout=60,
        check=False,
    )


def _owners(url: str) -> set[str]:
    with psycopg.connect(url) as connection:
        rows = connection.execute(
            """SELECT pg_get_userbyid(relowner) FROM pg_class
               WHERE oid IN ('alembic_version'::regclass, 'slack_workspaces'::regclass)"""
        ).fetchall()
    return {row[0] for row in rows}


def _has_schema(url: str) -> bool:
    with psycopg.connect(url) as connection:
        row = connection.execute("SELECT to_regclass('alembic_version')").fetchone()
    return row is not None and row[0] is not None


# Credential routing


def test_migrate_uses_the_migration_credentials(tenant_database: TenantDatabase) -> None:
    result = _migrate_command(
        MIGRATION_DATABASE_URL=tenant_database.owner_url,
        DATABASE_URL=tenant_database.runtime_url,
    )
    assert result.returncode == 0, result.stderr
    assert _owners(tenant_database.admin_url) == {tenant_database.owner_role}


def test_migrate_requires_migration_credentials_without_falling_back(
    tenant_database: TenantDatabase,
) -> None:
    # Even owner credentials in DATABASE_URL must not be used for migration.
    result = _migrate_command(
        DATABASE_URL=tenant_database.owner_url, SAGE_ENCRYPTION_KEY=ENCRYPTION_KEY
    )
    assert result.returncode == 2
    assert "settings are missing or invalid" in result.stderr
    output = result.stdout + result.stderr
    for secret in _secrets(tenant_database.owner_url):
        assert secret not in output
    assert _has_schema(tenant_database.admin_url) is False


def test_migrate_does_not_need_runtime_settings(tenant_database: TenantDatabase) -> None:
    result = _migrate_command(MIGRATION_DATABASE_URL=tenant_database.owner_url)
    assert result.returncode == 0, result.stderr


def test_server_uses_runtime_credentials_and_ignores_migration_ones(
    migrated: TenantDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SAGE_ENCRYPTION_KEY", ENCRYPTION_KEY)
    monkeypatch.setenv("DATABASE_URL", migrated.runtime_url)
    # Unusable migration credentials: the server must neither validate nor use them.
    monkeypatch.setenv("MIGRATION_DATABASE_URL", "not a url")
    assert asyncio.run(readiness_from_environment()()) is True
    # Swapped credentials: the owner as runtime is rejected, so DATABASE_URL is what is used.
    monkeypatch.setenv("DATABASE_URL", migrated.owner_url)
    monkeypatch.setenv("MIGRATION_DATABASE_URL", migrated.runtime_url)
    assert asyncio.run(readiness_from_environment()()) is False


def test_server_does_not_fall_back_to_migration_credentials(
    migrated: TenantDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("SAGE_ENCRYPTION_KEY", ENCRYPTION_KEY)
    monkeypatch.setenv("MIGRATION_DATABASE_URL", migrated.runtime_url)
    assert readiness_from_environment() is not_configured


# The restricted runtime role


def test_restricted_runtime_role_is_ready(
    migrated: TenantDatabase, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        assert _ready(migrated.runtime_url) is True
    assert caplog.text == ""


def test_runtime_role_without_the_revision_grant_is_not_ready(
    tenant_database: TenantDatabase, caplog: pytest.LogCaptureFixture
) -> None:
    migrate.upgrade(tenant_database.owner_url)
    with caplog.at_level(logging.WARNING):
        assert _ready(tenant_database.runtime_url) is False
    assert "database check failed (InsufficientPrivilege)" in caplog.text


def test_runtime_role_cannot_migrate(tenant_database: TenantDatabase) -> None:
    with pytest.raises(Exception, match="permission denied"):
        migrate.upgrade(tenant_database.runtime_url)
    assert _has_schema(tenant_database.admin_url) is False
    # Nor advance a schema the owner has started.
    from alembic import command

    command.upgrade(migrate.alembic_config(tenant_database.owner_url), "0001")
    grant_runtime_access_revision_only(tenant_database)
    with pytest.raises(Exception, match="permission denied"):
        migrate.upgrade(tenant_database.runtime_url)
    with psycopg.connect(tenant_database.admin_url) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchall() == [
            ("0001",)
        ]


def grant_runtime_access_revision_only(database: TenantDatabase) -> None:
    with psycopg.connect(database.owner_url, autocommit=True) as owner:
        owner.execute(
            sql.SQL("GRANT SELECT ON alembic_version TO {}").format(
                sql.Identifier(database.runtime_role)
            )
        )


# Unsafe runtime roles


def _admin(database: TenantDatabase, statement: LiteralString, **names: str) -> None:
    identifiers = {key: sql.Identifier(value) for key, value in names.items()}
    with psycopg.connect(database.admin_url, autocommit=True) as admin:
        admin.execute(sql.SQL(statement).format(**identifiers))


Change = Callable[[TenantDatabase], None]

UNSAFE_CHANGES: dict[str, tuple[Change, str]] = {
    "superuser": (
        lambda db: _admin(db, "ALTER ROLE {rt} SUPERUSER", rt=db.runtime_role),
        "superuser",
    ),
    "bypassrls": (
        lambda db: _admin(db, "ALTER ROLE {rt} BYPASSRLS", rt=db.runtime_role),
        "bypassrls",
    ),
    "createrole": (
        lambda db: _admin(db, "ALTER ROLE {rt} CREATEROLE", rt=db.runtime_role),
        "createrole",
    ),
    "createdb": (
        lambda db: _admin(db, "ALTER ROLE {rt} CREATEDB", rt=db.runtime_role),
        "createdb",
    ),
    "replication": (
        lambda db: _admin(db, "ALTER ROLE {rt} REPLICATION", rt=db.runtime_role),
        "replication",
    ),
    "maintains every table through SET ROLE only": (
        lambda db: _admin(
            db, "GRANT pg_maintain TO {rt} WITH INHERIT FALSE, SET TRUE", rt=db.runtime_role
        ),
        "writes_workspaces",
    ),
    "writes all data through SET ROLE only": (
        lambda db: _admin(
            db, "GRANT pg_write_all_data TO {rt} WITH INHERIT FALSE, SET TRUE", rt=db.runtime_role
        ),
        "writes_workspaces",
    ),
    "member of the owner": (
        lambda db: _admin(db, "GRANT {owner} TO {rt}", owner=db.owner_role, rt=db.runtime_role),
        "owns_relation",
    ),
    "member of the owner without inheritance": (
        lambda db: _admin(
            db,
            "GRANT {owner} TO {rt} WITH INHERIT FALSE, SET TRUE",
            owner=db.owner_role,
            rt=db.runtime_role,
        ),
        "owns_relation",
    ),
    "executes server programs": (
        lambda db: _admin(db, "GRANT pg_execute_server_program TO {rt}", rt=db.runtime_role),
        "server_files_or_programs",
    ),
    "reads server files": (
        lambda db: _admin(db, "GRANT pg_read_server_files TO {rt}", rt=db.runtime_role),
        "server_files_or_programs",
    ),
    "writes server files": (
        lambda db: _admin(db, "GRANT pg_write_server_files TO {rt}", rt=db.runtime_role),
        "server_files_or_programs",
    ),
    "signals backends": (
        lambda db: _admin(db, "GRANT pg_signal_backend TO {rt}", rt=db.runtime_role),
        "signal_backend",
    ),
    "maintains every table": (
        lambda db: _admin(db, "GRANT pg_maintain TO {rt}", rt=db.runtime_role),
        "writes_workspaces",
    ),
    "maintains workspaces": (
        lambda db: _admin(db, "GRANT MAINTAIN ON slack_workspaces TO {rt}", rt=db.runtime_role),
        "writes_workspaces",
    ),
    "maintains the schema revision": (
        lambda db: _admin(db, "GRANT MAINTAIN ON alembic_version TO {rt}", rt=db.runtime_role),
        "writes_schema_revision",
    ),
    "reads server files through a function": (
        lambda db: _admin(
            db,
            "GRANT EXECUTE ON FUNCTION pg_catalog.pg_read_binary_file(text) TO {rt}",
            rt=db.runtime_role,
        ),
        "privileged_function",
    ),
    "imports server files as large objects": (
        lambda db: _admin(
            db, "GRANT EXECUTE ON FUNCTION pg_catalog.lo_import(text) TO {rt}", rt=db.runtime_role
        ),
        "privileged_function",
    ),
    "everyone reads server files": (
        lambda db: _admin(db, "GRANT EXECUTE ON FUNCTION pg_catalog.pg_read_file(text) TO PUBLIC"),
        "privileged_function",
    ),
    "runs an extension function restricted by default": (
        lambda db: _admin(
            db,
            "CREATE EXTENSION dblink; "
            "GRANT EXECUTE ON FUNCTION dblink_connect_u(text, text) TO {rt}",
            rt=db.runtime_role,
        ),
        "unexpected_privilege",
    ),
    "everyone runs an extension function restricted by default": (
        lambda db: _admin(
            db,
            "CREATE EXTENSION dblink; "
            "GRANT EXECUTE ON FUNCTION dblink_connect_u(text, text) TO PUBLIC",
        ),
        "privileged_function",
    ),
    "reads column statistics": (
        lambda db: _admin(
            db, "GRANT SELECT ON pg_catalog.pg_statistic TO {rt}", rt=db.runtime_role
        ),
        "unexpected_privilege",
    ),
    "everyone reads column statistics": (
        lambda db: _admin(db, "GRANT SELECT ON pg_catalog.pg_statistic TO PUBLIC"),
        "public_catalog_access",
    ),
    "reads another table": (
        lambda db: _admin(
            db,
            "CREATE TABLE public.extra (id int); GRANT SELECT ON public.extra TO {rt}",
            rt=db.runtime_role,
        ),
        "unexpected_privilege",
    ),
    "a column grant": (
        lambda db: _admin(
            db, "GRANT SELECT (slack_team_id) ON slack_workspaces TO {rt}", rt=db.runtime_role
        ),
        "unexpected_privilege",
    ),
    "uses another schema": (
        lambda db: _admin(
            db, "CREATE SCHEMA extra; GRANT USAGE ON SCHEMA extra TO {rt}", rt=db.runtime_role
        ),
        "unexpected_privilege",
    ),
    "default privileges for future tables": (
        lambda db: _admin(
            db,
            "ALTER DEFAULT PRIVILEGES FOR ROLE {owner} GRANT INSERT ON TABLES TO {rt}",
            owner=db.owner_role,
            rt=db.runtime_role,
        ),
        "unexpected_privilege",
    ),
    "a user mapping": (
        lambda db: _admin(
            db,
            "CREATE EXTENSION postgres_fdw; "
            "CREATE SERVER extra FOREIGN DATA WRAPPER postgres_fdw; "
            "CREATE USER MAPPING FOR {rt} SERVER extra",
            rt=db.runtime_role,
        ),
        "unexpected_privilege",
    ),
    "monitoring role": (
        lambda db: _admin(db, "GRANT pg_monitor TO {rt}", rt=db.runtime_role),
        "predefined_role",
    ),
    "reads all data": (
        lambda db: _admin(db, "GRANT pg_read_all_data TO {rt}", rt=db.runtime_role),
        "predefined_role",
    ),
    "owns a function": (
        lambda db: _admin(
            db,
            "CREATE FUNCTION public.extra() RETURNS int LANGUAGE sql AS 'SELECT 1'; "
            "ALTER FUNCTION public.extra() OWNER TO {rt}",
            rt=db.runtime_role,
        ),
        "owns_function_or_type",
    ),
    "owns a schema on its search path": (
        lambda db: _admin(db, "CREATE SCHEMA {rt} AUTHORIZATION {rt}", rt=db.runtime_role),
        "owns_schema",
    ),
    "owns a relation": (
        lambda db: _admin(
            db,
            "CREATE TABLE public.extra (id int); ALTER TABLE public.extra OWNER TO {rt}",
            rt=db.runtime_role,
        ),
        "owns_relation",
    ),
    "owns the schema": (
        lambda db: _admin(db, "ALTER SCHEMA public OWNER TO {rt}", rt=db.runtime_role),
        "owns_schema",
    ),
    "owns the database": (
        lambda db: _admin(
            db, "ALTER DATABASE {name} OWNER TO {rt}", name=db.name, rt=db.runtime_role
        ),
        "owns_schema",
    ),
    "schema create for everyone": (
        lambda db: _admin(db, "GRANT CREATE ON SCHEMA public TO PUBLIC"),
        "schema_create",
    ),
    "database create": (
        lambda db: _admin(
            db, "GRANT CREATE ON DATABASE {name} TO {rt}", name=db.name, rt=db.runtime_role
        ),
        "database_create",
    ),
    "writes the schema revision": (
        lambda db: _admin(db, "GRANT UPDATE ON alembic_version TO {rt}", rt=db.runtime_role),
        "writes_schema_revision",
    ),
    "writes workspaces": (
        lambda db: _admin(db, "GRANT INSERT ON slack_workspaces TO {rt}", rt=db.runtime_role),
        "writes_workspaces",
    ),
    "updates a workspace column": (
        lambda db: _admin(
            db, "GRANT UPDATE (status) ON slack_workspaces TO {rt}", rt=db.runtime_role
        ),
        "writes_workspaces",
    ),
    "inserts a schema revision column": (
        lambda db: _admin(
            db, "GRANT INSERT (version_num) ON alembic_version TO {rt}", rt=db.runtime_role
        ),
        "writes_schema_revision",
    ),
    "truncates workspaces": (
        lambda db: _admin(db, "GRANT TRUNCATE ON slack_workspaces TO {rt}", rt=db.runtime_role),
        "writes_workspaces",
    ),
    "cannot read workspaces": (
        lambda db: _admin(db, "REVOKE SELECT ON slack_workspaces FROM {rt}", rt=db.runtime_role),
        "cannot_read_workspaces",
    ),
    "workspaces without row security": (
        lambda db: _admin(db, "ALTER TABLE slack_workspaces DISABLE ROW LEVEL SECURITY"),
        "workspaces_without_row_security",
    ),
}


@pytest.mark.parametrize("change", list(UNSAFE_CHANGES))
def test_unsafe_runtime_role_is_not_ready(
    migrated: TenantDatabase, change: str, caplog: pytest.LogCaptureFixture
) -> None:
    apply, code = UNSAFE_CHANGES[change]
    apply(migrated)
    with caplog.at_level(logging.WARNING):
        assert _ready(migrated.runtime_url) is False
    assert UNSAFE in caplog.text
    assert code in caplog.text.split(UNSAFE, 1)[1].splitlines()[0]
    for secret in _secrets(migrated.runtime_url, migrated.owner_url):
        assert secret not in caplog.text


def test_membership_in_a_bypassrls_role_is_unsafe(
    migrated: TenantDatabase, caplog: pytest.LogCaptureFixture
) -> None:
    privileged = f"{migrated.runtime_role}_x"
    _admin(migrated, "CREATE ROLE {role} NOLOGIN BYPASSRLS", role=privileged)
    try:
        _admin(migrated, "GRANT {role} TO {rt}", role=privileged, rt=migrated.runtime_role)
        with caplog.at_level(logging.WARNING):
            assert _ready(migrated.runtime_url) is False
        assert "bypassrls" in caplog.text
    finally:
        _admin(migrated, "DROP ROLE {role}", role=privileged)


@pytest.mark.parametrize(
    ("membership", "noinherit"),
    [
        ("WITH INHERIT FALSE, SET TRUE", False),
        ("", True),
        ("WITH ADMIN TRUE, INHERIT FALSE, SET FALSE", False),
    ],
    ids=["set-only", "noinherit-login", "admin-only"],
)
def test_privileges_reachable_only_through_another_role_count(
    migrated: TenantDatabase,
    membership: LiteralString,
    noinherit: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A role it can SET ROLE to, or grant itself, is as usable as an inherited one."""
    writer = f"{migrated.runtime_role}_w"
    _admin(migrated, "CREATE ROLE {role} NOLOGIN", role=writer)
    try:
        _admin(
            migrated,
            # TRUNCATE ignores row-level security; INSERT would forge the schema revision.
            "GRANT TRUNCATE ON slack_workspaces TO {role}; "
            "GRANT INSERT ON alembic_version TO {role}; "
            "GRANT CREATE ON SCHEMA public TO {role}; GRANT CREATE ON DATABASE {name} TO {role}",
            role=writer,
            name=migrated.name,
        )
        if noinherit:
            _admin(migrated, "ALTER ROLE {rt} NOINHERIT", rt=migrated.runtime_role)
        grant = "GRANT {role} TO {rt} " + membership
        _admin(migrated, grant, role=writer, rt=migrated.runtime_role)
        with caplog.at_level(logging.WARNING):
            assert _ready(migrated.runtime_url) is False
        codes = caplog.text.split(UNSAFE, 1)[1].splitlines()[0].split(" (", 1)[1]
        for code in ("writes_schema_revision", "writes_workspaces", "schema_create"):
            assert code in codes
        assert "database_create" in codes
    finally:
        _admin(migrated, "DROP OWNED BY {role}", role=writer)
        _admin(migrated, "DROP ROLE {role}", role=writer)


@pytest.mark.parametrize(
    ("grant", "revoke"),
    [
        (
            "GRANT SET ON PARAMETER session_replication_role TO {rt}",
            "REVOKE SET ON PARAMETER session_replication_role FROM {rt}",
        ),
        (
            "GRANT ALTER SYSTEM ON PARAMETER archive_command TO {rt}",
            "REVOKE ALTER SYSTEM ON PARAMETER archive_command FROM {rt}",
        ),
        (
            "GRANT SET ON PARAMETER session_replication_role TO PUBLIC",
            "REVOKE SET ON PARAMETER session_replication_role FROM PUBLIC",
        ),
    ],
    ids=["set", "alter-system", "public"],
)
def test_a_server_parameter_grant_is_unsafe(
    migrated: TenantDatabase,
    grant: LiteralString,
    revoke: LiteralString,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Parameter grants are cluster-wide, so they are always revoked here.
    _admin(migrated, grant, rt=migrated.runtime_role)
    try:
        with caplog.at_level(logging.WARNING):
            assert _ready(migrated.runtime_url) is False
        assert "parameter_grant" in caplog.text.split(UNSAFE, 1)[1].splitlines()[0]
    finally:
        _admin(migrated, revoke, rt=migrated.runtime_role)
    caplog.clear()
    assert _ready(migrated.runtime_url) is True


def test_a_default_set_role_does_not_hide_the_login_roles_memberships(
    migrated: TenantDatabase, caplog: pytest.LogCaptureFixture
) -> None:
    """A login role that switches to a weaker role by default can still switch to the owner."""
    weaker = f"{migrated.runtime_role}_w"
    _admin(migrated, "CREATE ROLE {role} NOLOGIN", role=weaker)
    try:
        _admin(migrated, "GRANT SELECT ON alembic_version, slack_workspaces TO {role}", role=weaker)
        _admin(migrated, "GRANT {role} TO {rt}", role=weaker, rt=migrated.runtime_role)
        _admin(
            migrated, "GRANT {owner} TO {rt}", owner=migrated.owner_role, rt=migrated.runtime_role
        )
        _admin(migrated, "ALTER ROLE {rt} SET role = {role}", rt=migrated.runtime_role, role=weaker)
        with caplog.at_level(logging.WARNING):
            assert _ready(migrated.runtime_url) is False
        codes = caplog.text.split(UNSAFE, 1)[1].splitlines()[0]
        assert "session_role_differs" in codes
        assert "owns_relation" in codes
    finally:
        _admin(migrated, "ALTER ROLE {rt} RESET role", rt=migrated.runtime_role)
        _admin(migrated, "DROP OWNED BY {role}", role=weaker)
        _admin(migrated, "DROP ROLE {role}", role=weaker)


@pytest.mark.parametrize("credentials", ["owner", "superuser"])
def test_privileged_credentials_as_runtime_are_not_ready(
    migrated: TenantDatabase, credentials: str, caplog: pytest.LogCaptureFixture
) -> None:
    url = migrated.owner_url if credentials == "owner" else migrated.admin_url
    with caplog.at_level(logging.WARNING):
        assert _ready(url) is False
    assert UNSAFE in caplog.text


def test_unsafe_runtime_role_gets_the_generic_503_and_liveness_stays_up(
    migrated: TenantDatabase, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("SAGE_ENCRYPTION_KEY", ENCRYPTION_KEY)
    monkeypatch.setenv("DATABASE_URL", migrated.owner_url)
    with caplog.at_level(logging.WARNING):
        client = TestClient(create_app(readiness=readiness_from_environment()))
        response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.content == b'{"status":"not_ready"}'
    assert client.head("/health/ready").status_code == 503
    assert client.get("/health/live").status_code == 200
    for secret in _secrets(migrated.owner_url):
        assert secret not in caplog.text
        assert secret not in response.text


def test_wrong_runtime_password_is_not_logged(
    migrated: TenantDatabase, caplog: pytest.LogCaptureFixture
) -> None:
    url = url_with(migrated.runtime_url, password=WRONG_PASSWORD)
    with caplog.at_level(logging.WARNING):
        assert _ready(url) is False
    assert WRONG_PASSWORD not in caplog.text
    assert url not in caplog.text
