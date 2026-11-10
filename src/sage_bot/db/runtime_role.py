"""Whether the runtime database role is restricted enough to serve tenant data.

Row-level security protects tenants only against a role that cannot bypass or change it. The
check evaluates the connected role's effective privileges, never its name:

- Role attributes and ownership count through every role the login role or the current role
  belongs to, whether it inherits their privileges or can `SET ROLE` to them.
- Schema and table privileges are read from the catalog, including grants to `PUBLIC`, rather
  than assumed from PostgreSQL's defaults.

It returns fixed problem codes, never names or values, so they are safe to log.
"""

from typing import LiteralString

# Starts ROLE_CHECK, so a test double can recognise it; a comment to PostgreSQL.
ROLE_CHECK_MARKER = "sage-bot runtime role check"

PROBLEMS = (
    # The session switched to another role (for example a role's default `SET ROLE`), so the
    # privileges in effect are not those of the login role it can switch back to.
    "session_role_differs",
    # A role it belongs to can bypass row-level security or every permission check.
    "superuser",
    "bypassrls",
    # Before PostgreSQL 16, CREATEROLE can grant membership in any non-superuser role,
    # including the schema owner.
    "createrole",
    # Server programs and files reach the database's data outside any permission check.
    "server_files_or_programs",
    # It could terminate other sessions, including migrations.
    "signal_backend",
    # An owner can disable or replace policies, alter or drop tables, or replace code that
    # other roles run. The schemas checked are the application schema and the search path.
    "owns_schema",
    "owns_relation",
    "owns_function_or_type",
    # New objects could shadow the application's tables or run code as other roles.
    "schema_create",
    "database_create",
    # It could change the recorded schema revision, provision or remove workspaces, or lock
    # the tables (PostgreSQL 17 MAINTAIN).
    "writes_schema_revision",
    "writes_workspaces",
    # It could not perform tenant-scoped workspace reads.
    "cannot_read_workspaces",
    "workspaces_without_row_security",
)

# Roles count by membership, of the login (session) role or the current role, whether they
# confer privileges by inheritance or by `SET ROLE`. The application schema is the one holding
# `alembic_version`, and both tables are resolved in it rather than through the search path.
# Column-level grants count as writes: has_any_column_privilege includes them.
ROLE_CHECK: LiteralString = """/* sage-bot runtime role check */
WITH held AS (
    SELECT r.oid, r.rolname, r.rolsuper, r.rolbypassrls, r.rolcreaterole
    FROM pg_catalog.pg_roles r
    WHERE pg_catalog.pg_has_role(session_user, r.oid, 'MEMBER')
       OR pg_catalog.pg_has_role(current_user, r.oid, 'MEMBER')
),
app AS (
    SELECT c.relnamespace AS schema_oid
    FROM pg_catalog.pg_class c
    WHERE c.oid = pg_catalog.to_regclass('alembic_version')
),
schemas AS (
    SELECT n.oid AS schema_oid, n.nspowner
    FROM pg_catalog.pg_namespace n
    WHERE n.nspname = ANY (pg_catalog.current_schemas(false))
       OR n.oid IN (SELECT schema_oid FROM app)
),
tables AS (
    SELECT
        (SELECT c.oid FROM pg_catalog.pg_class c JOIN app ON c.relnamespace = app.schema_oid
         WHERE c.relname = 'alembic_version') AS revision,
        (SELECT c.oid FROM pg_catalog.pg_class c JOIN app ON c.relnamespace = app.schema_oid
         WHERE c.relname = 'slack_workspaces') AS workspaces,
        pg_catalog.current_setting('server_version_num')::int >= 170000 AS has_maintain
),
writes AS (
    SELECT
        pg_catalog.has_table_privilege(revision, 'DELETE, TRUNCATE, TRIGGER')
            OR pg_catalog.has_any_column_privilege(revision, 'INSERT, UPDATE, REFERENCES')
            OR CASE WHEN has_maintain
                THEN pg_catalog.has_table_privilege(revision, 'MAINTAIN') END AS revision,
        pg_catalog.has_table_privilege(workspaces, 'DELETE, TRUNCATE, TRIGGER')
            OR pg_catalog.has_any_column_privilege(workspaces, 'INSERT, UPDATE, REFERENCES')
            OR CASE WHEN has_maintain
                THEN pg_catalog.has_table_privilege(workspaces, 'MAINTAIN') END AS workspaces
    FROM tables
)
SELECT pg_catalog.concat_ws(',',
    CASE WHEN current_user <> session_user THEN 'session_role_differs' END,
    CASE WHEN EXISTS (SELECT 1 FROM held WHERE rolsuper) THEN 'superuser' END,
    CASE WHEN EXISTS (SELECT 1 FROM held WHERE rolbypassrls) THEN 'bypassrls' END,
    CASE WHEN EXISTS (SELECT 1 FROM held WHERE rolcreaterole) THEN 'createrole' END,
    CASE WHEN EXISTS (
        SELECT 1 FROM held WHERE rolname IN (
            'pg_execute_server_program', 'pg_read_server_files', 'pg_write_server_files'
        )
    ) THEN 'server_files_or_programs' END,
    CASE WHEN EXISTS (SELECT 1 FROM held WHERE rolname = 'pg_signal_backend')
        THEN 'signal_backend' END,
    CASE WHEN EXISTS (SELECT 1 FROM schemas JOIN held ON held.oid = schemas.nspowner)
        THEN 'owns_schema' END,
    CASE WHEN EXISTS (
        SELECT 1 FROM pg_catalog.pg_class c
        JOIN schemas ON c.relnamespace = schemas.schema_oid
        JOIN held ON held.oid = c.relowner
    ) THEN 'owns_relation' END,
    CASE WHEN EXISTS (
        SELECT 1 FROM pg_catalog.pg_proc f
        JOIN schemas ON f.pronamespace = schemas.schema_oid
        JOIN held ON held.oid = f.proowner
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_type t
        JOIN schemas ON t.typnamespace = schemas.schema_oid
        JOIN held ON held.oid = t.typowner
    ) THEN 'owns_function_or_type' END,
    CASE WHEN EXISTS (
        SELECT 1 FROM schemas WHERE pg_catalog.has_schema_privilege(schemas.schema_oid, 'CREATE')
    ) THEN 'schema_create' END,
    CASE WHEN pg_catalog.has_database_privilege(pg_catalog.current_database(), 'CREATE')
        THEN 'database_create' END,
    CASE WHEN (SELECT revision FROM writes) THEN 'writes_schema_revision' END,
    CASE WHEN (SELECT workspaces FROM writes) THEN 'writes_workspaces' END,
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
