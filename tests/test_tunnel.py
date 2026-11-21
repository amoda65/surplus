"""Unit tests for tunnel state handling with a fake Xray/probe layer.

No process is spawned and no network is touched: the tunnel's collaborators
(server loading, binary discovery, spawning, stopping, probing) are faked.
"""

from __future__ import annotations

from pathlib import Path

from surplus.config import ProxyConfig
from surplus.subscription import Server
from surplus.tunnel import Tunnel


class FakeProc:
    returncode = None


def make_server(address: str) -> Server:
    return Server(
        name=f"route-{address.split('.')[0]}",
        protocol="vless",
        address=address,
        port=80,
        outbound={"tag": "proxy", "protocol": "vless"},
    )


def make_tunnel(proxy: ProxyConfig, tmp_path: Path):
    holder = {"cfg": proxy}
    tunnel = Tunnel(lambda: holder["cfg"], "https://api.example.test", workdir=tmp_path / "xray")
    return tunnel, holder


def patch_tunnel(monkeypatch, servers, *, good_addresses: set[str]) -> None:
    async def fake_ensure_servers(self, cfg, force: bool = False) -> bool:
        self._servers = servers
        if not servers:
            self._last_error = "no subscription configured"
            return False
        return True

    async def fake_ensure_binary(self, cfg):
        return Path("fake-xray")

    async def fake_start_server(self, binary, server, port) -> None:
        self._port = port
        self._proc = FakeProc()

    async def fake_stop_xray(self) -> None:
        self._proc = None

    async def fake_probe(self) -> bool:
        if self.cfg.mode == "external":
            return self.cfg.proxy_url in good_addresses
        server = self._servers[self._index] if self._servers else None
        return server is not None and server.address in good_addresses

    monkeypatch.setattr(Tunnel, "_ensure_servers", fake_ensure_servers)
    monkeypatch.setattr(Tunnel, "_ensure_binary", fake_ensure_binary)
    monkeypatch.setattr(Tunnel, "_start_server", fake_start_server)
    monkeypatch.setattr(Tunnel, "_stop_xray", fake_stop_xray)
    monkeypatch.setattr(Tunnel, "_probe", fake_probe)


# ----------------------------------------------------------------- interface


async def test_disabled_tunnel_is_direct(tmp_path: Path) -> None:
    tunnel, holder = make_tunnel(ProxyConfig(enabled=False), tmp_path)
    assert await tunnel.apply() == "disabled"
    assert tunnel.proxy_url() is None
    status = tunnel.status()
    assert status["enabled"] is False
    assert status["state"] == "disabled"


async def test_external_mode_uses_configured_url(tmp_path: Path) -> None:
    tunnel, holder = make_tunnel(
        ProxyConfig(enabled=True, mode="external", proxy_url="http://127.0.0.1:10809"),
        tmp_path,
    )
    assert await tunnel.apply() == "up"
    assert tunnel.proxy_url() == "http://127.0.0.1:10809"


async def test_external_mode_without_url_is_down(tmp_path: Path) -> None:
    tunnel, holder = make_tunnel(ProxyConfig(enabled=True, mode="external"), tmp_path)
    assert await tunnel.apply() == "down"
    assert tunnel.proxy_url() is None
    assert "empty" in tunnel.status()["last_error"]


async def test_external_mode_probe_failure_flips_state(
    tmp_path: Path, monkeypatch
) -> None:
    patch_tunnel(monkeypatch, [], good_addresses=set())
    tunnel, holder = make_tunnel(
        ProxyConfig(enabled=True, mode="external", proxy_url="http://127.0.0.1:9"),
        tmp_path,
    )
    await tunnel.apply()
    await tunnel.health_tick()
    assert tunnel.status()["state"] == "down"
    # external mode keeps the configured URL: no client flapping on a bad probe
    assert tunnel.proxy_url() == "http://127.0.0.1:9"
    assert "unreachable" in tunnel.error_hint()


# ------------------------------------------------------------------- managed


async def test_managed_starts_and_reports_route(tmp_path: Path, monkeypatch) -> None:
    servers = [make_server("one.example"), make_server("two.example")]
    patch_tunnel(monkeypatch, servers, good_addresses={"one.example"})
    tunnel, holder = make_tunnel(ProxyConfig(enabled=True, mode="managed"), tmp_path)
    assert await tunnel.apply() == "up"
    url = tunnel.proxy_url()
    assert url is not None and url.startswith("http://127.0.0.1:")
    status = tunnel.status()
    assert status["route"] == "1/2"
    assert status["server"]["address"] == "one.example"
    assert status["subscription"]["servers"] == 2
    assert status["enabled"] is True
    assert tunnel.error_hint() == ""


