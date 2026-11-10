"""PostgreSQL workspace persistence and the tenant transaction boundary.

`slack_workspaces` has row-level security with one policy: a row is visible and writable only
when its `id` equals the transaction's `sage.workspace_id` setting. With no setting, nothing is
visible. The policy constrains every role that neither owns the table nor has BYPASSRLS or
superuser; such a role also cannot disable or change the policy.

The policy limits a transaction to the workspace it is bound to; it does not decide which
workspace that is. Any session of the runtime role can bind itself to a workspace whose UUID it
knows, so it guards against unscoped or mis-scoped queries, not against stolen runtime
credentials. Bind only to a workspace resolved on the server from an authenticated source,
never from caller input.

The table owner is the trusted provisioning role, so row-level security is enabled but not
forced: provisioning must find any team without a tenant context, and owner-defined
SECURITY DEFINER lookups, such as the one resolving Slack ingress, bypass the policy only when
it is not forced. Tenant-scoped code must therefore connect as a different, restricted role.

`resolve_slack_workspace(text)` (migration 0003) is that lookup: it returns only an enabled
workspace's UUID, and the runtime role may execute it only once the deployment grants EXECUTE.
"""

import asyncio
import math
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import psycopg
from psycopg.pq import TransactionStatus

from sage_bot.db.connections import discard
from sage_bot.workspaces import (
    Registration,
    Workspace,
    WorkspaceLookupUnavailable,
    WorkspaceStatus,
    validate_slack_team_id,
)

TENANT_SETTING = "sage.workspace_id"
APPLICATION_NAME = "sage-bot"
CONNECT_TIMEOUT_SECONDS = 10
# Slack expects an event acknowledged within three seconds; the lookup must leave room for it.
RESOLVE_TIMEOUT_SECONDS = 2.0


@asynccontextmanager
async def tenant_transaction(
    connection: psycopg.AsyncConnection, workspace_id: UUID
) -> AsyncGenerator[psycopg.AsyncConnection]:
    """Run a transaction bound to one workspace; the binding ends with the transaction.

    It must start the transaction itself. Inside a caller's transaction it would only open a
    savepoint, and the transaction-local setting would outlive this block. It also refuses a
    connection that already carries a binding, such as one set for the whole session.
    """
    if not isinstance(workspace_id, UUID):
        raise TypeError("workspace_id must be a UUID")
    if connection.info.transaction_status != TransactionStatus.IDLE:
        raise RuntimeError("tenant_transaction cannot run inside an open transaction")
    async with connection.transaction():
        cursor = await connection.execute("SELECT current_setting(%s, true)", (TENANT_SETTING,))
        current = await cursor.fetchone()
        if current is not None and current[0]:
            raise RuntimeError("connection is already bound to a workspace")
        await connection.execute(
            "SELECT set_config(%s, %s, true)", (TENANT_SETTING, str(workspace_id))
        )
        yield connection


async def _connect(
    database_url: str, *, autocommit: bool, connect_timeout: int = CONNECT_TIMEOUT_SECONDS
) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(
        database_url,
        autocommit=autocommit,
        connect_timeout=connect_timeout,
        application_name=APPLICATION_NAME,
    )


def _workspace(row: tuple[Any, ...]) -> Workspace:
    workspace_id, slack_team_id, status, created_at = row
    return Workspace(
        id=workspace_id,
        slack_team_id=slack_team_id,
        status=WorkspaceStatus(status),
        created_at=created_at,
    )


