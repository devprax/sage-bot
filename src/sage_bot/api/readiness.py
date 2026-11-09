"""Readiness: whether the service can do work, as opposed to whether the process is alive.

A check never raises and never reports why it failed beyond an exception class name, because
dependency errors can carry credentials and infrastructure details.
"""

import asyncio
import logging
import math
from collections.abc import Callable, Coroutine
from typing import Any

import psycopg
from pydantic import ValidationError

from sage_bot.config.settings import Settings

logger = logging.getLogger(__name__)

# Identifies probe connections in pg_stat_activity.
APPLICATION_NAME = "sage-bot-readiness"

type ReadinessCheck = Callable[[], Coroutine[Any, Any, bool]]


async def not_configured() -> bool:
    """The service has no usable configuration, so it cannot do work."""
    return False


def postgres_check(database_url: str, *, timeout_seconds: float) -> ReadinessCheck:
    """Ready when PostgreSQL accepts a connection and answers a query within the timeout.

    Concurrent probes share one in-flight check, so a burst of probes opens one database
    connection rather than one per request. Results are not cached: the next probe after a
    check completes runs a new one.
    """
    connect_timeout = max(1, math.ceil(timeout_seconds))  # libpq's own bound, whole seconds
    in_flight: asyncio.Task[bool] | None = None

    async def probe() -> bool:
        try:
            async with asyncio.timeout(timeout_seconds):
                async with await psycopg.AsyncConnection.connect(
                    database_url,
                    connect_timeout=connect_timeout,
                    application_name=APPLICATION_NAME,
                ) as connection:
                    await connection.execute("SELECT 1")
        except Exception as error:  # any failure means not ready
            logger.warning("readiness: database check failed (%s)", type(error).__name__)
            return False
        return True

    async def check() -> bool:
        nonlocal in_flight
        # A probe left pending on another (closed) event loop can never finish here.
        if (
            in_flight is None
            or in_flight.done()
            or in_flight.get_loop() is not asyncio.get_running_loop()
        ):
            in_flight = asyncio.create_task(probe())
        # A caller that gives up must not cancel the probe other callers are waiting on.
        return await asyncio.shield(in_flight)

    return check


def readiness_from_environment() -> ReadinessCheck:
    """Build the readiness check from process settings; never fails, never logs values."""
    try:
        settings = Settings()
    except ValidationError as error:
        logger.warning(
            "readiness: settings are missing or invalid (%d errors); reporting not ready",
            error.error_count(),
        )
        return not_configured
    return postgres_check(
        settings.database_url.get_secret_value(),
        timeout_seconds=settings.readiness_timeout_seconds,
    )
