"""Upgrade the database schema to this release's revision.

Run `sage-bot-migrate` or `python -m sage_bot.db.migrate` as an explicit deployment step, with
`MIGRATION_DATABASE_URL` set to the role that owns the schema; the server never migrates on
startup and never receives these credentials. Readiness reports not ready until the recorded
revision matches `expected_revision()`.
"""

import argparse
import sys
from collections.abc import Sequence

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import ValidationError

from sage_bot.config.settings import MigrationSettings

SCRIPT_LOCATION = "sage_bot.db:migrations"
# Serializes concurrent migration runs, for example two replicas starting together.
MIGRATION_LOCK_KEY = 0x5A6E_B07D_B5C8_0001
# A run waiting on another runner's lock gives up after this, rather than hanging a deploy.
MIGRATION_LOCK_TIMEOUT_SECONDS = 60


def sqlalchemy_url(database_url: str) -> str:
    """Select SQLAlchemy's psycopg driver for a validated `postgresql://` URL."""
    return database_url.replace("postgresql://", "postgresql+psycopg://", 1)


def alembic_config(
    database_url: str | None = None,
    *,
    lock_timeout_seconds: float = MIGRATION_LOCK_TIMEOUT_SECONDS,
) -> Config:
    config = Config()
    config.set_main_option("script_location", SCRIPT_LOCATION)
    if database_url is not None:
        # Kept out of the ini options: they are interpolated and can surface in errors.
        config.attributes["sqlalchemy_url"] = sqlalchemy_url(database_url)
    config.attributes["lock_key"] = MIGRATION_LOCK_KEY
    config.attributes["lock_timeout_ms"] = int(lock_timeout_seconds * 1000)
    return config


def expected_revision() -> str:
    """The single head revision this release's code expects the database to be at."""
    heads = ScriptDirectory.from_config(alembic_config()).get_heads()
    if len(heads) != 1:
        raise RuntimeError(f"expected exactly one migration head, found {len(heads)}")
    return heads[0]


def upgrade(
    database_url: str, *, lock_timeout_seconds: float = MIGRATION_LOCK_TIMEOUT_SECONDS
) -> None:
    command.upgrade(alembic_config(database_url, lock_timeout_seconds=lock_timeout_seconds), "head")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sage-bot-migrate",
        description="Upgrade the database schema to this release's revision.",
    )
    parser.parse_args(argv)
    try:
        settings = MigrationSettings()
    except ValidationError as error:
        print(
            f"sage-bot-migrate: settings are missing or invalid ({error.error_count()} errors)",
            file=sys.stderr,
        )
        return 2
    try:
        upgrade(settings.migration_database_url.get_secret_value())
    except Exception as error:  # report the type only; messages can carry connection details
        print(f"sage-bot-migrate: migration failed ({type(error).__name__})", file=sys.stderr)
        return 1
    print(f"database schema is at revision {expected_revision()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
