"""Helpers for the PostgreSQL integration tests."""

from dataclasses import dataclass
from urllib.parse import quote, urlsplit, urlunsplit

import psycopg
from psycopg import sql


def url_with(
    url: str,
    *,
    user: str | None = None,
    password: str | None = None,
    database: str | None = None,
) -> str:
    """`url` with the user, password or database name replaced."""
    parts = urlsplit(url)
    host = parts.netloc.rpartition("@")[2]
    current_user = parts.username or ""
    current_password = parts.password
    new_user = quote(user, safe="") if user is not None else current_user
    if password is not None:
        credentials = f"{new_user}:{quote(password, safe='')}"
    elif user is None and current_password is not None:
        credentials = f"{new_user}:{current_password}"
    else:
        credentials = new_user
    path = f"/{database}" if database is not None else parts.path
    return urlunsplit(parts._replace(netloc=f"{credentials}@{host}", path=path))


@dataclass(frozen=True)
class TenantDatabase:
    """A temporary database owned by a non-superuser role, plus a restricted runtime role.

    `owner_url` connects as the role that owns the database and runs migrations: the trusted
    provisioning role. `runtime_url` connects as a role with no superuser, BYPASSRLS or
    ownership; it holds only the privileges a test grants it.
    """

    name: str
    admin_url: str
    owner_role: str
    owner_url: str
    runtime_role: str
    runtime_url: str


def grant_runtime_access(owner_url: str, runtime_role: str) -> None:
    """Apply the runtime grant the README documents, as the owning role."""
    with psycopg.connect(owner_url, autocommit=True) as owner:
        owner.execute(
            sql.SQL("GRANT SELECT ON alembic_version, slack_workspaces TO {}").format(
                sql.Identifier(runtime_role)
            )
        )
        owner.execute(
            sql.SQL("GRANT EXECUTE ON FUNCTION resolve_slack_workspace(text) TO {}").format(
                sql.Identifier(runtime_role)
            )
        )
