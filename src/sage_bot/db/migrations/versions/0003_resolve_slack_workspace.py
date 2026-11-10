"""Resolve a Slack team to its enabled workspace without a tenant binding.

Revision ID: 0003
Revises: 0002

`resolve_slack_workspace(text)` returns the workspace UUID for an enabled team and NULL
otherwise. It is SECURITY DEFINER, owned by the migration owner (the table owner), so it can
read past the tenant policy, which is enabled but not forced. It exposes only the UUID.

- Its body is a SQL-standard (`BEGIN ATOMIC`) body: PostgreSQL resolves the objects it names
  when the function is created, so a caller's search path cannot substitute them. The objects
  are also schema-qualified, as are the types in its signature, and its own search path is
  fixed to `pg_catalog, pg_temp`.
- No role can execute it until the deployment grants EXECUTE: the default PUBLIC grant is
  revoked in this same transaction.
"""

from typing import LiteralString

from alembic import op
from sqlalchemy import text

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


# The schemas holding slack_workspaces and alembic_version. The comparisons between different
# types name pg_catalog's operator, so no operator elsewhere on the search path can take over.
SCHEMAS: LiteralString = """SELECT
    (SELECT n.nspname FROM pg_catalog.pg_class c
     JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     WHERE c.oid OPERATOR(pg_catalog.=) pg_catalog.to_regclass('slack_workspaces')) AS workspaces,
    (SELECT n.nspname FROM pg_catalog.pg_class c
     JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     WHERE c.oid OPERATOR(pg_catalog.=) pg_catalog.to_regclass('alembic_version')) AS revision"""


def _schema() -> str:
    """The schema holding slack_workspaces and alembic_version, quoted for use in DDL."""
    bind = op.get_bind()
    schemas = bind.execute(text(SCHEMAS)).one()
    workspaces, revision = schemas
    if workspaces is None or workspaces != revision:
        raise RuntimeError("slack_workspaces is not in the schema that alembic_version is in")
    return bind.dialect.identifier_preparer.quote_schema(workspaces)


def upgrade() -> None:
    schema = _schema()
    # The only interpolated value is the schema name, quoted by the dialect.
    op.execute(
        f"""CREATE FUNCTION {schema}.resolve_slack_workspace(team_id pg_catalog.text)
            RETURNS pg_catalog.uuid
            LANGUAGE sql STABLE STRICT SECURITY DEFINER
            SET search_path = pg_catalog, pg_temp
            BEGIN ATOMIC
                SELECT w.id FROM {schema}.slack_workspaces AS w
                WHERE w.slack_team_id OPERATOR(pg_catalog.=) team_id
                  AND w.status OPERATOR(pg_catalog.=) 'enabled';
            END"""  # noqa: S608
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION {schema}.resolve_slack_workspace(pg_catalog.text) FROM PUBLIC"
    )


def downgrade() -> None:
    op.execute(f"DROP FUNCTION {_schema()}.resolve_slack_workspace(pg_catalog.text)")
