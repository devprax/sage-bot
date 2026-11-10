"""Fixtures for tests against a real PostgreSQL server.

Set SAGE_TEST_DATABASE_URL to a disposable database whose user may create databases and roles.
These tests are skipped locally without it, and fail in CI if it is missing, so they can never
pass by being skipped. Every database and role a test creates is dropped afterwards.
"""

import os
import secrets
from collections.abc import Iterator
from uuid import uuid4

import psycopg
import pytest
from pg_support import TenantDatabase, grant_runtime_access, url_with
from psycopg import sql

from sage_bot.db import migrate


@pytest.fixture
def database_url() -> str:
    url = os.environ.get("SAGE_TEST_DATABASE_URL", "")
    if not url:
        if os.environ.get("CI", "").lower() in {"1", "true", "yes"}:
            pytest.fail("SAGE_TEST_DATABASE_URL must be set in CI")
        pytest.skip("set SAGE_TEST_DATABASE_URL to run PostgreSQL integration tests")
    return url


def _drop_database(admin_url: str, name: str) -> None:
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )


@pytest.fixture
def fresh_database(database_url: str) -> Iterator[str]:
    """An empty database for one test, so migrations never touch shared state."""
    name = f"sage_test_{uuid4().hex[:12]}"
    with psycopg.connect(database_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        yield url_with(database_url, database=name)
    finally:
        _drop_database(database_url, name)


@pytest.fixture
def tenant_database(database_url: str) -> Iterator[TenantDatabase]:
    """An empty database owned by a fresh non-superuser role, and a fresh restricted role."""
    suffix = uuid4().hex[:12]
    name, owner, runtime = f"sage_test_{suffix}", f"sage_owner_{suffix}", f"sage_runtime_{suffix}"
    # Random per-test credentials for roles that exist only for this test.
    owner_password, runtime_password = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    restricted = sql.SQL("LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS")
    try:
        with psycopg.connect(database_url, autocommit=True) as admin:
            for role, password in ((owner, owner_password), (runtime, runtime_password)):
                admin.execute(
                    sql.SQL("CREATE ROLE {} {} PASSWORD {}").format(
                        sql.Identifier(role), restricted, sql.Literal(password)
                    )
                )
            admin.execute(
                sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(name), sql.Identifier(owner)
                )
            )
        yield TenantDatabase(
            name=name,
            admin_url=url_with(database_url, database=name),
            owner_role=owner,
            owner_url=url_with(database_url, user=owner, password=owner_password, database=name),
            runtime_role=runtime,
            runtime_url=url_with(
                database_url, user=runtime, password=runtime_password, database=name
            ),
        )
    finally:
        try:
            # Dropping the database first removes the roles' privileges in it.
            _drop_database(database_url, name)
        finally:
            with psycopg.connect(database_url, autocommit=True) as admin:
                for role in (runtime, owner):
                    admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


@pytest.fixture
def migrated(tenant_database: TenantDatabase) -> TenantDatabase:
    """The tenant database migrated to head by its owner, with the documented runtime grant."""
    migrate.upgrade(tenant_database.owner_url)
    grant_runtime_access(tenant_database.owner_url, tenant_database.runtime_role)
    return tenant_database
