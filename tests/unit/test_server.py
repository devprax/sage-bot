import signal
import subprocess
import sys
from importlib.metadata import entry_points
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from sage_bot.api import server


def test_defaults_bind_to_the_local_interface_only() -> None:
    args = server.parse_args([])
    assert (args.host, args.port) == ("127.0.0.1", 8000)


def test_host_and_port_can_be_set_explicitly() -> None:
    args = server.parse_args(["--host", "::1", "--port", "9000"])
    assert (args.host, args.port) == ("::1", 9000)


@pytest.mark.parametrize("port", ["-1", "65536", "http", ""])
def test_invalid_ports_are_rejected(port: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        server.parse_args(["--port", port])
    assert error.value.code == 2
    assert "--port" in capsys.readouterr().err


@pytest.mark.parametrize("host", ["", "  ", "local host"])
def test_blank_or_malformed_hosts_are_rejected(host: str) -> None:
    with pytest.raises(SystemExit) as error:
        server.parse_args(["--host", host])
    assert error.value.code == 2


def test_server_serves_the_application_without_identifying_itself() -> None:
    config = server.build_server("127.0.0.1", 8123).config
    assert (config.host, config.port) == ("127.0.0.1", 8123)
    assert config.server_header is False
    assert config.proxy_headers is False
    assert config.access_log is False  # request lines can carry credentials in query strings
    assert isinstance(config.app, FastAPI)
    assert {route.path for route in config.app.routes} == {"/health/live"}  # ty: ignore[unresolved-attribute]
    assert config.app.openapi_url is None


def test_main_runs_the_configured_server(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, int]] = []

    def fake_build(host: str, port: int) -> SimpleNamespace:
        return SimpleNamespace(run=lambda: calls.append((host, port)))

    monkeypatch.setattr(server, "build_server", fake_build)
    assert server.main(["--port", "9001"]) == 0
    assert calls == [("127.0.0.1", 9001)]


def test_interrupt_after_shutdown_exits_quietly(monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupted() -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(server, "build_server", lambda host, port: SimpleNamespace(run=interrupted))
    assert server.main([]) == server.INTERRUPTED == 130


def test_sigterm_after_shutdown_is_a_successful_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    before = signal.getsignal(signal.SIGTERM)

    def stopped_by_supervisor() -> None:
        signal.raise_signal(signal.SIGTERM)  # what Uvicorn does after shutting down

    monkeypatch.setattr(
        server, "build_server", lambda host, port: SimpleNamespace(run=stopped_by_supervisor)
    )
    assert server.main([]) == 0
    assert signal.getsignal(signal.SIGTERM) is before


def test_module_entrypoint_runs_main() -> None:
    result = subprocess.run(  # noqa: S603 -- fixed argv: this interpreter + our module
        [sys.executable, "-m", "sage_bot.api.server", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("usage: sage-bot-server")


def test_console_script_runs_the_server_entrypoint() -> None:
    [script] = entry_points(group="console_scripts", name="sage-bot-server")
    assert script.load() is server.main
