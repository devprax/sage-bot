"""Slack workspaces: the tenant root that every tenant-owned record will belong to.

Two interfaces separate the trust levels:

- `WorkspaceProvisioning` is trusted. It registers Slack teams and looks them up without a
  tenant context, so it runs with the role that owns the schema, as installation or bootstrap
  steps do. It must never be reachable from tenant request handling.
- `TenantWorkspaces` is tenant-scoped. Each call is bound to one workspace for one transaction,
  and the database's row-level security limits it to that workspace's row.
- `WorkspaceResolver` is the one tenant-free read the runtime needs: it maps a Slack team to its
  workspace UUID, and only for an enabled workspace.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

# Slack workspace (team) IDs: "T" then upper-case letters and digits.
_SLACK_TEAM_ID = re.compile(r"T[A-Z0-9]{1,31}")


class WorkspaceStatus(StrEnum):
    DISABLED = "disabled"
    ENABLED = "enabled"


@dataclass(frozen=True)
class Workspace:
    id: UUID
    slack_team_id: str
    status: WorkspaceStatus
    created_at: datetime


@dataclass(frozen=True)
class Registration:
    workspace: Workspace
    created: bool


def validate_slack_team_id(value: str) -> str:
    if not _SLACK_TEAM_ID.fullmatch(value):
        raise ValueError("invalid Slack team ID")
    return value


class WorkspaceProvisioning(Protocol):
    async def register(self, slack_team_id: str) -> Registration:
        """Return the team's workspace, creating it disabled if the team is new.

        Repeated and concurrent registrations of one team return the same workspace, with
        `created` true for exactly one of them, and never change an existing workspace.
        """
        ...

    async def find_by_slack_team(self, slack_team_id: str) -> Workspace | None: ...


class TenantWorkspaces(Protocol):
    async def get(self, workspace_id: UUID) -> Workspace | None:
        """The workspace, read within a transaction bound to that workspace."""
        ...


class WorkspaceLookupUnavailable(Exception):
    """The lookup could not complete (database failure or timeout); it may succeed later.

    Distinct from no match: a caller must not treat it as an unknown or disabled workspace.
    The message names only the failure's type, never connection details.
    """


class WorkspaceResolver(Protocol):
    async def resolve_enabled(self, slack_team_id: str) -> UUID | None:
        """The UUID of the team's workspace if it is enabled; None if it is unknown or disabled.

        Never registers or enables a workspace. Raises ValueError for a malformed team ID,
        before connecting, and WorkspaceLookupUnavailable if the lookup cannot complete.

        This is not authentication. The team ID must come from a request whose Slack origin was
        already verified (signature and timestamp); a resolved workspace says only that the
        team is enabled, not that the caller speaks for it.
        """
        ...
