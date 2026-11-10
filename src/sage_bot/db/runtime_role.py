"""Whether the runtime database role is restricted enough to serve tenant data.

Row-level security protects tenants only against a role that cannot bypass or change it. The
check evaluates the connected role's effective privileges, never its name:

- Role attributes and ownership count through every role it belongs to, whether it inherits
  their privileges or can `SET ROLE` to them.
- Schema and table privileges are read from the catalog, including grants to `PUBLIC`, rather
  than assumed from PostgreSQL's defaults.

It returns fixed problem codes, never names or values, so they are safe to log.
"""

from typing import LiteralString

# Starts ROLE_CHECK, so a test double can recognise it; a comment to PostgreSQL.
ROLE_CHECK_MARKER = "sage-bot runtime role check"

PROBLEMS = (
    # A role it belongs to can bypass row-level security or every permission check.
    "superuser",
    "bypassrls",
    # Before PostgreSQL 16, CREATEROLE can grant membership in any non-superuser role,
    # including the schema owner.
    "createrole",
    # An owner can disable or replace policies and alter or drop tables.
    "owns_schema",
    "owns_relation",
    # New objects could shadow the application's tables or run code as other roles.
    "schema_create",
    "database_create",
    # It could change the recorded schema revision, or provision and remove workspaces.
    "writes_schema_revision",
    "writes_workspaces",
    # It could not perform tenant-scoped workspace reads.
    "cannot_read_workspaces",
    "workspaces_without_row_security",
)

# The application schema is the one holding `alembic_version`. Privilege functions take
# regclass OIDs, which are NULL for a missing table instead of an error. Column-level grants
# count as writes too: has_any_column_privilege includes them, has_table_privilege does not.
ROLE_CHECK: LiteralString = """/* sage-bot runtime role check */
WITH held AS (
    SELECT r.oid, r.rolsuper, r.rolbypassrls, r.rolcreaterole
    FROM pg_catalog.pg_roles r
    WHERE pg_catalog.pg_has_role(current_user, r.oid, 'MEMBER')
),
app AS (
    SELECT c.relnamespace AS schema_oid
    FROM pg_catalog.pg_class c
    WHERE c.oid = pg_catalog.to_regclass('alembic_version')
),
tables AS (
    SELECT pg_catalog.to_regclass('alembic_version') AS revision,
           pg_catalog.to_regclass('slack_workspaces') AS workspaces
)
SELECT pg_catalog.concat_ws(',',
    CASE WHEN EXISTS (SELECT 1 FROM held WHERE rolsuper) THEN 'superuser' END,
    CASE WHEN EXISTS (SELECT 1 FROM held WHERE rolbypassrls) THEN 'bypassrls' END,
    CASE WHEN EXISTS (SELECT 1 FROM held WHERE rolcreaterole) THEN 'createrole' END,
    CASE WHEN EXISTS (
        SELECT 1 FROM app
        JOIN pg_catalog.pg_namespace n ON n.oid = app.schema_oid
        JOIN held ON held.oid = n.nspowner
    ) THEN 'owns_schema' END,
    CASE WHEN EXISTS (
        SELECT 1 FROM app
        JOIN pg_catalog.pg_class c ON c.relnamespace = app.schema_oid
        JOIN held ON held.oid = c.relowner
    ) THEN 'owns_relation' END,
    CASE WHEN EXISTS (
        SELECT 1 FROM app WHERE pg_catalog.has_schema_privilege(app.schema_oid, 'CREATE')
    ) THEN 'schema_create' END,
    CASE WHEN pg_catalog.has_database_privilege(pg_catalog.current_database(), 'CREATE')
        THEN 'database_create' END,
    CASE WHEN (SELECT pg_catalog.has_table_privilege(revision, 'DELETE, TRUNCATE, TRIGGER')
        OR pg_catalog.has_any_column_privilege(revision, 'INSERT, UPDATE, REFERENCES') FROM tables)
        THEN 'writes_schema_revision' END,
    CASE WHEN (SELECT pg_catalog.has_table_privilege(workspaces, 'DELETE, TRUNCATE, TRIGGER')
        OR pg_catalog.has_any_column_privilege(workspaces, 'INSERT, UPDATE, REFERENCES') FROM tables)
        THEN 'writes_workspaces' END,
    CASE WHEN (SELECT pg_catalog.has_table_privilege(workspaces, 'SELECT') FROM tables)
        IS NOT TRUE THEN 'cannot_read_workspaces' END,
    CASE WHEN (
        SELECT c.relrowsecurity FROM pg_catalog.pg_class c, tables WHERE c.oid = tables.workspaces
    ) IS NOT TRUE THEN 'workspaces_without_row_security' END
)"""


def parse_problems(value: object) -> list[str]:
    """The problem codes in a role check result; anything unexpected becomes `unknown`."""
    if not isinstance(value, str):
        return ["unknown"]
    codes = [code for code in value.split(",") if code]
    return [code if code in PROBLEMS else "unknown" for code in codes]
