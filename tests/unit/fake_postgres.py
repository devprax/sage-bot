"""A minimal PostgreSQL wire-protocol server for readiness tests.

It completes the startup handshake (no TLS, no password) and then either answers each query
with one text row, or stalls without replying, which lets tests reproduce a database that
accepts a query and stops responding. The runtime role check gets `role_problems` (empty: a
safe role); any other query gets `revision`, as `SELECT version_num FROM alembic_version`
would. It counts open client connections and cancel requests.
"""

import socket
import struct
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager

from sage_bot.db.runtime_role import ROLE_CHECK_MARKER

SSL_REQUEST = 80877103
GSS_REQUEST = 80877104
CANCEL_REQUEST = 80877102
PARAMETERS = {
    "server_version": "17.0",
    "server_encoding": "UTF8",
    "client_encoding": "UTF8",
    "DateStyle": "ISO, MDY",
    "TimeZone": "UTC",
    "integer_datetimes": "on",
    "standard_conforming_strings": "on",
}


def _message(kind: bytes, body: bytes = b"") -> bytes:
    return kind + struct.pack("!I", len(body) + 4) + body


# One text column named version_num (type oid 25, variable length, text format).
_ROW_DESCRIPTION = _message(
    b"T", struct.pack("!H", 1) + b"version_num\0" + struct.pack("!IhIhih", 0, 0, 25, -1, -1, 0)
)


def _read_exact(connection: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = connection.recv(size - len(data))
        if not chunk:
            raise ConnectionError("client closed")
        data += chunk
    return data


class FakePostgres:
    def __init__(self) -> None:
        self.stall = True
        self.revision = "0001"
        self.role_problems = ""
        self.cancel_requests = 0
        self._open = 0
        self._lock = threading.Lock()
        self._listener = socket.socket()
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(16)
        self._listener.settimeout(0.1)
        self._stopping = threading.Event()
        self._thread = threading.Thread(target=self._accept, daemon=True)

    @property
    def port(self) -> int:
        return int(self._listener.getsockname()[1])

    @property
    def open_connections(self) -> int:
        with self._lock:
            return self._open

    def wait_until_closed(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.open_connections == 0:
                return True
            time.sleep(0.01)
        return self.open_connections == 0

    def _accept(self) -> None:
        while not self._stopping.is_set():
            try:
                connection, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(target=self._serve, args=(connection,), daemon=True).start()

    def _serve(self, connection: socket.socket) -> None:
        with connection:
            try:
                if not self._handshake(connection):
                    return
                with self._lock:
                    self._open += 1
                try:
                    self._session(connection)
                finally:
                    with self._lock:
                        self._open -= 1
            except (ConnectionError, OSError):
                return

    def _handshake(self, connection: socket.socket) -> bool:
        while True:
            (length,) = struct.unpack("!I", _read_exact(connection, 4))
            payload = _read_exact(connection, length - 4)
            (code,) = struct.unpack("!I", payload[:4])
            if code in (SSL_REQUEST, GSS_REQUEST):
                connection.sendall(b"N")
                continue
            if code == CANCEL_REQUEST:
                with self._lock:
                    self.cancel_requests += 1
                return False
            reply = _message(b"R", struct.pack("!I", 0))  # AuthenticationOk
            for name, value in PARAMETERS.items():
                reply += _message(b"S", name.encode() + b"\0" + value.encode() + b"\0")
            reply += _message(b"K", struct.pack("!II", 4242, 2424))
            reply += _message(b"Z", b"I")
            connection.sendall(reply)
            return True

    def _session(self, connection: socket.socket) -> None:
        query = b""
        while True:
            kind = _read_exact(connection, 1)
            (length,) = struct.unpack("!I", _read_exact(connection, 4))
            body = _read_exact(connection, length - 4)
            if kind == b"X":  # Terminate
                return
            if kind == b"P":  # Parse: statement name, then the query text
                query = body.split(b"\0")[1]
            elif kind == b"Q":
                query = body.split(b"\0")[0]
            if self.stall:
                continue  # accept the query, never answer it
            if kind == b"P":
                connection.sendall(_message(b"1"))  # ParseComplete
            elif kind == b"B":
                connection.sendall(_message(b"2"))  # BindComplete
            elif kind == b"D":
                connection.sendall(_ROW_DESCRIPTION)
            elif kind == b"E":
                connection.sendall(self._row(query) + _message(b"C", b"SELECT 1\0"))
            elif kind == b"S":
                connection.sendall(_message(b"Z", b"I"))
            elif kind == b"Q":
                connection.sendall(
                    _ROW_DESCRIPTION
                    + self._row(query)
                    + _message(b"C", b"SELECT 1\0")
                    + _message(b"Z", b"I")
                )

    def _row(self, query: bytes) -> bytes:
        answer = self.role_problems if ROLE_CHECK_MARKER.encode() in query else self.revision
        value = answer.encode()
        return _message(b"D", struct.pack("!HI", 1, len(value)) + value)  # DataRow

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        self._listener.close()
        self._thread.join(timeout=2)


@contextmanager
def fake_postgres() -> Generator[FakePostgres]:
    server = FakePostgres()
    server.start()
    try:
        yield server
    finally:
        server.stop()
