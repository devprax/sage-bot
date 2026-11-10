"""Releasing a database connection whose query may have outlived its deadline.

Bounded operations never cancel a running query: psycopg answers cancellation with a
server-side cancel request and further waiting, which no deadline bounds. They close the
connection instead, then release the query task with `discard`.
"""

import asyncio
from typing import Any

import psycopg

# Bound for releasing a query task after its deadline. Closing is local, and psycopg ends the
# query at its next poll (tens of milliseconds). Only if it did not would an operation exceed
# its deadline, by at most 2 * CLEANUP_SECONDS.
CLEANUP_SECONDS = 1.0


async def discard(
    connection: psycopg.AsyncConnection[Any] | None, query: asyncio.Task[Any] | None
) -> None:
    """Release a connection and its query task without waiting on the server."""
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
