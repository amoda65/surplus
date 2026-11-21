"""Live verification for the proxy tunnel: boot the router, bring the managed
Xray tunnel up from a real subscription, and exercise the whole path.

Run (never commits the subscription anywhere — it comes from the environment):

    set VERIFY_SUBSCRIPTION_URL=https://panel.example/sub/token
    uv run python scripts/verify_proxy.py

Optional:
    set VERIFY_XRAY_BINARY=C:/path/to/xray.exe   # skip the auto-download

Checks: tunnel up with a live route, model list served through the tunnel,
proxy test endpoint, manual route rotation, disable -> direct fallback,
re-enable -> tunnel returns. Exits non-zero on failure.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("VERIFY_PORT", "8497"))
BASE = f"http://127.0.0.1:{PORT}"
SUBSCRIPTION_URL = os.environ.get("VERIFY_SUBSCRIPTION_URL", "").strip()
XRAY_BINARY = os.environ.get("VERIFY_XRAY_BINARY", "").strip()
RESULTS: list[tuple[bool, str, str]] = []

# Hermetic: the router runs against a throwaway config dir (and a throwaway
# xray workdir, because APPDATA points here too), so nothing of the owner's
# real setup is touched. The only real side effect is the network traffic.
CONFIG_DIR = Path(tempfile.mkdtemp(prefix="surplus-proxy-verify-"))
CONFIG = CONFIG_DIR / "config.ini"


def write_test_config() -> None:
    lines = [
        "[server]",
        "host = 127.0.0.1",
        f"port = {PORT}",
        "open_browser = false",
        "[filter]",
        "enable_price_input = true",
        "max_input_per_1m = 0.1",
        "enable_price_output = true",
        "max_output_per_1m = 0.5",
        "[proxy]",
        "enabled = true",
        "mode = managed",
        f"subscription_url = {SUBSCRIPTION_URL}",
    ]
    if XRAY_BINARY:
        lines.append(f"xray_binary = {XRAY_BINARY}")
    CONFIG.write_text("\n".join(lines) + "\n", encoding="utf-8")


def record(ok: bool, name: str, detail: str = "") -> None:
    RESULTS.append((ok, name, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")


def _dig(body, *keys):
    """Safe nested lookup in a JSON response."""
    current = body
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def request(method: str, path: str, payload: dict | None = None, timeout: float = 60.0):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode() or "null")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        try:
            return exc.code, json.loads(body)
        except ValueError:
            return exc.code, body[:300]
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


def wait_for_server(proc: subprocess.Popen, timeout: float = 240.0) -> bool:
    """Long timeout: the first run downloads Xray-core before serving."""
    print("waiting for the router (tunnel starts before the first request) ...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            print(f"server exited early with code {proc.returncode}")
            return False
        status, _ = request("GET", "/", timeout=5)
        if status == 200:
            return True
        time.sleep(1.0)
    return False


def wait_for_tunnel(state: str = "up", timeout: float = 120.0) -> dict | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, body = request("GET", "/admin/api/proxy", timeout=10)
        if status == 200 and isinstance(body, dict):
            if body.get("state") == state:
                return body
            if body.get("state") == "down":
                return body  # fail fast: no point waiting for a dead ladder
        time.sleep(2.0)
    return body if isinstance(body, dict) else None


def main() -> int:
    if not SUBSCRIPTION_URL:
        print("error: set VERIFY_SUBSCRIPTION_URL to a real subscription link first")
        return 2

    write_test_config()
    env = {k: v for k, v in os.environ.items() if k != "SURPLUS_API_KEY"}
    env["SURPLUS_CONFIG"] = str(CONFIG)
    env["APPDATA"] = str(CONFIG_DIR)  # xray workdir inside the throwaway dir
    cmd = [
        sys.executable,
        "-c",
        "import uvicorn; from surplus.main import app;"
        f" uvicorn.run(app, host='127.0.0.1', port={PORT}, log_level='warning', access_log=False)",
    ]
    print(f"starting router on {BASE} (config: {CONFIG}) ...")
    proc = subprocess.Popen(cmd, cwd=str(ROOT), env=env)
    try:
        if not wait_for_server(proc):
            record(False, "server_startup", "did not become ready")
            return report()

        record(True, "server_startup", f"pid {proc.pid}")

        # 1. tunnel reaches 'up' (subscription -> xray -> probe through Surplus)
        proxy = wait_for_tunnel("up")
        server = (proxy or {}).get("server") or {}
        record(
            bool(proxy and proxy.get("state") == "up"),
            "tunnel_up",
            f"state={proxy and proxy.get('state')} route={proxy and proxy.get('route')} "
            f"server={server.get('address')}:{server.get('port')} "
            f"xray={proxy and (proxy.get('xray', {}).get('version') or 'running')}",
        )
        if not proxy or proxy.get("state") != "up":
            print(f"tunnel status: {json.dumps(proxy, indent=2)[:2000]}")
            return report()

        # 2. model list is served from a catalog fetched through the tunnel
        status, body = request("GET", "/v1/models")
        data = body.get("data", []) if isinstance(body, dict) else []
        record(
            status == 200 and isinstance(data, list) and len(data) > 0,
            "models_list_through_tunnel",
            f"http {status}, {len(data)} models",
        )

        # 3. the dedicated proxy test endpoint agrees the tunnel works
        status, body = request("POST", "/admin/api/proxy/test", {})
        record(
            status == 200 and body.get("ok") is True,
            "proxy_test_endpoint",
            f"http {status} latency={body.get('latency_ms')}ms",
        )

        # 4. manual rotation lands on another (or the same, if only one) route
        before = proxy.get("route")
        status, body = request("POST", "/admin/api/proxy/rotate")
        after_state = _dig(body, "proxy", "state")
        after = _dig(body, "proxy", "route")
        record(
            status == 200 and after_state == "up",
            "manual_rotate",
            f"http {status} route {before} -> {after} state={after_state}",
        )

        # 5. subscription refresh endpoint
        status, body = request("POST", "/admin/api/proxy/refresh-subscription")
        sub = body.get("proxy", {}).get("subscription", {}) if isinstance(body, dict) else {}
        record(
            status == 200 and sub.get("servers", 0) > 0,
            "refresh_subscription",
            f"http {status} routes={sub.get('servers')} error={sub.get('error') or 'none'}",
        )

        # 6. disable -> direct connection, xray stopped
        status, body = request("POST", "/admin/api/proxy/toggle", {"enabled": False})
        disabled = body.get("proxy", {}) if isinstance(body, dict) else {}
        record(
            status == 200
            and disabled.get("state") == "disabled"
            and disabled.get("proxy_url") is None
            and disabled.get("xray", {}).get("running") is False,
            "toggle_off_direct_fallback",
            f"http {status} state={disabled.get('state')} "
            f"xray_running={disabled.get('xray', {}).get('running')}",
        )

        # 7. re-enable -> tunnel returns without a restart
        status, body = request("POST", "/admin/api/proxy/toggle", {"enabled": True})
        re_enabled = body.get("proxy", {}) if isinstance(body, dict) else {}
        record(
            status == 200 and re_enabled.get("state") == "up",
            "toggle_on_no_restart",
            f"http {status} state={re_enabled.get('state')} route={re_enabled.get('route')}",
        )

        return report()
    finally:
        # Graceful first (stops xray through the lifespan), then hard cleanup.
        try:
            request("POST", "/admin/api/proxy/toggle", {"enabled": False}, timeout=10)
        except Exception:  # noqa: BLE001
            pass
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        _kill_leftover_xray()
        shutil.rmtree(CONFIG_DIR, ignore_errors=True)


def _kill_leftover_xray() -> None:
    """Safety net: kill an xray orphan from the throwaway workdir, if any."""
    pid_file = CONFIG_DIR / "surplus-router" / "xray" / "xray.pid"
    try:
        pid = int(pid_file.read_text().strip())
    except (OSError, ValueError):
        return
    out = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
    ).stdout
    if "xray" in out.lower():
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)


def report() -> int:
    passed = sum(1 for ok, _, _ in RESULTS if ok)
    failed = [name for ok, name, _ in RESULTS if not ok]
    suffix = f"; failed: {', '.join(failed)}" if failed else ""
    print(f"\n{passed}/{len(RESULTS)} checks passed{suffix}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
