"""Outbound tunnel: external proxy URL or a managed Xray-core process.

The router's httpx client is the only component that talks to the upstream
marketplace, so this module owns the single place where a VPN/proxy is needed:

- ``external``: point at a local proxy port exposed by a VPN client running in
  proxy-only mode (no TUN, no system proxy). The rest of the machine, including
  a corporate VPN, stays untouched.
- ``managed``: run Xray-core ourselves from a subscription link, health-check
  the tunnel through Surplus, and rotate to the next subscribed route when the
  active one stops working.

Failure policy: the tunnel is best-effort. When it is down the router falls
back to a direct connection (which may work on networks where Surplus is
reachable) and surfaces the reason in the admin API and in /v1 errors.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import os
import platform
import signal
import socket
import subprocess
import time
import zipfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import httpx

from .config import ProxyConfig, user_config_dir
from .subscription import Server, build_client_config, parse_subscription

log = logging.getLogger("surplus.tunnel")

XRAY_RELEASE_URL = "https://github.com/XTLS/Xray-core/releases/latest/download/{asset}"
SUBSCRIPTION_USER_AGENT = "v2rayN/6.45"
PROBE_PATH = "/v1/models"


class Tunnel:
    """Owns the effective outbound proxy and (in managed mode) the Xray process."""

    def __init__(
        self,
        config_provider: Callable[[], ProxyConfig],
        upstream_base: str,
        workdir: Path | None = None,
    ) -> None:
        self._config = config_provider
        self.upstream_base = upstream_base.rstrip("/")
        self.workdir = Path(workdir) if workdir else user_config_dir() / "xray"
        self._proc: asyncio.subprocess.Process | None = None
        self._log_handle = None
        self._servers: list[Server] = []
        self._skipped: list[str] = []
        self._index = 0
        self._port: int | None = None
        self._state = "stopped"
        self._last_error = ""
        self._last_probe_at: float | None = None
        self._last_success_at: float | None = None
        self._probe_ms: int | None = None
        self._failures = 0
        self._rotations = 0
        self._xray_version = ""
        self._binary: Path | None = None
        self._subscription_fetched_at: float | None = None
        self._last_fetch_attempt: float | None = None
        self._subscription_error = ""
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------- interface

    @property
    def cfg(self) -> ProxyConfig:
        return self._config()

    @property
    def state(self) -> str:
        return self._state

    def proxy_url(self) -> str | None:
        """The proxy every upstream request must use, or None for direct."""
        cfg = self.cfg
        if not cfg.enabled:
            return None
        if cfg.mode == "external":
            return cfg.proxy_url or None
        if self._port is not None and self._state in {"starting", "up", "rotating"}:
            return f"http://127.0.0.1:{self._port}"
        return None

    async def apply(self) -> str:
        """Reconcile the running tunnel with the current config."""
        async with self._lock:
            return await self._apply_locked()

    async def stop(self) -> None:
        async with self._lock:
            await self._stop_xray()
            self._port = None
            self._state = "stopped"

    async def health_tick(self) -> None:
        """Periodic supervision: retry, probe, rotate, refresh the subscription."""
        async with self._lock:
            cfg = self.cfg
            if not cfg.enabled:
                if self._state not in {"disabled", "stopped"}:
                    await self._stop_xray()
                    self._port = None
                self._state = "disabled"
                return
            if cfg.mode == "external":
                self._state = "up" if await self._probe() else "down"
                return
            if self._state == "down" or self._proc is None:
                await self._apply_locked()
                return
            if self._proc.returncode is not None:
                await self._apply_locked()
                return
            if await self._refresh_due(cfg):
                await self._ensure_servers(cfg, force=True)
            if await self._probe():
                self._failures = 0
                self._state = "up"
                return
            self._failures += 1
            if self._failures >= cfg.failure_threshold:
                await self._rotate_locked("health check failed")

    async def rotate(self, reason: str = "manual") -> str:
        async with self._lock:
            return await self._rotate_locked(reason)

    async def refresh_subscription(self) -> str:
        async with self._lock:
            cfg = self.cfg
            if not cfg.enabled or cfg.mode != "managed":
                return self._state
            await self._ensure_servers(cfg, force=True)
            if not self._servers:
                self._state = "down"
                return self._state
            self._state = "starting"
            binary = await self._ensure_binary(cfg)
            if binary is None:
                self._state = "down"
                return self._state
            await self._stop_xray()
            if self._port is None:
                self._port = _pick_port()
            await self._run_ladder(binary)
            return self._state

    def error_hint(self) -> str:
        """Human-readable reason to attach to upstream failures ('' when fine)."""
        cfg = self.cfg
        if not cfg.enabled or self._state == "up":
            return ""
        detail = self._last_error or "not connected"
        if cfg.mode == "managed":
            position = f"{self._index + 1}/{len(self._servers)}" if self._servers else "-"
            return (
                f"proxy tunnel is {self._state} (route {position}; last error: {detail}). "
                "Open /admin to inspect the tunnel or rotate the route."
            )
        return f"external proxy {cfg.proxy_url or '(none)'} is unreachable (last error: {detail})."

    def status(self) -> dict:
        cfg = self.cfg
        server = (
            self._servers[self._index]
            if self._servers and self._index < len(self._servers)
            else None
        )
        return {
            "enabled": cfg.enabled,
            "mode": cfg.mode,
            "state": self._state,
            "proxy_url": self.proxy_url(),
            "server": server.summary() if server else None,
            "route": f"{self._index + 1}/{len(self._servers)}" if self._servers else None,
            "xray": {
                "running": bool(self._proc is not None and self._proc.returncode is None),
                "version": self._xray_version,
                "binary": str(self._binary) if self._binary else "",
                "workdir": str(self.workdir),
            },
            "health": {
                "last_probe_at": _iso(self._last_probe_at),
                "last_success_at": _iso(self._last_success_at),
                "latency_ms": self._probe_ms,
                "failures": self._failures,
                "rotations": self._rotations,
            },
            "subscription": {
                "configured": bool(cfg.subscription_url or cfg.subscription_links.strip()),
                "fetched_at": _iso(self._subscription_fetched_at),
                "servers": len(self._servers),
                "skipped": self._skipped[:8],
                "error": self._subscription_error,
            },
            "last_error": self._last_error,
        }

    # ------------------------------------------------------------- internals

    async def _apply_locked(self) -> str:
        cfg = self.cfg
        if not cfg.enabled:
            await self._stop_xray()
            self._port = None
            self._state = "disabled"
            self._last_error = ""
            self._failures = 0
            return self._state
        if cfg.mode == "external":
            await self._stop_xray()
            self._port = None
            if cfg.proxy_url:
                self._state = "up"
                self._last_error = ""
            else:
                self._state = "down"
                self._last_error = "proxy_url is empty for external mode"
            return self._state
        self._state = "starting"
        if not await self._ensure_servers(cfg):
            await self._stop_xray()
            self._state = "down"
            return self._state
        binary = await self._ensure_binary(cfg)
        if binary is None:
            await self._stop_xray()
            self._state = "down"
            return self._state
        if self._port is None:
            self._port = _pick_port()
        await self._stop_xray()
        await self._run_ladder(binary)
        return self._state

    async def _rotate_locked(self, reason: str) -> str:
        if not self._servers:
            return await self._apply_locked()
        binary = await self._ensure_binary(self.cfg)
        if binary is None:
            self._state = "down"
            return self._state
        log.warning("rotating proxy route (%s)", reason)
        self._failures = 0
        self._state = "rotating"
        if self._port is None:
            self._port = _pick_port()
        self._index = (self._index + 1) % len(self._servers)
        self._rotations += 1
        await self._stop_xray()
        # Rotate through the remaining routes until one answers.
        await self._run_ladder(binary)
        return self._state

    async def _run_ladder(self, binary: Path) -> bool:
        """Start routes in subscription order until a probe succeeds."""
        if self._port is None:
            self._port = _pick_port()
        attempts = max(1, len(self._servers))
        for attempt in range(attempts):
            server = self._servers[self._index]
            try:
                await self._start_server(binary, server, self._port)
                if await self._probe():
                    self._state = "up"
                    self._last_error = ""
                    self._failures = 0
                    return True
                self._last_error = f"probe failed via {server.address}:{server.port}"
            except Exception as exc:  # noqa: BLE001 - surface, then try the next route
                self._last_error = f"{server.address}: {type(exc).__name__}: {exc}"
            await self._stop_xray()
            if attempt < attempts - 1 and len(self._servers) > 1:
                self._index = (self._index + 1) % len(self._servers)
                self._rotations += 1
        self._state = "down"
        return False

    # ---------------------------------------------------------- subscription

    async def _refresh_due(self, cfg: ProxyConfig) -> bool:
        if self._subscription_fetched_at is None:
            return True
        return (
            time.time() - self._subscription_fetched_at
            >= cfg.subscription_refresh_hours * 3600
        )

    async def _ensure_servers(self, cfg: ProxyConfig, force: bool = False) -> bool:
        due = force or not self._servers or await self._refresh_due(cfg)
        if not due:
            return True
        if cfg.subscription_url.strip():
            await self._fetch_subscription(cfg)
        if not self._servers and cfg.subscription_links.strip():
            self._load_links(cfg.subscription_links)
        if not self._servers and not self._last_error:
            self._last_error = (
                "no subscription configured: set a subscription URL or paste raw links"
            )
        return bool(self._servers)

    async def _fetch_subscription(self, cfg: ProxyConfig) -> None:
        now = time.time()
        if (
            self._last_fetch_attempt is not None
            and now - self._last_fetch_attempt < 30
            and self._servers
        ):
            return  # back off: don't hammer a panel that just failed
        self._last_fetch_attempt = now
        self._subscription_error = ""
        timeout = httpx.Timeout(connect=15.0, read=25.0, write=15.0, pool=15.0)
        try:
            async with httpx.AsyncClient(
                timeout=timeout, follow_redirects=True, trust_env=False
            ) as client:
                response = await client.get(
                    cfg.subscription_url.strip(),
                    headers={"User-Agent": SUBSCRIPTION_USER_AGENT},
                )
            response.raise_for_status()
            servers, skipped = parse_subscription(response.text)
        except Exception as exc:  # noqa: BLE001 - keep the old routes on failure
            self._subscription_error = (
                f"subscription fetch failed: {type(exc).__name__}: {exc}"
            )
            self._last_error = self._subscription_error
            log.warning("%s", self._subscription_error)
            return
        self._skipped = skipped
        if not servers:
            self._last_error = "subscription contained no usable links"
            return
        previous = self._servers[self._index].address if self._servers else None
        self._servers = servers
        self._subscription_fetched_at = time.time()
        log.info(
            "subscription loaded: %d route(s), %d skipped", len(servers), len(skipped)
        )
        self._index = 0
        if previous:
            for position, server in enumerate(servers):
                if server.address == previous:
                    self._index = position
                    break

    def _load_links(self, text: str) -> bool:
        servers, skipped = parse_subscription(text)
        self._skipped = skipped
        if not servers:
            self._last_error = "no usable links in subscription_links"
            return False
        self._servers = servers
        self._subscription_fetched_at = time.time()
        self._index = 0
        return True

    # -------------------------------------------------------------- binaries

    async def _ensure_binary(self, cfg: ProxyConfig) -> Path | None:
        if cfg.xray_binary:
            path = Path(cfg.xray_binary).expanduser()
            if path.exists():
                self._binary = path
                return path
            self._last_error = f"xray_binary not found: {path}"
            return None
        if self._binary is not None and self._binary.exists():
            return self._binary
        self.workdir.mkdir(parents=True, exist_ok=True)
        candidate = self.workdir / ("xray.exe" if os.name == "nt" else "xray")
        if not candidate.exists():
            try:
                await self._download_binary(candidate)
            except Exception as exc:  # noqa: BLE001 - reported through status
                self._last_error = (
                    f"xray download failed: {type(exc).__name__}: {exc}. "
                    "Install Xray-core manually and set [proxy] xray_binary."
                )
                return None
        self._binary = candidate
        await self._read_version(candidate)
        return candidate

    async def _download_binary(self, target: Path) -> None:
        asset = _asset_name()
        url = XRAY_RELEASE_URL.format(asset=asset)
        log.info("downloading Xray-core: %s", url)
        timeout = httpx.Timeout(connect=20.0, read=180.0, write=30.0, pool=20.0)
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, trust_env=False
        ) as client:
            response = await client.get(url)
        response.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            member = next(
                (
                    name
                    for name in archive.namelist()
                    if Path(name).name in {"xray", "xray.exe"}
                ),
                None,
            )
            if member is None:
                raise ValueError(f"{asset} does not contain an xray binary")
            data = archive.read(member)
        target.write_bytes(data)
        if os.name != "nt":
            target.chmod(0o755)

    async def _read_version(self, binary: Path) -> None:
        try:
            proc = await asyncio.create_subprocess_exec(
                str(binary),
                "version",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
            lines = out.decode(errors="replace").strip().splitlines()
            self._xray_version = lines[0].strip() if lines else ""
        except Exception:  # noqa: BLE001 - version is cosmetic
            self._xray_version = ""

    # -------------------------------------------------------------- process

    async def _start_server(self, binary: Path, server: Server, port: int) -> None:
        self.workdir.mkdir(parents=True, exist_ok=True)
        config_path = self.workdir / "config.json"
        config_path.write_text(
            json.dumps(build_client_config(server, port), indent=2), encoding="utf-8"
        )
        await self._kill_stale()
        log_path = self.workdir / "xray.log"
        self._log_handle = open(log_path, "ab")  # noqa: SIM115 - closed in _stop_xray
        self._proc = await asyncio.create_subprocess_exec(
            str(binary),
            "run",
            "-c",
            str(config_path),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=self._log_handle,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(self.workdir),
        )
        (self.workdir / "xray.pid").write_text(str(self._proc.pid), encoding="utf-8")
        if not await self._wait_for_port(port, timeout=15.0):
            tail = self._log_tail()
            raise RuntimeError(f"xray did not listen on 127.0.0.1:{port}{tail}")

    async def _stop_xray(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except (TimeoutError, ProcessLookupError):
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()
        if self._log_handle is not None:
            with contextlib.suppress(Exception):
                self._log_handle.close()
            self._log_handle = None

    async def _kill_stale(self) -> None:
        """Kill an xray left behind by a previous run, using our own pid file."""
        pid_file = self.workdir / "xray.pid"
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return
        if pid <= 0:
            return
        try:
            if await asyncio.to_thread(_pid_is_xray, pid):
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGTERM)
                log.info("stopped stale xray process %d", pid)
        finally:
            with contextlib.suppress(OSError):
                pid_file.unlink()

    async def _wait_for_port(self, port: int, timeout: float = 15.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._proc is not None and self._proc.returncode is not None:
                return False
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.4):
                    return True
            except OSError:
                await asyncio.sleep(0.15)
        return False

    def _log_tail(self, lines: int = 6) -> str:
        try:
            text = (self.workdir / "xray.log").read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            return ""
        tail = [line.strip() for line in text.splitlines()[-lines:] if line.strip()]
        return f" ({'; '.join(tail)})" if tail else ""

    # ----------------------------------------------------------------- probe

    async def _probe(self) -> bool:
        cfg = self.cfg
        if cfg.mode == "managed" and self._port is not None:
            proxy = f"http://127.0.0.1:{self._port}"
        else:
            proxy = cfg.proxy_url
        if not proxy:
            self._last_error = "no proxy endpoint to probe"
            return False
        timeout = httpx.Timeout(connect=10.0, read=15.0, write=10.0, pool=10.0)
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(
                proxy=proxy, timeout=timeout, trust_env=False
            ) as client:
                await client.get(f"{self.upstream_base}{PROBE_PATH}")
            ok = True  # any HTTP answer proves the tunnel carried TLS upstream
        except Exception as exc:  # noqa: BLE001 - any failure means "not usable"
            ok = False
            self._last_error = f"probe failed: {type(exc).__name__}: {exc}"
        self._last_probe_at = time.time()
        if ok:
            self._last_success_at = self._last_probe_at
            self._probe_ms = int((time.perf_counter() - started) * 1000)
        return ok


# ---------------------------------------------------------------- helpers


def _iso(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp).astimezone().isoformat(timespec="seconds")


def _pick_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _asset_name() -> str:
    """Xray-core release asset for this platform/architecture."""
    system = platform.system().lower()
    machine = platform.machine().lower()
    arm = machine in {"arm64", "aarch64"}
    if system == "windows":
        return "Xray-windows-64.zip"
    if system == "darwin":
        return "Xray-macos-arm64-v8a.zip" if arm else "Xray-macos-64.zip"
    if arm:
        return "Xray-linux-arm64-v8a.zip"
    if machine in {"x86", "i386", "i686"}:
        return "Xray-linux-32.zip"
    return "Xray-linux-64.zip"


def _pid_is_xray(pid: int) -> bool:
    """Best-effort check that a pid belongs to xray before killing it."""
    try:
        if os.name == "nt":
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return "xray" in result.stdout.lower()
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "comm="],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return "xray" in result.stdout.lower()
    except (OSError, subprocess.SubprocessError):
        return False