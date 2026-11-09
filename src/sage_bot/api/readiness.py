"""Readiness: whether the service can do work, as opposed to whether the process is alive.

The service is ready when its settings are valid and PostgreSQL reports the schema revision
this release expects, within the readiness timeout.

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
from sage_bot.db.migrate import expected_revision

logger = logging.getLogger(__name__)

# Identifies probe connections in pg_stat_activity.
APPLICATION_NAME = "sage-bot-readiness"
# Bound for releasing a probe after its deadline. Closing is local, and psycopg ends the query
# at its next poll (tens of milliseconds). Only if it did not would a response exceed the
# deadline, by at most 2 * CLEANUP_SECONDS.
CLEANUP_SECONDS = 1.0

type ReadinessCheck = Callable[[], Coroutine[Any, Any, bool]]


async def not_configured() -> bool:
    """The service has no usable configuration, so it cannot do work."""
    return False


def postgres_check(
    database_url: str, *, timeout_seconds: float, expected_revision: str
) -> ReadinessCheck:
    """Ready when PostgreSQL answers within the timeout with the expected schema revision.

    The revision is the one Alembic records in `alembic_version`; a database that has not been
    migrated, or is at another revision, is not ready.

    Concurrent probes share one in-flight check, so a burst of probes opens one database
    connection rather than one per request. Results are not cached: the next probe after a
    check completes runs a new one.
    """
    connect_timeout = max(1, math.ceil(timeout_seconds))  # libpq's own bound, whole seconds
    in_flight: asyncio.Task[bool] | None = None

    async def probe() -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        connection: psycopg.AsyncConnection[Any] | None = None
        query: asyncio.Task[Any] | None = None
        try:
            async with asyncio.timeout_at(deadline):
                connection = await psycopg.AsyncConnection.connect(
                    database_url,
                    # No transaction, so closing never needs a rollback round trip.
                    autocommit=True,
                    connect_timeout=connect_timeout,
                    application_name=APPLICATION_NAME,
                )
            # The query is never cancelled: psycopg answers cancellation with a server-side
            # cancel request and further waiting, which the deadline would not bound. If the
            # deadline passes, closing the connection ends the query instead.
            query = asyncio.create_task(_recorded_revisions(connection))
            done, _ = await asyncio.wait({query}, timeout=max(0.0, deadline - loop.time()))
            if query not in done:
                raise TimeoutError
            revisions = await query
        except Exception as error:  # any failure means not ready
            logger.warning("readiness: database check failed (%s)", type(error).__name__)
            return False
        finally:
            await _discard(connection, query)
        if revisions != [(expected_revision,)]:
            logger.warning("readiness: database schema is not at the expected revision")
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


async def _recorded_revisions(connection: psycopg.AsyncConnection[Any]) -> list[Any]:
    cursor = await connection.execute("SELECT version_num FROM alembic_version")
    return await cursor.fetchall()


async def _discard(
    connection: psycopg.AsyncConnection[Any] | None, query: asyncio.Task[Any] | None
) -> None:
    """Release a probe's connection and query without waiting on the server."""
    if connection is not None:
        # libpq's PQfinish: closes the socket locally and does not wait for the server.
        await connection.close()
    if query is None:
        return
    if not query.done():
        # With its socket closed, the query fails at psycopg's next poll; bound the wait.
        await asyncio.wait({query}, timeout=CLEANUP_SECONDS)
    if not query.done():
        # Last resort. The connection is closed, so psycopg sends no cancel request.
        query.cancel()
        await asyncio.wait({query}, timeout=CLEANUP_SECONDS)
    if query.done() and not query.cancelled():
        query.exception()  # retrieved, so asyncio does not report it as unhandled


def readiness_from_environment() -> ReadinessCheck:
    """Build the readiness check from process settings; never logs values.

    Missing or invalid settings yield a check that reports not ready. A packaging defect (not
    exactly one migration head) raises, so a broken build fails at startup rather than serving.
    """
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
        expected_revision=expected_revision(),
    )
