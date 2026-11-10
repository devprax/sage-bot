"""PostgreSQL workspace persistence and the tenant transaction boundary.

`slack_workspaces` has row-level security with one policy: a row is visible and writable only
when its `id` equals the transaction's `sage.workspace_id` setting. With no setting, nothing is
visible. The policy constrains every role that neither owns the table nor has BYPASSRLS or
superuser; such a role also cannot disable or change the policy.

The table owner is the trusted provisioning role, so row-level security is enabled but not
forced: provisioning must find any team without a tenant context. Tenant-scoped code must
therefore connect as a different, restricted role that has only the privileges it needs.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import psycopg
from psycopg.pq import TransactionStatus

from sage_bot.workspaces import Registration, Workspace, WorkspaceStatus, validate_slack_team_id

TENANT_SETTING = "sage.workspace_id"
APPLICATION_NAME = "sage-bot"
CONNECT_TIMEOUT_SECONDS = 10


@asynccontextmanager
async def tenant_transaction(
    connection: psycopg.AsyncConnection, workspace_id: UUID
) -> AsyncGenerator[psycopg.AsyncConnection]:
    """Run a transaction bound to one workspace; the binding ends with the transaction.

    It must start the transaction itself. Inside a caller's transaction it would only open a
    savepoint, and the transaction-local setting would outlive this block.
    """
    if not isinstance(workspace_id, UUID):
        raise TypeError("workspace_id must be a UUID")
    if connection.info.transaction_status != TransactionStatus.IDLE:
        raise RuntimeError("tenant_transaction cannot run inside an open transaction")
    async with connection.transaction():
        await connection.execute(
            "SELECT set_config(%s, %s, true)", (TENANT_SETTING, str(workspace_id))
        )
        yield connection


async def _connect(database_url: str, *, autocommit: bool) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(
        database_url,
        autocommit=autocommit,
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
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
        # Autocommit: each statement sees rows committed before it started. An insert that
        # conflicts with a concurrent, uncommitted registration waits for it to finish.
        async with await _connect(self._database_url, autocommit=True) as connection:
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
            # The team exists but is not visible: the role cannot bypass row-level security.
            raise LookupError("registered workspace is not visible to this role")
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
