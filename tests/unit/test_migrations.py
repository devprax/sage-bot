import base64
import os
from importlib.resources import files

import pytest
from alembic.script import ScriptDirectory

from sage_bot.db import migrate

PASSWORD = "migrate-test-password"  # noqa: S105
ENCRYPTION_KEY = base64.b64encode(bytes(range(32))).decode()
SETTINGS_VARIABLES = {
    "SAGE_ENV",
    "DATABASE_URL",
    "MIGRATION_DATABASE_URL",
    "SAGE_ENCRYPTION_KEY",
    "READINESS_TIMEOUT_SECONDS",
}


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.upper() in SETTINGS_VARIABLES:
            monkeypatch.delenv(name)


@pytest.fixture
def valid_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    # Only the migration URL: the command needs no runtime settings.
    monkeypatch.setenv(
        "MIGRATION_DATABASE_URL", f"postgresql://sage:{PASSWORD}@127.0.0.1:5432/sage"
    )


def test_the_single_migration_head_is_the_expected_revision() -> None:
    heads = ScriptDirectory.from_config(migrate.alembic_config()).get_heads()
    assert heads == [migrate.expected_revision()]
    assert migrate.expected_revision() == "0002"


def test_migrations_ship_inside_the_package() -> None:
    migrations = files("sage_bot.db") / "migrations"
    assert (migrations / "env.py").is_file()
    assert any(entry.name.endswith(".py") for entry in (migrations / "versions").iterdir())


def test_sqlalchemy_url_uses_the_psycopg_driver() -> None:
    url = f"postgresql://sage:{PASSWORD}@db.example:5432/sage"
    assert (
        migrate.sqlalchemy_url(url) == f"postgresql+psycopg://sage:{PASSWORD}@db.example:5432/sage"
    )


def test_database_url_is_not_stored_in_the_alembic_options() -> None:
    config = migrate.alembic_config(f"postgresql://sage:{PASSWORD}@127.0.0.1/sage")
    assert config.get_main_option("sqlalchemy.url") is None
    assert PASSWORD not in repr(config.get_section(config.config_ini_section))


def test_invalid_settings_exit_2_without_values(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MIGRATION_DATABASE_URL", f"sqlite:///{PASSWORD}.db")
    assert migrate.main([]) == 2
    output = capsys.readouterr()
    assert "settings are missing or invalid" in output.err
    assert PASSWORD not in output.out + output.err


def test_runtime_credentials_are_never_used_for_migration(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    upgraded: list[str] = []
    monkeypatch.setattr(migrate, "upgrade", upgraded.append)
    monkeypatch.setenv("DATABASE_URL", f"postgresql://sage:{PASSWORD}@127.0.0.1:5432/sage")
    monkeypatch.setenv("SAGE_ENCRYPTION_KEY", ENCRYPTION_KEY)
    assert migrate.main([]) == 2
    assert upgraded == []
    output = capsys.readouterr()
    assert "settings are missing or invalid (1 errors)" in output.err
    assert PASSWORD not in output.out + output.err


@pytest.mark.usefixtures("valid_settings")
def test_failed_migration_exits_1_reporting_only_the_error_type(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def failing(database_url: str) -> None:
        raise RuntimeError(f"could not connect to {database_url}")

    monkeypatch.setattr(migrate, "upgrade", failing)
    assert migrate.main([]) == 1
    output = capsys.readouterr()
    assert "migration failed (RuntimeError)" in output.err
    assert PASSWORD not in output.out + output.err


@pytest.mark.usefixtures("valid_settings")
def test_successful_migration_reports_the_revision(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    upgraded: list[str] = []
    monkeypatch.setattr(migrate, "upgrade", upgraded.append)
    assert migrate.main([]) == 0
    assert upgraded == [f"postgresql://sage:{PASSWORD}@127.0.0.1:5432/sage"]
    output = capsys.readouterr()
    assert output.out == f"database schema is at revision {migrate.expected_revision()}\n"
    assert PASSWORD not in output.out + output.err


def test_running_alembic_without_a_database_url_fails_clearly() -> None:
    from alembic import command

    with pytest.raises(RuntimeError, match="no database URL configured"):
        command.upgrade(migrate.alembic_config(), "head")
