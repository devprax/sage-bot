"""Alembic environment: run migrations online through SQLAlchemy and psycopg.

The database URL arrives in `config.attributes`, never in the ini-style options, so it is
not interpolated, stored or logged. Engine errors hide statement parameters.
"""

from alembic import context
from sqlalchemy import create_engine, pool, text

if context.is_offline_mode():
    raise RuntimeError("offline (SQL script) migrations are not supported")

url = context.config.attributes.get("sqlalchemy_url")
if url is None:
    raise RuntimeError("no database URL configured; run migrations with sage-bot-migrate")

engine = create_engine(
    url,
    poolclass=pool.NullPool,
    hide_parameters=True,
    connect_args={"connect_timeout": 10},
)
try:
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            # Serialize concurrent runs with a transaction-scoped advisory lock, released on
            # commit or rollback. A runner that waited sees the revision the first one
            # committed; one that waits longer than the lock timeout fails instead of hanging.
            connection.execute(
                text("SELECT set_config('lock_timeout', :timeout, true)"),
                {"timeout": f"{context.config.attributes['lock_timeout_ms']}ms"},
            )
            connection.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": context.config.attributes["lock_key"]},
            )
            context.run_migrations()
finally:
    engine.dispose()
