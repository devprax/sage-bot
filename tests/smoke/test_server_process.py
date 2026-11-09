"""Start the real server process, call liveness over TCP, and stop it with a signal."""

import http.client
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from typing import IO

import pytest

STARTUP_TIMEOUT = 20.0
SHUTDOWN_TIMEOUT = 10.0
STARTED = re.compile(r"Uvicorn running on http://127\.0\.0\.1:(\d+)")
STOPPED = ("Application shutdown complete.", "Finished server process")
# Runs the entrypoint with SIGINT delivered as a terminal would, even if the test runner was
# started with SIGINT ignored (a child inherits that).
LAUNCHER = (
    "import signal, sys; signal.signal(signal.SIGINT, signal.default_int_handler); "
    "from sage_bot.api.server import main; sys.exit(main(sys.argv[1:]))"
)

type Lines = queue.Queue[str | None]


def _stream_lines(stream: IO[str], lines: Lines) -> None:
    for line in iter(stream.readline, ""):
        lines.put(line)
    lines.put(None)


def _wait_for_start(lines: Lines, seen: list[str]) -> int:
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            line = lines.get(timeout=remaining)
        except queue.Empty:
            break
        if line is None:
            pytest.fail("server exited before starting:\n" + "".join(seen))
        seen.append(line)
        if match := STARTED.search(line):
            return int(match.group(1))
    pytest.fail(f"server did not start within {STARTUP_TIMEOUT}s:\n" + "".join(seen))


def _get(port: int, path: str) -> tuple[http.client.HTTPResponse, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response, response.read()
    finally:
        connection.close()


@pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX signals")
@pytest.mark.parametrize(
    ("stop", "exit_status"),
    [
        # A supervisor's stop request: graceful shutdown is a successful exit.
        (signal.SIGTERM, 0),
        # Ctrl+C: graceful shutdown, then the conventional interrupted status.
        (signal.SIGINT, 130),
    ],
)
def test_server_starts_answers_liveness_and_shuts_down_cleanly(
    stop: signal.Signals, exit_status: int
) -> None:
    # No credentials or service settings: liveness must not need them.
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.upper().startswith("SAGE_") and name.upper() != "DATABASE_URL"
    }
    process = subprocess.Popen(  # noqa: S603 -- fixed argv: this interpreter + LAUNCHER
        [sys.executable, "-c", LAUNCHER, "--port", "0"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    lines: Lines = queue.Queue()
    reader = threading.Thread(target=_stream_lines, args=(process.stdout, lines), daemon=True)
    seen: list[str] = []
    try:
        assert process.stdout is not None  # requested as a pipe
        reader.start()
        port = _wait_for_start(lines, seen)

        response, body = _get(port, "/health/live")
        assert response.status == 200
        assert response.getheader("content-type") == "application/json"
        assert response.getheader("server") is None
        assert body == b'{"status":"ok"}'
        assert _get(port, "/docs")[0].status == 404

        # Started without settings: alive, but not ready, and without saying why.
        ready, ready_body = _get(port, "/health/ready")
        assert ready.status == 503
        assert ready_body == b'{"status":"not_ready"}'

        process.send_signal(stop)
        assert process.wait(timeout=SHUTDOWN_TIMEOUT) == exit_status
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=SHUTDOWN_TIMEOUT)
        if reader.is_alive():
            reader.join(timeout=SHUTDOWN_TIMEOUT)
        process.stdout.close()

    while (line := lines.get_nowait() if not lines.empty() else None) is not None:
        seen.append(line)
    output = "".join(seen)
    assert all(marker in output for marker in STOPPED), output
    assert "Traceback" not in output, output
