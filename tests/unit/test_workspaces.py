import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from alembic.script import ScriptDirectory

from sage_bot.db import migrate
from sage_bot.db.workspaces import (
    PostgresTenantWorkspaces,
    PostgresWorkspaceProvisioning,
    tenant_transaction,
)
from sage_bot.workspaces import (
    Registration,
    TenantWorkspaces,
    Workspace,
    WorkspaceProvisioning,
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
    assert provisioning is not None
    assert tenant is not None


def test_tenant_transaction_requires_a_uuid() -> None:
    async def scenario() -> None:
        connection: Any = None
        workspace_id: Any = "not-a-uuid"
        async with tenant_transaction(connection, workspace_id):
            pass

    with pytest.raises(TypeError, match="UUID"):
        asyncio.run(scenario())


def test_workspace_migration_follows_the_baseline() -> None:
    scripts = ScriptDirectory.from_config(migrate.alembic_config())
    revision = scripts.get_revision(migrate.expected_revision())
    assert revision is not None
    assert revision.revision == "0002"
    assert revision.down_revision == "0001"
