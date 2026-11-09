"""Validated process settings. Never log raw values or structured validation errors."""

import base64
import binascii
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        extra="forbid", frozen=True, hide_input_in_errors=True, env_file=None
    )

    environment: Literal["local", "test"] = Field("local", validation_alias="SAGE_ENV")
    database_url: SecretStr = Field(validation_alias="DATABASE_URL", repr=False)
    encryption_key: SecretStr = Field(validation_alias="SAGE_ENCRYPTION_KEY", repr=False)
    readiness_timeout_seconds: float = Field(default=2.0, gt=0, le=5)

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        message = "a PostgreSQL URL with non-placeholder credentials is required"
        try:
            parsed = urlsplit(raw)
            valid = (
                parsed.scheme == "postgresql"
                and bool(parsed.hostname)
                and bool(parsed.username)
                and bool(parsed.password)
                and parsed.path not in ("", "/")
                and (parsed.port is None or parsed.port > 0)
                and "REPLACE_ME" not in raw
            )
        except ValueError:
            # URL parser errors can include input values, including credentials.
            raise ValueError(message) from None
        if not valid:
            raise ValueError(message)
        return value

    @field_validator("encryption_key")
    @classmethod
    def validate_encryption_key(cls, value: SecretStr) -> SecretStr:
        message = "encryption key must encode 32 bytes as base64; repeated-byte keys are rejected"
        try:
            decoded = base64.b64decode(value.get_secret_value(), validate=True)
        except (ValueError, binascii.Error):
            raise ValueError(message) from None
        if len(decoded) != 32 or len(set(decoded)) < 2:
            raise ValueError(message)
        return value
