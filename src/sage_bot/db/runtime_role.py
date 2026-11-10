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
    # It could create databases it owns; the deployment's restricted role has no need to.
    "createdb",
    # Replication connections can copy the data files, bypassing every permission check.
    "replication",
    # Server programs and files reach the database's data outside any permission check.
    "server_files_or_programs",
    # It could terminate other sessions, including migrations.
    "signal_backend",
    # It belongs to a predefined role (pg_monitor, pg_read_all_data, ...). Their powers are
    # implicit rather than recorded as grants, so none is accepted.
    "predefined_role",
    # It, or a role it belongs to, holds a grant beyond the runtime allow-list: SELECT on
    # alembic_version and slack_workspaces, USAGE on the application schema, and CONNECT or
    # TEMPORARY on databases. This covers every object kind with grants, column grants, default
    # privileges for future objects, and user mappings.
    "unexpected_privilege",
    # The deployment granted PUBLIC more than an object starts with: initdb's or an
    # extension's initial privileges, or else PostgreSQL's default for the object kind. Every
    # role, including the runtime role, holds such grants; for example EXECUTE on
    # pg_read_binary_file, SELECT on pg_statistic, USAGE on a foreign server, or a user
    # mapping for PUBLIC.
    "public_privilege",
    # It was granted a server parameter (PostgreSQL 15+), which can load code or change how
    # the server enforces permissions.
    "parameter_grant",
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
# confer privileges by inheritance, by `SET ROLE` or by `ADMIN` (which can grant either).
# Privileges are evaluated for every such role, not only for the current one, since a
# privilege reachable through `SET ROLE` is as usable as an inherited one. The application schema is the one holding
# `alembic_version`, and both tables are resolved in it rather than through the search path.
# Column-level grants count as writes: has_any_column_privilege includes them.
ROLE_CHECK: LiteralString = """/* sage-bot runtime role check */
WITH held AS (
    SELECT r.oid, r.rolname, r.rolsuper, r.rolbypassrls, r.rolcreaterole, r.rolcreatedb,
        r.rolreplication
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
acl_objects AS (
    -- Every object kind with grants: catalog, object, column (0 for none), the kind letter
    -- acldefault() takes, owner, ACL, and the schema it is in (for schemas, itself).
    SELECT 'pg_catalog.pg_class'::pg_catalog.regclass AS catalog, c.oid AS object,
           0 AS subobject,
           CASE WHEN c.relkind = 'S' THEN 's' ELSE 'r' END::"char" AS kind,
           c.relowner AS owner, c.relacl AS acl, c.relnamespace AS schema
    FROM pg_catalog.pg_class c WHERE c.relacl IS NOT NULL
    UNION ALL
    SELECT 'pg_catalog.pg_class'::pg_catalog.regclass, t.attrelid, t.attnum, 'c', c.relowner,
           t.attacl, c.relnamespace
    FROM pg_catalog.pg_attribute t JOIN pg_catalog.pg_class c ON c.oid = t.attrelid
    WHERE t.attacl IS NOT NULL
    UNION ALL
    SELECT 'pg_catalog.pg_proc'::pg_catalog.regclass, f.oid, 0, 'f', f.proowner, f.proacl,
           f.pronamespace
    FROM pg_catalog.pg_proc f WHERE f.proacl IS NOT NULL
    UNION ALL
    SELECT 'pg_catalog.pg_namespace'::pg_catalog.regclass, n.oid, 0, 'n', n.nspowner, n.nspacl,
           n.oid
    FROM pg_catalog.pg_namespace n WHERE n.nspacl IS NOT NULL
    UNION ALL
    SELECT 'pg_catalog.pg_database'::pg_catalog.regclass, d.oid, 0, 'd', d.datdba, d.datacl,
           NULL::oid
    FROM pg_catalog.pg_database d WHERE d.datacl IS NOT NULL
    UNION ALL
    SELECT 'pg_catalog.pg_type'::pg_catalog.regclass, t.oid, 0, 'T', t.typowner, t.typacl,
           t.typnamespace
    FROM pg_catalog.pg_type t WHERE t.typacl IS NOT NULL
    UNION ALL
    SELECT 'pg_catalog.pg_language'::pg_catalog.regclass, l.oid, 0, 'l', l.lanowner, l.lanacl,
           NULL::oid
    FROM pg_catalog.pg_language l WHERE l.lanacl IS NOT NULL
    UNION ALL
    SELECT 'pg_catalog.pg_foreign_data_wrapper'::pg_catalog.regclass, w.oid, 0, 'F',
           w.fdwowner, w.fdwacl, NULL::oid
    FROM pg_catalog.pg_foreign_data_wrapper w WHERE w.fdwacl IS NOT NULL
    UNION ALL
    SELECT 'pg_catalog.pg_foreign_server'::pg_catalog.regclass, v.oid, 0, 'S', v.srvowner,
           v.srvacl, NULL::oid
    FROM pg_catalog.pg_foreign_server v WHERE v.srvacl IS NOT NULL
    UNION ALL
    SELECT 'pg_catalog.pg_largeobject'::pg_catalog.regclass, m.oid, 0, 'L', m.lomowner,
           m.lomacl, NULL::oid
    FROM pg_catalog.pg_largeobject_metadata m WHERE m.lomacl IS NOT NULL
    UNION ALL
    SELECT 'pg_catalog.pg_tablespace'::pg_catalog.regclass, s.oid, 0, 't', s.spcowner, s.spcacl,
           NULL::oid
    FROM pg_catalog.pg_tablespace s WHERE s.spcacl IS NOT NULL
    UNION ALL
    -- Default privileges apply to future objects; they have no initial privileges.
    SELECT 'pg_catalog.pg_default_acl'::pg_catalog.regclass, d.oid, 0,
           CASE d.defaclobjtype WHEN 'S' THEN 's' ELSE d.defaclobjtype END::"char",
           d.defaclrole, d.defaclacl, d.defaclnamespace
    FROM pg_catalog.pg_default_acl d
),
grants AS (
    -- Grants to the runtime role, a role it belongs to, or PUBLIC (grantee 0).
    SELECT o.catalog, o.object, o.subobject, o.kind, o.owner, a.grantee, a.privilege_type,
        -- The runtime allow-list. Never NULL, so NOT allowed is never NULL either.
        COALESCE(
            (o.catalog = 'pg_catalog.pg_class'::pg_catalog.regclass AND o.subobject = 0
                AND a.privilege_type = 'SELECT'
                AND (o.object IS NOT DISTINCT FROM tables.revision
                     OR o.object IS NOT DISTINCT FROM tables.workspaces))
            OR (o.catalog = 'pg_catalog.pg_namespace'::pg_catalog.regclass
                AND a.privilege_type = 'USAGE'
                AND o.object IN (SELECT schema_oid FROM app))
            OR (o.catalog = 'pg_catalog.pg_database'::pg_catalog.regclass
                AND a.privilege_type IN ('CONNECT', 'TEMPORARY')),
            false
        ) AS allowed,
        -- For PUBLIC: what the object started with. Initial privileges recorded by initdb or
        -- an extension, or else PostgreSQL's built-in default for the object kind. initdb
        -- grants the information_schema views to PUBLIC without recording initial privileges;
        -- those views show only what the caller may already see.
        COALESCE((o.catalog <> 'pg_catalog.pg_default_acl'::pg_catalog.regclass
            AND o.schema IS NOT DISTINCT FROM 'information_schema'::pg_catalog.regnamespace
            AND a.privilege_type IN ('SELECT', 'USAGE'))
        OR CASE WHEN o.catalog = 'pg_catalog.pg_default_acl'::pg_catalog.regclass
                  OR NOT EXISTS (
                      SELECT 1 FROM pg_catalog.pg_init_privs i
                      WHERE i.classoid = o.catalog AND i.objoid = o.object
                        AND i.objsubid = o.subobject
                  )
            THEN EXISTS (
                SELECT 1 FROM pg_catalog.aclexplode(pg_catalog.acldefault(o.kind, o.owner)) d
                WHERE d.grantee = 0 AND d.privilege_type = a.privilege_type
            )
            ELSE EXISTS (
                SELECT 1 FROM pg_catalog.pg_init_privs i, pg_catalog.aclexplode(i.initprivs) d
                WHERE i.classoid = o.catalog AND i.objoid = o.object
                  AND i.objsubid = o.subobject
                  AND d.grantee = 0 AND d.privilege_type = a.privilege_type
            )
        END, false) AS initial
    FROM acl_objects o, tables, pg_catalog.aclexplode(o.acl) a
    WHERE a.grantee = 0 OR a.grantee IN (SELECT oid FROM held)
),
writes AS (
    SELECT
        EXISTS (
            SELECT 1 FROM held
            WHERE pg_catalog.has_table_privilege(held.oid, revision, 'DELETE, TRUNCATE, TRIGGER')
               OR pg_catalog.has_any_column_privilege(
                   held.oid, revision, 'INSERT, UPDATE, REFERENCES'
               )
               OR CASE WHEN has_maintain
                   THEN pg_catalog.has_table_privilege(held.oid, revision, 'MAINTAIN') END
        ) AS revision,
        EXISTS (
            SELECT 1 FROM held
            WHERE pg_catalog.has_table_privilege(held.oid, workspaces, 'DELETE, TRUNCATE, TRIGGER')
               OR pg_catalog.has_any_column_privilege(
                   held.oid, workspaces, 'INSERT, UPDATE, REFERENCES'
               )
               OR CASE WHEN has_maintain
                   THEN pg_catalog.has_table_privilege(held.oid, workspaces, 'MAINTAIN') END
        ) AS workspaces
    FROM tables
)
SELECT pg_catalog.concat_ws(',',
    CASE WHEN current_user <> session_user THEN 'session_role_differs' END,
    CASE WHEN EXISTS (SELECT 1 FROM held WHERE rolsuper) THEN 'superuser' END,
    CASE WHEN EXISTS (SELECT 1 FROM held WHERE rolbypassrls) THEN 'bypassrls' END,
    CASE WHEN EXISTS (SELECT 1 FROM held WHERE rolcreaterole) THEN 'createrole' END,
    CASE WHEN EXISTS (SELECT 1 FROM held WHERE rolcreatedb) THEN 'createdb' END,
    CASE WHEN EXISTS (SELECT 1 FROM held WHERE rolreplication) THEN 'replication' END,
    CASE WHEN EXISTS (
        SELECT 1 FROM held WHERE rolname IN (
            'pg_execute_server_program', 'pg_read_server_files', 'pg_write_server_files'
        )
    ) THEN 'server_files_or_programs' END,
    CASE WHEN EXISTS (SELECT 1 FROM held WHERE rolname = 'pg_signal_backend')
        THEN 'signal_backend' END,
    CASE WHEN EXISTS (SELECT 1 FROM held WHERE pg_catalog.starts_with(rolname, 'pg_'))
        THEN 'predefined_role' END,
    CASE WHEN EXISTS (
        SELECT 1 FROM grants WHERE grantee <> 0 AND NOT allowed
    ) THEN 'unexpected_privilege' END,
    CASE WHEN EXISTS (
        SELECT 1 FROM grants WHERE grantee = 0 AND NOT allowed AND NOT initial
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_user_mappings m WHERE m.umuser = 0
    ) THEN 'public_privilege' END,
    CASE WHEN EXISTS (
        SELECT 1 FROM pg_catalog.pg_user_mappings m WHERE m.umuser IN (SELECT oid FROM held)
    ) THEN 'unexpected_privilege' END,
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
        SELECT 1 FROM held, schemas
        WHERE pg_catalog.has_schema_privilege(held.oid, schemas.schema_oid, 'CREATE')
    ) THEN 'schema_create' END,
    CASE WHEN EXISTS (
        SELECT 1 FROM held
        WHERE pg_catalog.has_database_privilege(held.oid, pg_catalog.current_database(), 'CREATE')
    ) THEN 'database_create' END,
    CASE WHEN (SELECT revision FROM writes) THEN 'writes_schema_revision' END,
    CASE WHEN (SELECT workspaces FROM writes) THEN 'writes_workspaces' END,
    CASE WHEN (SELECT pg_catalog.has_table_privilege(workspaces, 'SELECT') FROM tables)
        IS NOT TRUE THEN 'cannot_read_workspaces' END,
    CASE WHEN (
        SELECT c.relrowsecurity FROM pg_catalog.pg_class c, tables WHERE c.oid = tables.workspaces
    ) IS NOT TRUE THEN 'workspaces_without_row_security' END
)"""


# pg_parameter_acl exists from PostgreSQL 15; run this only there. Any ACL entry for a role it
# belongs to, or for PUBLIC, counts.
PARAMETER_CHECK: LiteralString = """/* sage-bot runtime role check: parameters */
WITH held AS (
    SELECT r.oid
    FROM pg_catalog.pg_roles r
    WHERE pg_catalog.pg_has_role(session_user, r.oid, 'MEMBER')
       OR pg_catalog.pg_has_role(current_user, r.oid, 'MEMBER')
)
SELECT CASE WHEN EXISTS (
    SELECT 1
    FROM pg_catalog.pg_parameter_acl p, pg_catalog.aclexplode(p.paracl) a
    WHERE a.grantee = 0 OR a.grantee IN (SELECT oid FROM held)
) THEN 'parameter_grant' ELSE '' END"""
PARAMETER_CHECK_MIN_SERVER_VERSION = 150000


def parse_problems(value: object) -> list[str]:
    """The problem codes in a role check result; anything unexpected becomes `unknown`."""
    if not isinstance(value, str):
        return ["unknown"]
    codes = [code if code in PROBLEMS else "unknown" for code in value.split(",") if code]
    return list(dict.fromkeys(codes))
