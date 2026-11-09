import base64
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from sage_bot.config.settings import Settings

# Synthetic fixtures only; no service is contacted by these tests.
DATABASE_URL = "postgresql://test_user:test_password@localhost:5432/sage_test"
ENCRYPTION_KEY = base64.b64encode(bytes(range(32))).decode()


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    names = {"SAGE_ENV", "DATABASE_URL", "SAGE_ENCRYPTION_KEY", "READINESS_TIMEOUT_SECONDS"}
    for name in list(os.environ):
        if name.upper() in names:
            monkeypatch.delenv(name)


@pytest.fixture
def inputs() -> dict[str, str]:
    return {"DATABASE_URL": DATABASE_URL, "SAGE_ENCRYPTION_KEY": ENCRYPTION_KEY}


def parse_settings(inputs: dict[str, str]) -> Settings:
    # Exercise runtime validation, including deliberately invalid field values.
    return Settings(**inputs)  # ty: ignore[invalid-argument-type]


def test_defaults_and_secret_access(inputs: dict[str, str]) -> None:
    settings = parse_settings(inputs)
    assert settings.environment == "local"
    assert settings.readiness_timeout_seconds == 2.0
    assert settings.database_url.get_secret_value() == DATABASE_URL
    assert settings.encryption_key.get_secret_value() == ENCRYPTION_KEY


@pytest.mark.parametrize("missing", ["DATABASE_URL", "SAGE_ENCRYPTION_KEY"])
def test_credentials_are_required(inputs: dict[str, str], missing: str) -> None:
    del inputs[missing]
    with pytest.raises(ValidationError) as error:
        parse_settings(inputs)
    assert error.value.errors(include_input=False)[0]["loc"] == (missing,)
    assert DATABASE_URL not in str(error.value)
    assert ENCRYPTION_KEY not in str(error.value)


@pytest.mark.parametrize(
    "database",
    [
        "sqlite:///test.db",
        "postgresql://test_user:REPLACE_ME@localhost/sage_test",
        "postgresql://localhost/sage_test",
        "postgresql://test_user@localhost/sage_test",
        "postgresql://:test_password@localhost/sage_test",
        "postgresql://test_user:test_password@/sage_test",
        "postgresql://test_user:test_password@localhost",
        "postgresql://test_user:test_password@localhost/",
        "postgresql://test_user:test_password@localhost:bad-port/sage_test",
        "postgresql://test_user:test_password@localhost:65536/sage_test",
        "postgresql://test_user:test_password@localhost:0/sage_test",
        "postgresql://test_user:test_password@[broken-host/sage_test",
    ],
)
def test_invalid_database_is_rejected_without_disclosing_input(
    inputs: dict[str, str], database: str
) -> None:
    inputs["DATABASE_URL"] = database
    with pytest.raises(ValidationError) as error:
        parse_settings(inputs)
    assert "a PostgreSQL URL" in str(error.value)
    assert database not in str(error.value)
    assert "test_password" not in str(error.value)
    assert "bad-port" not in str(error.value)


@pytest.mark.parametrize(
    "database",
    [
        DATABASE_URL,
        "postgresql://test_user:test_password@localhost/sage_test?sslmode=require",
        "postgresql://test_user:test_password@[::1]:5432/sage_test",
        "postgresql://test_user:p%40ssword@localhost/sage_test",
    ],
)
def test_valid_database_url_is_preserved(inputs: dict[str, str], database: str) -> None:
    inputs["DATABASE_URL"] = database
    assert parse_settings(inputs).database_url.get_secret_value() == database


@pytest.mark.parametrize(
    "key",
    [
        "",
        "REPLACE_ME",
        "not-base64!",
        "non-ascii-\u00e9",
        base64.b64encode(bytes(range(31))).decode(),
        base64.b64encode(bytes(range(33))).decode(),
        base64.b64encode(b"a" * 32).decode(),
        ENCRYPTION_KEY.rstrip("="),
    ],
)
def test_invalid_key_is_rejected_without_disclosing_input(inputs: dict[str, str], key: str) -> None:
    inputs["SAGE_ENCRYPTION_KEY"] = key
    with pytest.raises(ValidationError) as error:
        parse_settings(inputs)
    assert "encryption key must encode 32 bytes" in str(error.value)
    if key:
        assert key not in str(error.value)


@pytest.mark.parametrize("environment", ["local", "test"])
def test_environment_loading(
    inputs: dict[str, str], monkeypatch: pytest.MonkeyPatch, environment: str
) -> None:
    for name, value in inputs.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("SAGE_ENV", environment)
    monkeypatch.setenv("READINESS_TIMEOUT_SECONDS", "3.5")
    settings = Settings()
    assert settings.environment == environment
    assert settings.readiness_timeout_seconds == 3.5
    assert settings.database_url.get_secret_value() == DATABASE_URL
    assert settings.encryption_key.get_secret_value() == ENCRYPTION_KEY


def test_explicit_values_override_environment(
    inputs: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", "invalid-environment-url")
    monkeypatch.setenv("SAGE_ENCRYPTION_KEY", "invalid-environment-key")
    assert parse_settings(inputs).database_url.get_secret_value() == DATABASE_URL


@pytest.mark.parametrize("environment", ["production", "staging", ""])
def test_unsupported_environment_is_rejected(inputs: dict[str, str], environment: str) -> None:
    inputs["SAGE_ENV"] = environment
    with pytest.raises(ValidationError):
        parse_settings(inputs)


@pytest.mark.parametrize("timeout", ["0", "-1", "5.1", "nan", "inf", "not-a-number"])
def test_timeout_must_be_positive_and_bounded(inputs: dict[str, str], timeout: str) -> None:
    inputs["readiness_timeout_seconds"] = timeout
    with pytest.raises(ValidationError):
        parse_settings(inputs)


def test_unknown_constructor_fields_are_rejected(inputs: dict[str, str]) -> None:
    inputs["unexpected"] = "synthetic-private-value"
    with pytest.raises(ValidationError) as error:
        parse_settings(inputs)
    assert error.value.errors(include_input=False)[0]["type"] == "extra_forbidden"
    assert "synthetic-private-value" not in str(error.value)


def test_settings_are_immutable(inputs: dict[str, str]) -> None:
    settings = parse_settings(inputs)
    with pytest.raises(ValidationError, match="Instance is frozen"):
        # Deliberately attempt a forbidden write to verify runtime immutability.
        settings.readiness_timeout_seconds = 1.0  # ty: ignore[invalid-assignment]
    assert settings.readiness_timeout_seconds == 2.0


def test_secrets_are_redacted_in_representations(inputs: dict[str, str]) -> None:
    settings = parse_settings(inputs)
    for rendered in [
        str(settings),
        repr(settings),
        str(settings.model_dump()),
        settings.model_dump_json(),
    ]:
        assert DATABASE_URL not in rendered
        assert "test_password" not in rendered
        assert ENCRYPTION_KEY not in rendered


def test_dotenv_is_not_loaded_implicitly(
    inputs: dict[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".env").write_text(
        "\n".join(f"{name}={value}" for name, value in inputs.items()), encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValidationError) as error:
        Settings()
    assert {entry["loc"] for entry in error.value.errors(include_input=False)} == {
        ("DATABASE_URL",),
        ("SAGE_ENCRYPTION_KEY",),
    }
