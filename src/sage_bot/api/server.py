"""Process entrypoint: serve the HTTP API with Uvicorn.

Run `sage-bot-server` or `python -m sage_bot.api.server`. The server binds to the local
interface unless a host is given explicitly. SIGTERM and Ctrl+C shut it down gracefully; the
process then exits 0 after SIGTERM and 130 after Ctrl+C.
"""

import argparse
import signal
from collections.abc import Sequence
from types import FrameType

import uvicorn

from sage_bot.api.app import create_app

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
# Conventional exit status for a process stopped by Ctrl+C (128 + SIGINT).
INTERRUPTED = 130


def _host(value: str) -> str:
    if not value or any(character.isspace() for character in value):
        raise argparse.ArgumentTypeError("host must be a non-empty name or address")
    return value


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("port must be an integer") from None
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 0 and 65535")
    return port


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="sage-bot-server", description="Serve the Sage Bot API.")
    parser.add_argument(
        "--host",
        type=_host,
        default=DEFAULT_HOST,
        help=f"interface to bind; IPv6 without brackets (default: {DEFAULT_HOST}, local only)",
    )
    parser.add_argument(
        "--port",
        type=_port,
        default=DEFAULT_PORT,
        help=f"TCP port to bind; 0 picks a free port (default: {DEFAULT_PORT})",
    )
    return parser.parse_args(argv)


def build_server(host: str, port: int) -> uvicorn.Server:
    config = uvicorn.Config(
        create_app(),
        host=host,
        port=port,
        # Do not advertise the server software.
        server_header=False,
        # Forwarded headers are not trusted until a deployment defines its proxies.
        proxy_headers=False,
        # Request lines can carry credentials in query strings; do not log them.
        access_log=False,
    )
    return uvicorn.Server(config)


class _Terminated(Exception):
    """SIGTERM arrived; Uvicorn re-raises it once it has shut down gracefully."""


def _on_sigterm(signum: int, frame: FrameType | None) -> None:
    raise _Terminated


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    server = build_server(args.host, args.port)
    # Uvicorn handles SIGTERM and SIGINT while serving, then restores these handlers and
    # re-raises the signal. A requested stop that completed is a success, not a signal death.
    previous = signal.signal(signal.SIGTERM, _on_sigterm)
    try:
        server.run()
    except _Terminated:
        return 0
    except KeyboardInterrupt:
        return INTERRUPTED
    finally:
        signal.signal(signal.SIGTERM, previous)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