class PostgresWorkspaceProvisioning:
    """Trusted provisioning. Connect as the role that owns the schema."""

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url

    async def register(self, slack_team_id: str) -> Registration:
        team = validate_slack_team_id(slack_team_id)
        async with await _connect(self._database_url, autocommit=False) as connection:
            # Read committed regardless of the server default: an insert that conflicts with a
            # concurrent, uncommitted registration waits for it, and the following select takes
            # a new snapshot that sees the committed row. Under repeatable read or serializable
            # the insert would fail with a serialization error instead.
            await connection.set_isolation_level(psycopg.IsolationLevel.READ_COMMITTED)
            async with connection.transaction():
                cursor = await connection.execute(
                    """INSERT INTO slack_workspaces (slack_team_id) VALUES (%s)
                       ON CONFLICT (slack_team_id) DO NOTHING
                       RETURNING id, slack_team_id, status, created_at""",
                    (team,),
                )
                row = await cursor.fetchone()
                if row is not None:
                    return Registration(workspace=_workspace(row), created=True)
                existing = await self._find(connection, team)
        if existing is None:
            # The insert conflicted, yet no row is visible: it was removed in between, or this
            # role cannot read it.
            raise LookupError("workspace registration conflicted but no record was found")
        return Registration(workspace=existing, created=False)

    async def find_by_slack_team(self, slack_team_id: str) -> Workspace | None:
        team = validate_slack_team_id(slack_team_id)
        async with await _connect(self._database_url, autocommit=True) as connection:
            return await self._find(connection, team)

    @staticmethod
    async def _find(connection: psycopg.AsyncConnection, team: str) -> Workspace | None:
        cursor = await connection.execute(
            """SELECT id, slack_team_id, status, created_at
               FROM slack_workspaces WHERE slack_team_id = %s""",
            (team,),
        )
        row = await cursor.fetchone()
        return None if row is None else _workspace(row)


class PostgresTenantWorkspaces:
    """Tenant-scoped access. Connect as the restricted runtime role."""

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url

    async def get(self, workspace_id: UUID) -> Workspace | None:
        async with await _connect(self._database_url, autocommit=False) as connection:
            async with tenant_transaction(connection, workspace_id):
                cursor = await connection.execute(
                    """SELECT id, slack_team_id, status, created_at
                       FROM slack_workspaces WHERE id = %s""",
                    (workspace_id,),
                )
                row = await cursor.fetchone()
        return None if row is None else _workspace(row)


class PostgresWorkspaceResolver:
    """Resolves Slack teams to enabled workspaces. Connect as the restricted runtime role.

    One deadline bounds connecting and the lookup. The query is never cancelled: psycopg answers
    cancellation with a server-side cancel request and further waiting, which the deadline would
    not bound. Past the deadline the connection is closed instead, and the session's
    `statement_timeout`, set to the time remaining, makes the server abandon the query too.
    """

    def __init__(
        self, database_url: str, *, timeout_seconds: float = RESOLVE_TIMEOUT_SECONDS
    ) -> None:
        if not (math.isfinite(timeout_seconds) and timeout_seconds > 0):
            raise ValueError("timeout_seconds must be a positive number")
        self._database_url = database_url
        self._timeout_seconds = timeout_seconds

    async def resolve_enabled(self, slack_team_id: str) -> UUID | None:
        team = validate_slack_team_id(slack_team_id)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeout_seconds
        connection: psycopg.AsyncConnection[Any] | None = None
        query: asyncio.Task[UUID | None] | None = None
        try:
            async with asyncio.timeout_at(deadline):
                connection = await _connect(
                    self._database_url,
                    autocommit=True,
                    connect_timeout=max(1, math.ceil(self._timeout_seconds)),
                )
            query = asyncio.create_task(_resolve(connection, team, deadline - loop.time()))
            done, _ = await asyncio.wait({query}, timeout=max(0.0, deadline - loop.time()))
            if query not in done:
                raise TimeoutError
            return query.result()
        except Exception as error:
            # The type only: database errors can carry connection details.
            raise WorkspaceLookupUnavailable(
                f"workspace lookup failed ({type(error).__name__})"
            ) from None
        finally:
            await discard(connection, query)


async def _resolve(
    connection: psycopg.AsyncConnection[Any], team: str, remaining_seconds: float
) -> UUID | None:
    # A session setting rather than a connection option, which would replace any options the
    # deployment put in DATABASE_URL.
    timeout_ms = max(1, math.ceil(remaining_seconds * 1000))
    await connection.execute(
        "SELECT pg_catalog.set_config('statement_timeout', %s, false)", (f"{timeout_ms}ms",)
    )
    cursor = await connection.execute("SELECT resolve_slack_workspace(%s)", (team,))
    row = await cursor.fetchone()
    value = None if row is None else row[0]
    if value is not None and not isinstance(value, UUID):
        raise TypeError("unexpected workspace lookup result")
    return value
