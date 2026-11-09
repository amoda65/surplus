"""Local verification: boot the router, exercise the gate against the live catalog.

Run: ``uv run python scripts/verify_local.py``

Starts the real server on a test port, hits the live Surplus catalog, and checks:
admin status, filtered model list, per-model retrieval, capped-out rejection,
unknown-model rejection, and the missing-API-key gate. Exits non-zero on failure.
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
PORT = int(os.environ.get("VERIFY_PORT", "8499"))
BASE = f"http://127.0.0.1:{PORT}"
RESULTS: list[tuple[bool, str, str]] = []

# Hermetic config: the router is pointed at a throwaway file via SURPLUS_CONFIG so
# the checks never read or write the owner's real config.ini.
CONFIG_DIR = Path(tempfile.mkdtemp(prefix="surplus-verify-"))
CONFIG = CONFIG_DIR / "config.ini"


def write_test_config() -> None:
    CONFIG.write_text(
        "[server]\n"
        "host = 127.0.0.1\n"
        f"port = {PORT}\n"
        "open_browser = false\n"
        "[filter]\n"
        "enable_price_input = true\n"
        "max_input_per_1m = 0.1\n"
        "enable_price_output = true\n"
        "max_output_per_1m = 0.5\n",
        encoding="utf-8",
    )


def record(ok: bool, name: str, detail: str = "") -> None:
    RESULTS.append((ok, name, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")


def request(method: str, path: str, payload: dict | None = None, timeout: float = 30.0):
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


def wait_for_server(proc: subprocess.Popen, timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            print(f"server exited early with code {proc.returncode}")
            return False
        status, _ = request("GET", "/", timeout=5)
        if status == 200:
            return True
        time.sleep(0.5)
    return False


def main() -> int:
    write_test_config()
    # Strip the owner's key so the missing-credential path is deterministic and
    # no check can ever place a real (billed) upstream request.
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

        # 1. admin status
        status, body = request("GET", "/admin/api/status")
        ok = status == 200 and isinstance(body, dict) and "counts" in body
        counts = body.get("counts", {}) if ok else {}
        record(
            ok,
            "admin_status",
            f"total={counts.get('total')} passing={counts.get('passing')} "
            f"filtered={counts.get('filtered_out')} token_priced={counts.get('class_token')} "
            f"no_offer={counts.get('class_no_offer')} "
            f"stale={body.get('catalog', {}).get('stale') if ok else '?'}",
        )
        if not ok:
            return report()

        cfg = body.get("config", {})
        cap_in = cfg.get("filter", {}).get("max_input_per_1m")
        cap_out = cfg.get("filter", {}).get("max_output_per_1m")
        record(
            body.get("api_key", {}).get("masked", "") != ""
            or body.get("api_key", {}).get("source") == "missing",
            "api_key_masked_in_status",
            f"source={body.get('api_key', {}).get('source')} "
            f"masked={body.get('api_key', {}).get('masked')!r}",
        )

        # 2. filtered model list
        status, body = request("GET", "/v1/models")
        ok = status == 200 and isinstance(body, dict) and isinstance(body.get("data"), list)
        data = body.get("data", []) if ok else []
        record(ok, "models_list_filtered", f"{len(data)} models returned")
        if not ok:
            return report()

        # every served model must respect the caps on its ACTUAL market price
        status, admin_body = request("GET", "/admin/api/models?limit=1000")
        rows = admin_body.get("models", []) if isinstance(admin_body, dict) else []
        by_id = {r["id"]: r for r in rows}
        violations = []
        for entry in data:
            row = by_id.get(entry.get("id"))
            if row is None:
                violations.append((entry.get("id"), "absent from admin rows"))
                continue
            if row.get("price_class") != "token":
                violations.append((entry.get("id"), f"class={row.get('price_class')}"))
                continue
            market_in = row.get("market_input_per_1m")
            market_out = row.get("market_output_per_1m")
            if (
                market_in is None
                or market_out is None
                or market_in > cap_in + 1e-9
                or market_out > cap_out + 1e-9
            ):
                violations.append((entry.get("id"), market_in, market_out))
        record(
            not violations,
            "served_models_respect_market_price_caps",
            f"caps input<={cap_in} output<={cap_out} on market prices; "
            f"violations={len(violations)}" + (f" e.g. {violations[:3]}" if violations else ""),
        )

        passing_id = data[0]["id"] if data else None
        record(bool(passing_id), "have_passing_model", str(passing_id))

        # 3. locate models rejected by each rule family (for the rejection tests)
        failing = next((r for r in rows if "price_input" in (r.get("failed_filters") or [])), None)
        no_offer = next(
            (r for r in rows if "no_market_offer" in (r.get("failed_filters") or [])), None
        )
        not_token = next(
            (r for r in rows if "not_token_priced" in (r.get("failed_filters") or [])), None
        )
        record(
            bool(failing),
            "found_price_filtered_model",
            f"{failing and failing['id']} market_in={failing and failing['market_input_per_1m']}",
        )
        record(bool(no_offer), "found_model_without_offer", str(no_offer and no_offer["id"]))
        record(bool(not_token), "found_non_token_priced_model", str(not_token and not_token["id"]))

        # a cheap, high-discount model must survive the default caps
        record(
            "qwen3.8-max-0902" in {m["id"] for m in data},
            "cheap_high_discount_model_served",
            "qwen3.8-max-0902 (market $0.0022/$0.0065 per 1M) present in /v1/models",
        )

        # 4. per-model retrieval
        if passing_id:
            status, body = request("GET", f"/v1/models/{passing_id}")
            record(
                status == 200 and isinstance(body, dict) and body.get("id") == passing_id,
                "retrieve_passing_model",
                f"http {status}",
            )

        # 5. capped-out model must be rejected locally with failed_filters
        if failing:
            status, body = request(
                "POST",
                "/v1/chat/completions",
                {"model": failing["id"], "messages": [{"role": "user", "content": "hi"}]},
            )
            err = body.get("error", {}) if isinstance(body, dict) else {}
            record(
                status == 403 and err.get("type") == "model_filtered_out",
                "reject_capped_model",
                f"http {status} failed_filters={err.get('failed_filters')}",
            )

        # 5b. models that cannot be routed are rejected fail-closed
        for label, row, expected in (
            ("no_offer", no_offer, "no_market_offer"),
            ("not_token_priced", not_token, "not_token_priced"),
        ):
            if row:
                status, body = request(
                    "POST",
                    "/v1/chat/completions",
                    {"model": row["id"], "messages": [{"role": "user", "content": "hi"}]},
                )
                err = body.get("error", {}) if isinstance(body, dict) else {}
                record(
                    status == 403 and expected in (err.get("failed_filters") or []),
                    f"reject_{label}",
                    f"http {status} model={row['id']} failed_filters={err.get('failed_filters')}",
                )

        # 6. unknown model fails closed
        status, body = request(
            "POST",
            "/v1/chat/completions",
            {"model": "definitely-not-a-real-model-xyz", "messages": []},
        )
        err = body.get("error", {}) if isinstance(body, dict) else {}
        record(
            status == 403 and "unknown_model" in (err.get("failed_filters") or []),
            "reject_unknown_model",
            f"http {status} failed_filters={err.get('failed_filters')}",
        )

        # 7. missing model field is a 400, not a crash
        status, body = request("POST", "/v1/chat/completions", {"messages": []})
        err = body.get("error", {}) if isinstance(body, dict) else {}
        record(
            status in (400, 503),
            "reject_missing_model_field",
            f"http {status} type={err.get('type')}",
        )

        # 8. passing model with no API key -> explicit 503 (never forwarded blindly)
        if passing_id:
            status, body = request(
                "POST",
                "/v1/chat/completions",
                {"model": passing_id, "messages": [{"role": "user", "content": "hi"}]},
            )
            err = body.get("error", {}) if isinstance(body, dict) else {}
            if os.environ.get("SURPLUS_API_KEY"):
                record(
                    status == 200,
                    "forward_passing_model_with_key",
                    f"http {status} (real key present)",
                )
            else:
                record(
                    status == 503 and err.get("type") == "api_key_missing",
                    "gate_requires_api_key",
                    f"http {status} type={err.get('type')}",
                )

        # 9. manual refresh keeps the catalog fresh
        status, body = request("POST", "/admin/api/refresh")
        record(
            status == 200 and isinstance(body, dict) and body.get("ok") is True,
            "manual_refresh",
            f"http {status} ok={body.get('ok') if isinstance(body, dict) else body} "
            f"models={body.get('counts', {}).get('total') if isinstance(body, dict) else '?'}",
        )

        return report()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(CONFIG_DIR, ignore_errors=True)


def report() -> int:
    passed = sum(1 for ok, _, _ in RESULTS if ok)
    failed = [name for ok, name, _ in RESULTS if not ok]
    suffix = f"; failed: {', '.join(failed)}" if failed else ""
    print(f"\n{passed}/{len(RESULTS)} checks passed{suffix}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