async def test_managed_rotates_to_a_working_route(tmp_path: Path, monkeypatch) -> None:
    servers = [
        make_server("one.example"),
        make_server("two.example"),
        make_server("three.example"),
    ]
    patch_tunnel(monkeypatch, servers, good_addresses={"three.example"})
    tunnel, holder = make_tunnel(ProxyConfig(enabled=True, mode="managed"), tmp_path)
    assert await tunnel.apply() == "up"
    status = tunnel.status()
    assert status["server"]["address"] == "three.example"
    assert status["route"] == "3/3"
    assert status["health"]["rotations"] == 2
    assert tunnel.proxy_url() is not None


async def test_managed_down_when_no_route_works(tmp_path: Path, monkeypatch) -> None:
    servers = [make_server("one.example"), make_server("two.example")]
    patch_tunnel(monkeypatch, servers, good_addresses=set())
    tunnel, holder = make_tunnel(ProxyConfig(enabled=True, mode="managed"), tmp_path)
    assert await tunnel.apply() == "down"
    # graceful degradation: no proxy URL, so the router goes direct
    assert tunnel.proxy_url() is None
    assert "proxy tunnel is down" in tunnel.error_hint()


async def test_health_tick_rotates_then_heals(tmp_path: Path, monkeypatch) -> None:
    servers = [make_server("one.example"), make_server("two.example")]

    async def probe_none(self) -> bool:
        return False

    async def probe_first(self) -> bool:
        return self._servers[self._index].address == "one.example"

    patch_tunnel(monkeypatch, servers, good_addresses={"one.example"})
    tunnel, holder = make_tunnel(
        ProxyConfig(
            enabled=True,
            mode="managed",
            failure_threshold=1,
            health_check_interval_seconds=10,
        ),
        tmp_path,
    )
    assert await tunnel.apply() == "up"
    assert tunnel.status()["server"]["address"] == "one.example"

    # both routes die: the tick probes, fails, rotates through everything, ends down
    monkeypatch.setattr(Tunnel, "_probe", probe_none, raising=True)
    await tunnel.health_tick()
    assert tunnel.status()["state"] == "down"
    assert tunnel.proxy_url() is None
    assert "down" in tunnel.error_hint()

    # routes come back: the next tick re-applies and the tunnel heals
    monkeypatch.setattr(Tunnel, "_probe", probe_first, raising=True)
    await tunnel.health_tick()
    assert tunnel.status()["state"] == "up"
    assert tunnel.proxy_url() is not None


async def test_manual_rotate_cycles_routes(tmp_path: Path, monkeypatch) -> None:
    servers = [make_server("one.example"), make_server("two.example")]
    patch_tunnel(monkeypatch, servers, good_addresses={"one.example", "two.example"})
    tunnel, holder = make_tunnel(ProxyConfig(enabled=True, mode="managed"), tmp_path)
    await tunnel.apply()
    first = tunnel.status()["server"]["address"]
    await tunnel.rotate("manual")
    second = tunnel.status()["server"]["address"]
    assert first != second
    assert tunnel.status()["state"] == "up"
    assert tunnel.status()["health"]["rotations"] >= 1


async def test_subscription_failure_marks_tunnel_down(tmp_path: Path, monkeypatch) -> None:
    async def no_servers(self, cfg, force: bool = False) -> bool:
        self._servers = []
        self._last_error = "subscription fetch failed"
        return False

    async def fake_ensure_binary(self, cfg):
        return Path("fake-xray")

    monkeypatch.setattr(Tunnel, "_ensure_servers", no_servers)
    monkeypatch.setattr(Tunnel, "_ensure_binary", fake_ensure_binary)
    tunnel, holder = make_tunnel(ProxyConfig(enabled=True, mode="managed"), tmp_path)
    assert await tunnel.apply() == "down"
    assert "subscription" in tunnel.status()["last_error"]


async def test_stop_clears_port_and_state(tmp_path: Path, monkeypatch) -> None:
    servers = [make_server("one.example")]
    patch_tunnel(monkeypatch, servers, good_addresses={"one.example"})
    tunnel, holder = make_tunnel(ProxyConfig(enabled=True, mode="managed"), tmp_path)
    await tunnel.apply()
    assert tunnel.proxy_url() is not None
    await tunnel.stop()
    assert tunnel.status()["state"] == "stopped"
    assert tunnel.proxy_url() is None
