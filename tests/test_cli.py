"""Unit tests for the CLI entrypoint helpers: port pre-check and arg parsing."""

from __future__ import annotations

import socket
import threading
import time

import pytest

from surplus.config import AppConfig, ServerConfig, load_config, save_config
from surplus.main import (
    CONFIG_PATH,
    admin_url,
    open_browser_when_ready,
    parse_args,
    port_in_use,
    wait_for_port,
)


def test_free_port_is_not_in_use() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        free_port = sock.getsockname()[1]
    assert port_in_use("127.0.0.1", free_port) is False


def test_bound_port_is_reported_in_use() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.listen(1)
        assert port_in_use("127.0.0.1", port) is True


def test_wildcard_host_probe_does_not_crash() -> None:
    # 0.0.0.0 and :: must be probeable without raising
    assert port_in_use("0.0.0.0", 1) in (True, False)


def test_parse_args_defaults_to_config_values() -> None:
    args = parse_args([])
    assert args.host is None
    assert args.port is None
    assert args.config is None
    assert args.no_browser is False


def test_parse_args_overrides() -> None:
    args = parse_args(["--host", "127.0.0.1", "--port", "9999", "--config", "other.ini"])
    assert args.host == "127.0.0.1"
    assert args.port == 9999
    assert args.config == "other.ini"


def test_parse_args_no_browser_flag() -> None:
    assert parse_args(["--no-browser"]).no_browser is True


def test_parse_args_rejects_non_numeric_port() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--port", "not-a-port"])


def test_default_config_path_is_resolved_not_hardcoded() -> None:
    assert CONFIG_PATH.name == "config.ini"
    assert CONFIG_PATH.is_absolute()


@pytest.mark.parametrize(
    ("host", "port", "expected"),
    [
        ("127.0.0.1", 8404, "http://127.0.0.1:8404/admin"),
        ("0.0.0.0", 8404, "http://127.0.0.1:8404/admin"),
        ("::", 9000, "http://127.0.0.1:9000/admin"),
        ("", 8080, "http://127.0.0.1:8080/admin"),
        ("localhost", 8404, "http://localhost:8404/admin"),
    ],
)
def test_admin_url_uses_a_dialable_host(host: str, port: int, expected: str) -> None:
    assert admin_url(host, port) == expected


def test_wait_for_port_true_when_listening() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        assert wait_for_port("127.0.0.1", port, timeout=3.0) is True


def test_wait_for_port_false_when_nothing_listens() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        free_port = sock.getsockname()[1]
    started = time.monotonic()
    assert wait_for_port("127.0.0.1", free_port, timeout=0.6) is False
    assert time.monotonic() - started < 3.0  # it gives up, it does not hang


def test_wait_for_port_wakes_when_server_binds_late() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    def bind_later() -> None:
        time.sleep(0.4)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", port))
            sock.listen(1)
            time.sleep(1.5)

    thread = threading.Thread(target=bind_later, daemon=True)
    thread.start()
    try:
        assert wait_for_port("127.0.0.1", port, timeout=5.0) is True
    finally:
        thread.join(timeout=3)


def test_open_browser_when_ready_calls_opener_with_admin_url() -> None:
    opened: list[str] = []
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        url = admin_url("127.0.0.1", port)
        thread = open_browser_when_ready(
            url, "127.0.0.1", port, timeout=5.0, opener=opened.append
        )
        thread.join(timeout=6)

    assert opened == [url]


def test_open_browser_when_ready_does_not_open_on_dead_port() -> None:
    opened: list[str] = []
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        free_port = sock.getsockname()[1]
    thread = open_browser_when_ready(
        f"http://127.0.0.1:{free_port}/admin",
        "127.0.0.1",
        free_port,
        timeout=0.6,
        opener=opened.append,
    )
    thread.join(timeout=5)
    assert opened == []


def test_open_browser_thread_is_daemon() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        thread = open_browser_when_ready(
            admin_url("127.0.0.1", port), "127.0.0.1", port, timeout=3.0, opener=lambda url: None
        )
        assert thread.daemon is True
        thread.join(timeout=5)


def test_open_browser_setting_round_trips(tmp_path) -> None:
    path = tmp_path / "config.ini"
    cfg = AppConfig(server=ServerConfig(open_browser=False))
    save_config(cfg, path)
    assert load_config(path).server.open_browser is False

    save_config(AppConfig(server=ServerConfig(open_browser=True)), path)
    assert load_config(path).server.open_browser is True


def test_open_browser_defaults_to_true(tmp_path) -> None:
    assert load_config(tmp_path / "missing.ini").server.open_browser is True
