"""Unit tests for the CLI entrypoint helpers: port pre-check and arg parsing."""

from __future__ import annotations

import socket

import pytest

from surplus.main import CONFIG_PATH, parse_args, port_in_use


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


def test_parse_args_overrides() -> None:
    args = parse_args(["--host", "127.0.0.1", "--port", "9999", "--config", "other.ini"])
    assert args.host == "127.0.0.1"
    assert args.port == 9999
    assert args.config == "other.ini"


def test_parse_args_rejects_non_numeric_port() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--port", "not-a-port"])


def test_default_config_path_points_at_project_root() -> None:
    assert CONFIG_PATH.name == "config.ini"
    assert (CONFIG_PATH.parent / "surplus").is_dir()
