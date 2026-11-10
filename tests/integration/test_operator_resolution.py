"""The trusted queries bind only pg_catalog's operators, whatever is on the search path.

PostgreSQL prefers an operator whose argument types match exactly, in any schema on the search
path, over a pg_catalog operator that needs a coercion. This plants every such operator over a
matrix of types, compiles each query as a view, and checks what the view depends on. (Function
calls in these queries are all schema-qualified; this test plants only operators.)
"""

from typing import Any, LiteralString, cast

import psycopg
import pytest
from alembic.script import ScriptDirectory
from psycopg import sql

from sage_bot.db import migrate
from sage_bot.db.runtime_role import PARAMETER_CHECK, ROLE_CHECK
from sage_bot.db.workspaces import STATEMENT_TIMEOUT

_PLANT = """
DO $plant$
DECLARE
    types pg_catalog.regtype[] := ARRAY[
        'oid', 'regclass', 'regtype', 'regnamespace', 'regproc', 'regprocedure', 'regrole',
        'int2', 'int4', 'int8', 'numeric', 'bool', 'name', 'text', '"char"', 'varchar',
        'bpchar', 'oidvector', 'aclitem', 'name[]', 'text[]', 'oid[]'
    ]::pg_catalog.regtype[];
    operators text[] := ARRAY['=', '<>', '<', '<=', '>', '>='];
    l pg_catalog.regtype;
    r pg_catalog.regtype;
    op text;
    n int := 0;
BEGIN
    FOREACH l IN ARRAY types LOOP
        FOREACH r IN ARRAY types LOOP
            FOREACH op IN ARRAY operators LOOP
                IF NOT EXISTS (
                    SELECT 1 FROM pg_catalog.pg_operator o
                    WHERE o.oprname = op AND o.oprleft = l AND o.oprright = r
                      AND o.oprnamespace = 'pg_catalog'::pg_catalog.regnamespace
                ) THEN
                    n := n + 1;
                    EXECUTE pg_catalog.format(
                        'CREATE FUNCTION plant.f%s(%s, %s) RETURNS boolean '
                        'LANGUAGE sql IMMUTABLE AS %L', n, l, r, 'SELECT false');
                    EXECUTE pg_catalog.format(
                        'CREATE OPERATOR plant.%s (LEFTARG = %s, RIGHTARG = %s, '
                        'FUNCTION = plant.f%s)', op, l, r, n);
                END IF;
            END LOOP;
        END LOOP;
    END LOOP;
END
$plant$"""

_FOREIGN_DEPENDENCIES = """
SELECT DISTINCT d.refclassid::pg_catalog.regclass::text, d.refobjid
FROM pg_catalog.pg_depend d
JOIN pg_catalog.pg_rewrite w ON w.oid = d.objid
WHERE d.classid = 'pg_catalog.pg_rewrite'::pg_catalog.regclass
  AND w.ev_class = %s::pg_catalog.regclass
  AND (
      (d.refclassid = 'pg_catalog.pg_operator'::pg_catalog.regclass AND d.refobjid IN (
          SELECT oid FROM pg_catalog.pg_operator
          WHERE oprnamespace <> 'pg_catalog'::pg_catalog.regnamespace))
      OR (d.refclassid = 'pg_catalog.pg_proc'::pg_catalog.regclass AND d.refobjid IN (
          SELECT oid FROM pg_catalog.pg_proc
          WHERE pronamespace <> 'pg_catalog'::pg_catalog.regnamespace))
  )"""


def _migration_schemas() -> LiteralString:
    revision = ScriptDirectory.from_config(migrate.alembic_config()).get_revision("0003")
    assert revision is not None
    query = revision.module.SCHEMAS
    assert isinstance(query, str)
    return cast("LiteralString", query)


def _queries() -> dict[str, LiteralString]:
    queries: dict[str, LiteralString] = {
        "role_check": ROLE_CHECK,
        "parameter_check": PARAMETER_CHECK,
        "migration_schemas": _migration_schemas(),
    }
    # The lookup's limit arrives as whatever integer type psycopg chooses.
    for kind, literal in (
        ("int2", "'5'::pg_catalog.int2"),
        ("int4", "'5'::pg_catalog.int4"),
        ("int8", "'5'::pg_catalog.int8"),
        ("numeric", "'5'::pg_catalog.numeric"),
    ):
        queries[f"statement_timeout_{kind}"] = STATEMENT_TIMEOUT.replace("%(limit)s", literal)
    return queries


@pytest.fixture
def planted(fresh_database: str) -> str:
    with psycopg.connect(fresh_database, autocommit=True) as admin:
        admin.execute("SET search_path = pg_catalog")
        admin.execute("CREATE SCHEMA plant")
        admin.execute(_PLANT)
        row = admin.execute(
            "SELECT count(*) FROM pg_operator WHERE oprnamespace = 'plant'::regnamespace"
        ).fetchone()
    assert row is not None
    assert row[0] > 1000
    return fresh_database


def _foreign_dependencies(database: str, name: str, query: LiteralString) -> list[Any]:
    view = sql.Identifier("plant", f"v_{name}")
    with psycopg.connect(database, autocommit=True) as admin:
        admin.execute('SET search_path = "$user", plant, public')
        admin.execute(sql.SQL("CREATE VIEW {} AS ").format(view) + sql.SQL(query))
        # Read the dependencies with nothing planted on the path.
        admin.execute("SET search_path = pg_catalog")
        return admin.execute(_FOREIGN_DEPENDENCIES, (f"plant.v_{name}",)).fetchall()


def test_a_captured_comparison_is_detected(planted: str) -> None:
    """The check itself works: an unqualified oid = regclass binds a planted operator."""
    control = "SELECT 1::pg_catalog.oid = 'pg_class'::pg_catalog.regclass AS captured"
    assert len(_foreign_dependencies(planted, "control", control)) == 1


@pytest.mark.parametrize("name", list(_queries()))
def test_trusted_queries_bind_only_built_in_operators(planted: str, name: str) -> None:
    assert _foreign_dependencies(planted, name, _queries()[name]) == []
