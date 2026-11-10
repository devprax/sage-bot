"""Slack workspaces with tenant row-level security.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

# A row is visible and writable only within a transaction bound to that workspace.
_TENANT = "nullif(current_setting('sage.workspace_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "slack_workspaces",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()")),
        sa.Column("slack_team_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'disabled'")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_slack_workspaces"),
        sa.UniqueConstraint("slack_team_id", name="uq_slack_workspaces_slack_team_id"),
        sa.CheckConstraint("status IN ('disabled', 'enabled')", name="ck_slack_workspaces_status"),
        sa.CheckConstraint(
            "slack_team_id ~ '^T[A-Z0-9]{1,31}$'", name="ck_slack_workspaces_slack_team_id"
        ),
    )
    # Enabled, not forced: the owning role is the trusted provisioning role, and functions it
    # defines with SECURITY DEFINER can bypass the policy only while it is not forced.
    op.execute("ALTER TABLE slack_workspaces ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON slack_workspaces "
        f"USING (id = {_TENANT}) WITH CHECK (id = {_TENANT})"
    )


def downgrade() -> None:
    op.drop_table("slack_workspaces")
