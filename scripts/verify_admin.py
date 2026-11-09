"""Admin panel verification: boot the router, drive the admin API and UI over HTTP.

Run: ``uv run python scripts/verify_admin.py``

Backs up any existing config.ini, exercises the panel endpoints against the live
catalog, then restores the original config. Exits non-zero on failure.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("VERIFY_PORT", "8498"))
BASE = f"http://127.0.0.1:{PORT}"
RESULTS: list[tuple[bool, str, str]] = []

# Hermetic config via SURPLUS_CONFIG: the owner's real config.ini is never read
# or written by these checks.
CONFIG_DIR = Path(tempfile.mkdtemp(prefix="surplus-verify-admin-"))
CONFIG = CONFIG_DIR / "config.ini"


def record(ok: bool, name: str, detail: str = "") -> None:
    RESULTS.append((ok, name, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")


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
            body = resp.read().decode(errors="replace")
            ctype = resp.headers.get("content-type", "")
            if "json" in ctype:
                return resp.status, json.loads(body or "null")
            return resp.status, body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        try:
            return exc.code, json.loads(body)
        except ValueError:
            return exc.code, body[:300]
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


def wait_for_server(proc: subprocess.Popen, timeout: float = 90.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            print(f"server exited early with code {proc.returncode}")
            return False
        status, _ = request("GET", "/admin/api/status", timeout=5)
        if status == 200:
            return True
        time.sleep(0.5)
    return False


def main() -> int:
    # Mimic a real deployment: the process starts from config.ini, so the file
    # must name the port the test server actually binds.
    CONFIG.write_text(f"[server]\nhost = 127.0.0.1\nport = {PORT}\n", encoding="utf-8")

    env = {k: v for k, v in os.environ.items() if k != "SURPLUS_API_KEY"}
    env["SURPLUS_CONFIG"] = str(CONFIG)
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

        # ---- UI is served -------------------------------------------------
        status, body = request("GET", "/admin")
        record(
            status == 200 and isinstance(body, str) and "Surplus Router" in body,
            "admin_page_served",
            f"http {status}, {len(body) if isinstance(body, str) else 0} bytes",
        )

        for asset, needle in (("app.js", "FILTERS"), ("style.css", "--accent")):
            status, body = request("GET", f"/admin/assets/{asset}")
            record(
                status == 200 and isinstance(body, str) and needle in body,
                f"asset_{asset}",
                f"http {status}, {len(body) if isinstance(body, str) else 0} bytes",
            )

        status, body = request("GET", "/admin/")
        record(status == 200 and isinstance(body, str), "admin_trailing_slash", f"http {status}")

        # ---- baseline status ---------------------------------------------
        status, st = request("GET", "/admin/api/status")
        baseline = st.get("counts", {}) if status == 200 else {}
        record(
            status == 200 and baseline.get("total", 0) > 0,
            "status_baseline",
            f"total={baseline.get('total')} passing={baseline.get('passing')} "
            f"key_source={st.get('api_key', {}).get('source')}",
        )
        record(
            st.get("restart_required") is False
            and st.get("listening_on", {}).get("port") == PORT,
            "listening_matches_config",
            f"listening_on={st.get('listening_on')}",
        )

        # ---- filters: tighten caps and check the gate follows -------------
        payload = {
            "filter": {
                "enable_price_input": True,
                "max_input_per_1m": 0.001,
                "enable_price_output": True,
                "max_output_per_1m": 0.005,
                "enable_models_deny": True,
                "deny_models": ["minimax-m2.1"],
                "enable_discount": False,
                "enable_models_allow": False,
                "enable_providers_allow": False,
                "enable_context_min": False,
                "allow_unknown_models": False,
            }
        }
        status, res = request("POST", "/admin/api/config", payload)
        counts = res.get("counts", {}) if isinstance(res, dict) else {}
        record(
            status == 200 and res.get("ok") is True,
            "save_filters",
            f"http {status} passing={counts.get('passing')} "
            f"(baseline {baseline.get('passing')})",
        )
        record(
            counts.get("passing", 10**9) < baseline.get("passing", 0),
            "tighter_filters_reduce_passing",
            f"{baseline.get('passing')} -> {counts.get('passing')}",
        )
        record(
            res.get("restart_required") is False,
            "filter_change_needs_no_restart",
            f"listening_on={res.get('listening_on')}",
        )

        status, body = request("GET", "/v1/models")
        served = body.get("data", []) if isinstance(body, dict) else []
        record(
            status == 200 and len(served) == counts.get("passing"),
            "models_list_matches_saved_filters",
            f"served={len(served)} passing={counts.get('passing')}",
        )
        served_ids = {m.get("id") for m in served}
        record(
            "minimax-m2.1" not in served_ids,
            "deny_list_hides_model",
            "minimax-m2.1 absent from /v1/models",
        )

        # denied model rejected at the request gate with the right reason
        status, body = request(
            "POST",
            "/v1/chat/completions",
            {"model": "minimax-m2.1", "messages": [{"role": "user", "content": "hi"}]},
        )
        err = body.get("error", {}) if isinstance(body, dict) else {}
        record(
            status == 403 and err.get("failed_filters") == ["models_deny"],
            "deny_list_blocks_request",
            f"http {status} failed_filters={err.get('failed_filters')}",
        )

        # discount filter maps to the upstream /min{N} prefix
        status, res = request(
            "POST",
            "/admin/api/config",
            {"filter": {"enable_discount": True, "min_discount_pct": 40}},
        )
        status2, st2 = request("GET", "/admin/api/status")
        record(
            res.get("ok") is True and st2.get("minimum_discount_prefix") == "/min40",
            "discount_prefix_applied",
            f"prefix={st2.get('minimum_discount_prefix')}",
        )
        request(
            "POST",
            "/admin/api/config",
            {"filter": {"enable_discount": False, "min_discount_pct": 30}},
        )

        # ---- API key handling --------------------------------------------
        secret = "si_test_dummy_9876543210"
        status, res = request("POST", "/admin/api/config", {"auth": {"api_key": secret}})
        auth = res.get("config", {}).get("auth", {}) if isinstance(res, dict) else {}
        record(
            status == 200 and auth.get("api_key_source") == "file",
            "key_saved_to_file",
            f"source={auth.get('api_key_source')} masked={auth.get('api_key_masked')}",
        )
        record(
            secret not in json.dumps(res),
            "key_never_echoed_in_response",
            "response contains only the masked form",
        )
        record(
            secret in CONFIG.read_text(encoding="utf-8"),
            "key_persisted_to_config_ini",
            "config.ini holds it; the file is gitignored",
        )
        status, st = request("GET", "/admin/api/status")
        record(
            secret not in json.dumps(st) and st.get("api_key", {}).get("masked") != "",
            "status_masks_key",
            f"masked={st.get('api_key', {}).get('masked')}",
        )

        status, res = request("POST", "/admin/api/config", {"auth": {"clear_api_key": True}})
        record(
            res.get("config", {}).get("auth", {}).get("api_key_source") == "missing",
            "key_cleared",
            f"source={res.get('config', {}).get('auth', {}).get('api_key_source')}",
        )

        # an empty key value must not wipe a stored key
        request("POST", "/admin/api/config", {"auth": {"api_key": secret}})
        request("POST", "/admin/api/config", {"filter": {"enable_price_input": True}})
        status, st = request("GET", "/admin/api/status")
        record(
            st.get("api_key", {}).get("source") == "file",
            "filter_save_keeps_stored_key",
            f"source={st.get('api_key', {}).get('source')}",
        )
        request("POST", "/admin/api/config", {"auth": {"clear_api_key": True}})

        # ---- port change requires a restart -------------------------------
        status, res = request("POST", "/admin/api/config", {"server": {"port": PORT + 1}})
        record(
            res.get("restart_required") is True
            and res.get("listening_on", {}).get("port") == PORT,
            "port_change_flags_restart",
            f"restart_required={res.get('restart_required')} "
            f"listening_on={res.get('listening_on')}",
        )
        request("POST", "/admin/api/config", {"server": {"port": PORT}})

        status, res = request("POST", "/admin/api/config", {"server": {"port": "nope"}})
        record(status == 400, "invalid_port_rejected", f"http {status}")

        # ---- editing config.ini on disk applies without a restart ----------
        status, before = request("GET", "/admin/api/status")
        before_passing = before.get("counts", {}).get("passing")
        text = CONFIG.read_text(encoding="utf-8")
        if "max_input_per_1m" in text:
            text = re.sub(r"max_input_per_1m = .*", "max_input_per_1m = 0.0005", text)
        else:
            text += "\n[filter]\nmax_input_per_1m = 0.0005\n"
        CONFIG.write_text(text, encoding="utf-8")
        future = time.time() + 10  # guarantee the mtime visibly moved
        os.utime(CONFIG, (future, future))

        status, after = request("GET", "/admin/api/status")
        after_caps = after.get("config", {}).get("filter", {}).get("max_input_per_1m")
        after_passing = after.get("counts", {}).get("passing")
        record(
            after_caps == 0.0005 and after_passing < before_passing,
            "disk_edit_applies_without_restart",
            f"cap {before.get('config', {}).get('filter', {}).get('max_input_per_1m')} -> "
            f"{after_caps}; passing {before_passing} -> {after_passing}",
        )
        request(
            "POST",
            "/admin/api/config",
            {"filter": {"max_input_per_1m": 0.1, "max_output_per_1m": 0.5}},
        )

        # ---- browser auto-open toggle -------------------------------------
        status, res = request("POST", "/admin/api/config", {"server": {"open_browser": False}})
        record(
            status == 200 and res.get("config", {}).get("server", {}).get("open_browser") is False,
            "browser_autoopen_can_be_disabled",
            f"open_browser={res.get('config', {}).get('server', {}).get('open_browser')}",
        )
        request("POST", "/admin/api/config", {"server": {"open_browser": True}})

        # ---- catalog table + search + sort --------------------------------
        query = urllib.parse.urlencode({"q": "gpt", "limit": 5})
        status, body = request("GET", f"/admin/api/models?{query}")
        record(
            status == 200 and body.get("count", 0) > 0 and len(body.get("models", [])) <= 5,
            "models_search",
            f"q=gpt count={body.get('count')} returned={body.get('returned')}",
        )

        status, body = request("GET", "/admin/api/models?sort=input&limit=600")
        rows = body.get("models", []) if isinstance(body, dict) else []
        priced = [
            r["market_input_per_1m"] for r in rows if r.get("market_input_per_1m") is not None
        ]
        unpriced_tail = all(
            r.get("market_input_per_1m") is None for r in rows[len(priced):]
        )
        record(
            bool(rows) and priced == sorted(priced) and unpriced_tail,
            "models_sorted_by_market_input",
            f"{len(rows)} rows, {len(priced)} priced, unpriced pushed last={unpriced_tail}",
        )

        status, body = request("GET", "/admin/api/models?passed_only=true")
        rows = body.get("models", []) if isinstance(body, dict) else []
        all_pass = bool(rows) and all(r["passed"] for r in rows)
        record(
            status == 200 and all_pass and body.get("count", 0) > 0,
            "models_passed_only",
            f"count={body.get('count')}",
        )

        # a blocked row carries its reasons for the UI
        status, body = request("GET", "/admin/api/models?passed_only=false&limit=400")
        blocked = [r for r in body.get("models", []) if not r["passed"]]
        record(
            bool(blocked) and all(r["failed_filters"] for r in blocked),
            "blocked_rows_have_reasons",
            (
                f"{len(blocked)} blocked rows, "
                f"e.g. {blocked[0]['id']} -> {blocked[0]['failed_filters']}"
                if blocked
                else "none"
            ),
        )

        # ---- market source + quality fields --------------------------------
        status, st = request("GET", "/admin/api/status")
        cat = st.get("catalog", {}) if isinstance(st, dict) else {}
        record(
            cat.get("market_source") == "/api/markets" and cat.get("market_offer_count", 0) > 0,
            "market_source_is_api_markets",
            f"source={cat.get('market_source')} offers={cat.get('market_offer_count')}",
        )

        status, body = request("GET", "/admin/api/models?limit=600")
        rows = body.get("models", []) if isinstance(body, dict) else []
        with_uptime = [r for r in rows if r.get("uptime_pct") is not None]
        with_ttft = [r for r in rows if r.get("ttft_p50_ms") is not None]
        with_trend = [r for r in rows if r.get("trend_direction")]
        record(
            bool(with_uptime) and bool(with_ttft) and bool(with_trend),
            "quality_fields_populated",
            f"uptime={len(with_uptime)} ttft={len(with_ttft)} trend={len(with_trend)} "
            f"of {len(rows)} rows",
        )

        # new sort keys honour dir=desc, unmeasured rows stay last
        status, body = request("GET", "/admin/api/models?sort=uptime&dir=desc&limit=600")
        rows = body.get("models", []) if isinstance(body, dict) else []
        ups = [r["uptime_pct"] for r in rows if r.get("uptime_pct") is not None]
        record(
            bool(ups) and ups == sorted(ups, reverse=True),
            "sort_uptime_desc",
            f"{len(ups)} measured rows in descending order",
        )

        # price_class filter narrows to one class
        status, body = request("GET", "/admin/api/models?price_class=token&limit=600")
        rows = body.get("models", []) if isinstance(body, dict) else []
        record(
            bool(rows) and all(r["price_class"] == "token" for r in rows),
            "price_class_filter",
            f"{len(rows)} token-priced rows only",
        )

        # ---- per-model detail ------------------------------------------------
        status, body = request("GET", "/admin/api/models?sort=requests&dir=desc&limit=1")
        rows = body.get("models", []) if isinstance(body, dict) else []
        if rows:
            mid = rows[0]["id"]
            status, detail = request("GET", f"/admin/api/models/{urllib.parse.quote(mid)}")
            record(
                status == 200
                and isinstance(detail.get("providers"), list)
                and "features" in detail
                and "trend_direction" in detail,
                "model_detail_endpoint",
                f"{mid}: providers={len(detail.get('providers', []))} "
                f"trusted_out={detail.get('trusted_output_per_1m')}",
            )
        else:
            record(False, "model_detail_endpoint", "no rows to probe")

        status, _ = request("GET", "/admin/api/models/does-not-exist")
        record(status == 404, "model_detail_unknown_404", f"http {status}")

        # ---- account endpoints degrade cleanly without a key -----------------
        status, body = request("GET", "/admin/api/account")
        record(
            status == 200 and body.get("available") is False and body.get("reason") == "no_api_key",
            "account_without_key",
            f"available={body.get('available')} reason={body.get('reason')}",
        )
        status, body = request("GET", "/admin/api/account/requests?limit=5")
        record(
            status == 200 and body.get("available") is False and body.get("reason") == "no_api_key",
            "account_requests_without_key",
            f"available={body.get('available')} reason={body.get('reason')}",
        )

        # ---- new filter knobs round-trip through config ----------------------
        status, res = request(
            "POST",
            "/admin/api/config",
            {
                "filter": {
                    "enable_min_sellers": True,
                    "min_healthy_sellers": 3,
                    "enable_min_uncapped": True,
                    "min_uncapped_offers": 1,
                    "enable_require_features": True,
                    "require_features": "streaming, tools",
                }
            },
        )
        f = res.get("config", {}).get("filter", {}) if isinstance(res, dict) else {}
        new_counts = res.get("counts", {})
        record(
            status == 200
            and f.get("enable_min_sellers") is True
            and f.get("min_healthy_sellers") == 3
            and f.get("require_features") == ["streaming", "tools"],
            "quality_filters_round_trip",
            f"passing={new_counts.get('passing')} (before {baseline.get('passing')})",
        )
        record(
            new_counts.get("passing", 10**9) < baseline.get("passing", 0),
            "quality_filters_reduce_passing",
            f"{baseline.get('passing')} -> {new_counts.get('passing')}",
        )

        # the gate enforces the new rules on a request
        status, body = request(
            "GET", "/admin/api/models?passed_only=false&limit=600"
        )
        rows = body.get("models", []) if isinstance(body, dict) else []
        new_reasons = {r for row in rows for r in row.get("failed_filters", [])} & {
            "min_sellers",
            "min_uncapped",
            "require_features",
        }
        record(
            bool(new_reasons),
            "new_filter_reasons_visible",
            f"reasons seen: {sorted(new_reasons)}",
        )

        request(
            "POST",
            "/admin/api/config",
            {
                "filter": {
                    "enable_min_sellers": False,
                    "enable_min_uncapped": False,
                    "enable_require_features": False,
                }
            },
        )

        # ---- manual refresh ------------------------------------------------
        status, res = request("POST", "/admin/api/refresh")
        record(
            status == 200 and res.get("ok") is True,
            "manual_refresh",
            f"http {status} models={res.get('counts', {}).get('total')} "
            f"stale={res.get('catalog', {}).get('stale')}",
        )

        return report()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(CONFIG_DIR, ignore_errors=True)
        print("temporary test config discarded")


def report() -> int:
    passed = sum(1 for ok, _, _ in RESULTS if ok)
    failed = [name for ok, name, _ in RESULTS if not ok]
    suffix = f"; failed: {', '.join(failed)}" if failed else ""
    print(f"\n{passed}/{len(RESULTS)} checks passed{suffix}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
