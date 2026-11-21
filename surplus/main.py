"""Surplus Router app: filter gate + OpenAI-compatible passthrough + admin API.

Run: ``uv run python -m surplus.main`` (host/port come from config.ini).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .account import account_reader
from .catalog import Catalog
from .config import (
    AppConfig,
    load_config,
    mask_key,
    normalize_proxy_mode,
    normalize_proxy_url,
    public_view,
    resolve_api_key,
    resolve_config_path,
    save_config,
    write_example_config,
)
from .filters import UNKNOWN_MODEL, evaluate, evaluate_by_id, minimum_discount_path_prefix
from .proxy import build_client, forward
from .tunnel import Tunnel

log = logging.getLogger("surplus")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# Where config.ini lives: ./config.ini when present, else the per-user dir
# (%APPDATA%\surplus-router on Windows). See config.resolve_config_path.
CONFIG_PATH = resolve_config_path()

# Paths proxied without a model gate (no model in body, or handled explicitly).
PASS_THROUGH_PATHS = {
    "chat/completions",
    "completions",
    "embeddings",
    "responses",
    "audio/speech",
    "audio/transcriptions",
    "images/generations",
}


class State:
    """Mutable app state: config can be reloaded in-process without restart."""

    def __init__(self) -> None:
        self.cfg: AppConfig = load_config(CONFIG_PATH)
        self.catalog = Catalog(self.cfg.upstream.base_url)
        # Provisional direct client; swapped for a proxied one once the tunnel
        # is up. Built at call time reads, so a swap never breaks live streams.
        self.client = build_client(self.cfg.upstream.request_timeout_seconds)
        self.tunnel = Tunnel(lambda: self.cfg.proxy, self.cfg.upstream.base_url)
        self.client_proxy_url: str | None = None
        self._retired: list[httpx.AsyncClient] = []
        self.refresh_task: asyncio.Task | None = None
        self.supervisor_task: asyncio.Task | None = None
        self.started_at = time.time()

    def swap_client(self, new_client: httpx.AsyncClient) -> None:
        """Replace the upstream client; retire the old one after a grace period.

        Grace must exceed the request timeout: a streaming completion holds the
        client it was started with, and closing it early would cut the stream.
        """
        old = self.client
        self.client = new_client
        if old is not None and old is not new_client:
            self._retired.append(old)
            task = asyncio.create_task(self._close_later(old))

            def _drop(client: httpx.AsyncClient = old) -> None:
                if client in self._retired:
                    self._retired.remove(client)

            task.add_done_callback(_drop)

    async def _close_later(self, client: httpx.AsyncClient) -> None:
        grace = max(60.0, self.cfg.upstream.request_timeout_seconds + 60.0)
        try:
            await asyncio.sleep(grace)
        except asyncio.CancelledError:
            return
        with contextlib.suppress(Exception):
            await client.aclose()

    async def sync_client(self) -> None:
        """Point the upstream client at the tunnel's current proxy URL."""
        wanted = self.tunnel.proxy_url()
        if wanted != self.client_proxy_url:
            self.client_proxy_url = wanted
            log.info("upstream client rebuilt (proxy: %s)", wanted or "direct")
            self.swap_client(
                build_client(self.cfg.upstream.request_timeout_seconds, wanted)
            )
            await self.catalog.refresh(self.client)

    async def apply_proxy(self) -> dict:
        """Apply the current [proxy] settings; returns the tunnel status."""
        await self.tunnel.apply()
        await self.sync_client()
        return self.tunnel.status()

    async def close_clients(self) -> None:
        for client in list(self._retired) + [self.client]:
            with contextlib.suppress(Exception):
                await client.aclose()
        self._retired.clear()


def _proxy_fingerprint(cfg: AppConfig) -> tuple:
    """Which proxy settings matter for the running tunnel and client."""
    proxy = cfg.proxy
    return (
        proxy.enabled,
        proxy.mode,
        proxy.proxy_url,
        proxy.subscription_url,
        proxy.subscription_links,
        proxy.xray_binary,
    )


state = State()
_applied_proxy_fp: tuple | None = None


def reload_config() -> AppConfig:
    """Re-read config.ini from disk (used after the admin UI saves)."""
    global _config_mtime
    state.cfg = load_config(CONFIG_PATH)
    _config_mtime = _stat_mtime(CONFIG_PATH)
    return state.cfg


def _stat_mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def maybe_reload_config() -> AppConfig:
    """Pick up edits made directly to config.ini, without a restart.

    One stat() per request; the file is only re-parsed when its mtime moved.
    Host/port changes still need a restart (the socket is already bound), but
    filters, caps, TTL and the API key apply immediately.
    """
    global _config_mtime
    mtime = _stat_mtime(CONFIG_PATH)
    if mtime is None:
        return state.cfg
    if _config_mtime is None:
        _config_mtime = mtime
        return state.cfg
    if mtime != _config_mtime:
        _config_mtime = mtime
        previous = state.cfg
        state.cfg = load_config(CONFIG_PATH)
        state.tunnel.upstream_base = state.cfg.upstream.base_url
        log.info(
            "config.ini changed on disk; reloaded (caps %s/%s -> %s/%s)",
            previous.filters.max_input_per_1m,
            previous.filters.max_output_per_1m,
            state.cfg.filters.max_input_per_1m,
            state.cfg.filters.max_output_per_1m,
        )
    return state.cfg


_config_mtime: float | None = _stat_mtime(CONFIG_PATH)


def api_key_or_error() -> tuple[str | None, JSONResponse | None]:
    key, source = resolve_api_key(state.cfg)
    if key is None:
        return None, JSONResponse(
            status_code=503,
            content={
                "error": {
                    "message": (
                        "No API key configured. Set it in the admin panel "
                        f"(/admin) or export {state.cfg.api_key_env}."
                    ),
                    "type": "api_key_missing",
                }
            },
        )
    return key, None


def upstream_url(sub_path: str) -> str:
    """Build the upstream URL, inserting the /min{N} discount-routing prefix."""
    prefix = minimum_discount_path_prefix(state.cfg.filters)
    base = state.cfg.upstream.base_url.rstrip("/")
    return f"{base}{prefix}/v1/{sub_path.lstrip('/')}"


def filtered_models() -> list[dict]:
    filters = state.cfg.filters
    return [
        model.raw
        for model in state.catalog.models.values()
        if not evaluate(model, filters)
    ]


def gate_model(model_id: str | None) -> JSONResponse | None:
    """Return an error response when the model must not be forwarded, else None."""
    if not model_id:
        return JSONResponse(
            status_code=400,
            content={
                "error": {"message": "Request body is missing 'model'.", "type": "bad_request"}
            },
        )

    known = model_id in state.catalog.models
    if not known and not state.cfg.filters.allow_unknown_models:
        return JSONResponse(
            status_code=403,
            content={
                "error": {
                    "message": (
                        f"Model '{model_id}' is not in the Surplus catalog; rejected "
                        "(fail-closed). Enable allow_unknown_models to forward it anyway."
                    ),
                    "type": "model_filtered_out",
                    "model": model_id,
                    "failed_filters": [UNKNOWN_MODEL],
                }
            },
        )

    passed, failed = evaluate_by_id(model_id, state.catalog.models, state.cfg.filters)
    if passed:
        return None
    model = state.catalog.models.get(model_id)
    detail = ""
    if model is not None:
        market_in = model.market_input_per_1m
        market_out = model.market_output_per_1m
        detail = (
            f" market=${market_in:.6f}/1M in, ${market_out:.6f}/1M out"
            if market_in is not None and market_out is not None
            else f" price_class={model.price_class}"
        )
        detail += (
            f" (list=${model.list_input_per_1m:.4f}/${model.list_output_per_1m:.4f} per 1M)"
        )
    return JSONResponse(
        status_code=403,
        content={
            "error": {
                "message": f"Model '{model_id}' rejected by router filters.{detail}",
                "type": "model_filtered_out",
                "model": model_id,
                "failed_filters": failed,
            }
        },
    )


def listening_on(request: Request, cfg: AppConfig) -> tuple[str, int]:
    """The socket this process is actually bound to, not what config.ini says.

    They differ when the owner changed the port in the panel but has not
    restarted yet - which is exactly when the UI must warn about it.
    """
    server = request.scope.get("server")
    if isinstance(server, tuple) and len(server) == 2 and server[1]:
        return str(server[0]), int(server[1])
    return cfg.server.host, cfg.server.port


def _none_last(value: float | None) -> float:
    """Sort key that pushes models with no price to the end of the list."""
    return float("inf") if value is None else float(value)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    """Load the catalog at startup, then refresh it on a TTL loop forever."""
    log.info("config file: %s", CONFIG_PATH)

    # Bring the proxy tunnel up first (managed mode may download Xray-core);
    # a failure here is not fatal: the router runs direct and reports why.
    global _applied_proxy_fp
    await state.apply_proxy()
    _applied_proxy_fp = _proxy_fingerprint(state.cfg)

    ok = await state.catalog.refresh(state.client)
    if ok:
        log.info(
            "catalog loaded: %d models (%d passing filters)",
            len(state.catalog.models),
            len(filtered_models()),
        )
    else:
        log.warning("startup catalog refresh failed; retrying in background")

    state.refresh_task = asyncio.create_task(_refresh_loop())
    state.supervisor_task = asyncio.create_task(_supervisor_loop())
    try:
        yield
    finally:
        for task in (state.refresh_task, state.supervisor_task):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        await state.tunnel.stop()
        await state.close_clients()


async def _refresh_loop() -> None:
    while True:
        ttl = max(5, state.cfg.upstream.catalog_ttl_seconds)
        await asyncio.sleep(ttl)
        await state.catalog.refresh(state.client)


async def _supervisor_loop() -> None:
    """Watch the tunnel: probe, rotate, refresh the subscription, and pick up
    manual edits made to the [proxy] section of config.ini."""
    global _applied_proxy_fp
    while True:
        interval = max(10, state.cfg.proxy.health_check_interval_seconds)
        await asyncio.sleep(interval)
        try:
            maybe_reload_config()
            fingerprint = _proxy_fingerprint(state.cfg)
            if fingerprint != _applied_proxy_fp:
                log.info("[proxy] settings changed; re-applying tunnel")
                await state.apply_proxy()
                _applied_proxy_fp = fingerprint
                continue
            await state.tunnel.health_tick()
            await state.sync_client()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the supervisor must never die
            log.warning("tunnel supervision failed: %s", exc)


app = FastAPI(
    title="Surplus Router",
    version=__version__,
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/admin/assets", StaticFiles(directory=STATIC_DIR), name="admin-assets")


# ------------------------------------------------------------------ admin UI


@app.get("/admin")
@app.get("/admin/")
async def admin_ui() -> FileResponse:
    """Serve the control panel. Localhost-only bind keeps it off the network."""
    return FileResponse(STATIC_DIR / "index.html")


# ---------------------------------------------------------------- admin API


@app.get("/")
async def root() -> JSONResponse:
    key, source = resolve_api_key(state.cfg)
    return JSONResponse(
        {
            "service": "surplus-router",
            "version": __version__,
            "admin_ui": "/admin",
            "openai_base_url": "/v1",
            "api_key_source": source,
            "api_key_masked": mask_key(key),
            "catalog": state.catalog.snapshot(state.cfg.upstream.catalog_ttl_seconds),
        }
    )


@app.get("/admin/api/status")
async def admin_status(request: Request) -> JSONResponse:
    cfg = maybe_reload_config()
    filters = cfg.filters
    total = len(state.catalog.models)
    passing = [m for m in state.catalog.models.values() if not evaluate(m, filters)]
    key, source = resolve_api_key(cfg)
    host, port = listening_on(request, cfg)
    return JSONResponse(
        {
            "version": __version__,
            "uptime_seconds": round(time.time() - state.started_at, 1),
            "config": public_view(cfg),
            "api_key": {"source": source, "masked": mask_key(key)},
            "listening_on": {"host": host, "port": port},
            "proxy": state.tunnel.status(),
            "restart_required": (host, port) != (cfg.server.host, cfg.server.port),
            "catalog": state.catalog.snapshot(cfg.upstream.catalog_ttl_seconds),
            "counts": {
                "total": total,
                "passing": len(passing),
                "filtered_out": total - len(passing),
                **{f"class_{k}": v for k, v in state.catalog.class_summary().items()},
            },
            "minimum_discount_prefix": minimum_discount_path_prefix(filters),
        }
    )


@app.get("/admin/api/models")
async def admin_models(
    q: str = "",
    passed_only: bool = False,
    limit: int = 1000,
    sort: str = "output",
    dir: str = "asc",
    price_class: str = "",
    provider: str = "",
) -> JSONResponse:
    """Annotated catalog for the admin table: price per 1M + pass/fail reasons."""
    filters = maybe_reload_config().filters
    needle = q.strip().lower()
    rows = []
    for model in state.catalog.models.values():
        if needle and needle not in model.id.lower() and needle not in model.name.lower():
            continue
        if price_class and model.price_class != price_class:
            continue
        if provider and model.provider.lower() != provider.lower():
            continue
        failed = evaluate(model, filters)
        if passed_only and failed:
            continue
        rows.append(model.to_admin_row(failed))

    descending = dir == "desc"
    numeric_keys = {
        "output": "market_output_per_1m",
        "input": "market_input_per_1m",
        "list_output": "list_output_per_1m",
        "discount": "estimated_discount_pct",
        "context": "context_length",
        "sellers": "healthy_seller_count",
        "uncapped": "uncapped_offer_count",
        "uptime": "uptime_pct",
        "ttft": "ttft_p50_ms",
        "tps": "tps_p50",
        "requests": "requests_24h",
        "liquidity": "liquidity_usd",
        "cache_write": "cache_write_per_1m",
        "trusted_output": "trusted_output_per_1m",
    }
    if sort == "name":
        rows.sort(key=lambda r: r["id"].lower(), reverse=descending)
    elif sort in numeric_keys:
        field = numeric_keys[sort]
        # Missing measurements sort last in both directions.
        missing = float("-inf") if descending else float("inf")
        rows.sort(
            key=lambda r: (r.get(field) if r.get(field) is not None else missing, r["id"]),
            reverse=descending,
        )
    else:
        rows.sort(key=lambda r: (_none_last(r["market_output_per_1m"]), r["id"]))
    return JSONResponse(
        {
            "count": len(rows),
            "returned": min(len(rows), max(1, limit)),
            "models": rows[: max(1, limit)],
        }
    )


@app.get("/admin/api/models/{model_id}")
async def admin_model_detail(model_id: str) -> JSONResponse:
    """Full dossier for one model: per-provider offers, trend, capability."""
    filters = maybe_reload_config().filters
    model = state.catalog.models.get(model_id)
    if model is None:
        return JSONResponse(
            status_code=404, content={"error": f"Unknown model '{model_id}'."}
        )
    return JSONResponse(model.to_detail(evaluate(model, filters)))


@app.get("/admin/api/account")
async def admin_account(refresh: bool = False) -> JSONResponse:
    """Balance + spend overview, fetched upstream with the owner's key."""
    cfg = maybe_reload_config()
    key, _source = resolve_api_key(cfg)
    data = await account_reader.account(
        state.client, cfg.upstream.base_url, key, force=refresh
    )
    return JSONResponse(data)


@app.get("/admin/api/account/requests")
async def admin_account_requests(request: Request) -> JSONResponse:
    """Pass-through of the upstream request log (cost/model/latency per call)."""
    cfg = maybe_reload_config()
    key, _source = resolve_api_key(cfg)
    data = await account_reader.requests(
        state.client, cfg.upstream.base_url, key, dict(request.query_params)
    )
    return JSONResponse(data)


@app.post("/admin/api/refresh")
async def admin_refresh() -> JSONResponse:
    maybe_reload_config()
    ok = await state.catalog.refresh(state.client)
    return JSONResponse(
        {
            "ok": ok,
            "catalog": state.catalog.snapshot(state.cfg.upstream.catalog_ttl_seconds),
            "counts": {"total": len(state.catalog.models), "passing": len(filtered_models())},
        }
    )


# ----------------------------------------------------------- proxy tunnel


@app.get("/admin/api/proxy")
async def admin_proxy_status() -> JSONResponse:
    return JSONResponse(state.tunnel.status())


@app.post("/admin/api/proxy/toggle")
async def admin_proxy_toggle(payload: dict[str, Any]) -> JSONResponse:
    """Enable or disable the tunnel; applies immediately, no restart."""
    global _applied_proxy_fp
    enabled = bool(payload.get("enabled"))
    reload_config()
    state.cfg.proxy.enabled = enabled
    save_config(state.cfg, CONFIG_PATH)
    reload_config()
    status = await state.apply_proxy()
    _applied_proxy_fp = _proxy_fingerprint(state.cfg)
    log.info("proxy tunnel %s", "enabled" if enabled else "disabled")
    return JSONResponse({"ok": True, "enabled": enabled, "proxy": status})


@app.post("/admin/api/proxy/rotate")
async def admin_proxy_rotate() -> JSONResponse:
    """Skip to the next subscribed route (managed mode)."""
    await state.tunnel.rotate("manual")
    await state.sync_client()
    return JSONResponse({"ok": True, "proxy": state.tunnel.status()})


@app.post("/admin/api/proxy/refresh-subscription")
async def admin_proxy_refresh_subscription() -> JSONResponse:
    """Re-fetch the subscription and re-run the route ladder (managed mode)."""
    await state.tunnel.refresh_subscription()
    await state.sync_client()
    return JSONResponse({"ok": True, "proxy": state.tunnel.status()})


@app.post("/admin/api/proxy/test")
async def admin_proxy_test(payload: dict[str, Any]) -> JSONResponse:
    """Try one round trip through a proxy without touching live traffic.

    With no ``proxy_url`` in the payload the router's effective proxy is used.
    """
    proxy = str(payload.get("proxy_url") or "").strip()
    if not proxy:
        proxy = state.tunnel.proxy_url() or ""
    if not proxy:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "no proxy URL to test (tunnel is direct)"},
        )
    timeout = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0)
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(proxy=proxy, timeout=timeout, trust_env=False) as probe:
            await probe.get(f"{state.cfg.upstream.base_url.rstrip('/')}/v1/models")
        return JSONResponse(
            {
                "ok": True,
                "proxy_url": proxy,
                "latency_ms": int((time.perf_counter() - started) * 1000),
            }
        )
    except Exception as exc:  # noqa: BLE001 - the point is to report failures
        return JSONResponse(
            status_code=502,
            content={"ok": False, "proxy_url": proxy, "error": f"{type(exc).__name__}: {exc}"},
        )


@app.get("/admin/api/config")
async def admin_get_config() -> JSONResponse:
    return JSONResponse(public_view(state.cfg))


@app.post("/admin/api/config")
async def admin_set_config(request: Request, payload: dict[str, Any]) -> JSONResponse:
    """Save config from the UI. The API key is only written when supplied."""
    global _applied_proxy_fp
    reload_config()
    cfg = state.cfg
    # What the process is actually listening on right now: a host/port change
    # cannot take effect without a restart, so the UI must say so.
    running_host, running_port = listening_on(request, cfg)

    server = payload.get("server") or {}
    if "host" in server and isinstance(server["host"], str) and server["host"].strip():
        cfg.server.host = server["host"].strip()
    if "open_browser" in server:
        cfg.server.open_browser = bool(server["open_browser"])
    if "port" in server:
        try:
            port = int(server["port"])
            if 1 <= port <= 65535:
                cfg.server.port = port
        except (TypeError, ValueError):
            return JSONResponse(status_code=400, content={"error": "server.port must be 1-65535"})

    upstream = payload.get("upstream") or {}
    if isinstance(upstream.get("base_url"), str) and upstream["base_url"].strip():
        cfg.upstream.base_url = upstream["base_url"].strip().rstrip("/")
    if "catalog_ttl_seconds" in upstream:
        with contextlib.suppress(TypeError, ValueError):
            cfg.upstream.catalog_ttl_seconds = max(5, int(upstream["catalog_ttl_seconds"]))
    if "request_timeout_seconds" in upstream:
        with contextlib.suppress(TypeError, ValueError):
            cfg.upstream.request_timeout_seconds = max(
                5.0, float(upstream["request_timeout_seconds"])
            )

    incoming = payload.get("filter") or {}
    filters = cfg.filters
    bool_keys = (
        "enable_price_input",
        "enable_price_output",
        "enable_discount",
        "enable_models_allow",
        "enable_models_deny",
        "enable_providers_allow",
        "enable_context_min",
        "enable_trusted_only",
        "enable_min_uptime",
        "enable_max_ttft",
        "enable_min_tps",
        "enable_min_sellers",
        "enable_min_uncapped",
        "enable_cache_write_cap",
        "enable_require_features",
        "enable_exclude_tightening",
        "allow_unknown_models",
    )
    for key in bool_keys:
        if key in incoming:
            setattr(filters, key, bool(incoming[key]))

    list_keys = ("allow_models", "deny_models", "allow_providers", "require_features")
    for key in list_keys:
        if key in incoming:
            value = incoming[key]
            if isinstance(value, str):
                setattr(filters, key, [i.strip() for i in value.split(",") if i.strip()])
            elif isinstance(value, list):
                setattr(filters, key, [str(i).strip() for i in value if str(i).strip()])

    float_keys = (
        "max_input_per_1m",
        "max_output_per_1m",
        "min_uptime_pct",
        "max_ttft_ms",
        "min_tps",
        "max_cache_write_per_1m",
    )
    for key in float_keys:
        if key in incoming:
            with contextlib.suppress(TypeError, ValueError):
                setattr(filters, key, max(0.0, float(incoming[key])))

    int_keys = (
        "min_discount_pct",
        "min_context_length",
        "min_healthy_sellers",
        "min_uncapped_offers",
    )
    for key in int_keys:
        if key in incoming:
            with contextlib.suppress(TypeError, ValueError):
                setattr(filters, key, int(incoming[key]))
    filters.min_discount_pct = max(0, min(100, filters.min_discount_pct))

    # API key: only overwrite when a non-empty value is supplied.
    auth = payload.get("auth") or {}
    new_key = auth.get("api_key")
    if isinstance(new_key, str) and new_key.strip():
        cfg.file_api_key = new_key.strip()
    elif auth.get("clear_api_key") is True:
        cfg.file_api_key = ""

    # Proxy tunnel: persisted here, applied by the tunnel endpoints (or the
    # supervisor on the next tick). The subscription URL is only written when a
    # non-empty value is supplied, like the API key.
    proxy_payload = payload.get("proxy") or {}
    proxy = cfg.proxy
    previous_fingerprint = _proxy_fingerprint(cfg)
    if "enabled" in proxy_payload:
        proxy.enabled = bool(proxy_payload["enabled"])
    if isinstance(proxy_payload.get("mode"), str) and proxy_payload["mode"].strip():
        proxy.mode = normalize_proxy_mode(proxy_payload["mode"])
    if isinstance(proxy_payload.get("proxy_url"), str):
        proxy.proxy_url = normalize_proxy_url(proxy_payload["proxy_url"])
    new_sub = proxy_payload.get("subscription_url")
    if isinstance(new_sub, str) and new_sub.strip():
        proxy.subscription_url = new_sub.strip()
    elif proxy_payload.get("clear_subscription_url") is True:
        proxy.subscription_url = ""
    new_links = proxy_payload.get("subscription_links")
    if isinstance(new_links, str) and new_links.strip():
        proxy.subscription_links = new_links.strip()
    elif proxy_payload.get("clear_subscription_links") is True:
        proxy.subscription_links = ""
    if isinstance(proxy_payload.get("xray_binary"), str):
        proxy.xray_binary = proxy_payload["xray_binary"].strip()
    for key, setter in (
        ("health_check_interval_seconds", lambda v: max(10, int(v))),
        ("failure_threshold", lambda v: max(1, int(v))),
        ("subscription_refresh_hours", lambda v: max(1, int(v))),
    ):
        if key in proxy_payload:
            with contextlib.suppress(TypeError, ValueError):
                setattr(proxy, key, setter(proxy_payload[key]))

    save_config(cfg, CONFIG_PATH)
    reload_config()

    # Applying a newly enabled tunnel can take a while (subscription fetch,
    # Xray download); only do it when the proxy settings actually changed.
    if _proxy_fingerprint(cfg) != previous_fingerprint:
        status = await state.apply_proxy()
        _applied_proxy_fp = _proxy_fingerprint(state.cfg)
    else:
        status = state.tunnel.status()

    passing = len(filtered_models())
    total = len(state.catalog.models)
    return JSONResponse(
        {
            "ok": True,
            "config": public_view(state.cfg),
            "counts": {"total": total, "passing": passing, "filtered_out": total - passing},
            "restart_required": (
                cfg.server.host != running_host or cfg.server.port != running_port
            ),
            "listening_on": {"host": running_host, "port": running_port},
            "proxy": status,
        }
    )


# ------------------------------------------------------------ OpenAI routes


@app.get("/v1/models")
async def list_models() -> JSONResponse:
    maybe_reload_config()
    return JSONResponse({"object": "list", "data": filtered_models()})


@app.get("/v1/models/{model_id}")
async def retrieve_model(model_id: str) -> JSONResponse:
    maybe_reload_config()
    model = state.catalog.models.get(model_id)
    if model is None:
        return JSONResponse(
            status_code=404, content={"error": {"message": f"Unknown model '{model_id}'."}}
        )
    error = gate_model(model_id)
    if error is not None:
        return error
    return JSONResponse(model.raw)


@app.api_route(
    "/v1/{sub_path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
)
async def proxy_v1(sub_path: str, request: Request):
    """Gate on the requested model first, then stream everything through.

    Order matters: a model rejected by the filters must return 403 even when no
    API key is configured - the router never leaks a key problem as the reason
    a filtered model was refused.
    """
    maybe_reload_config()
    body: bytes = await request.body()
    model_id: str | None = None
    if body:
        with contextlib.suppress(ValueError):
            payload = json.loads(body)
            if isinstance(payload, dict):
                raw_model = payload.get("model")
                if isinstance(raw_model, str):
                    model_id = raw_model

    if model_id is not None or sub_path.strip("/") in PASS_THROUGH_PATHS:
        error = gate_model(model_id)
        if error is not None:
            return error

    key, key_error = api_key_or_error()
    if key_error is not None:
        return key_error

    url = upstream_url(sub_path)
    try:
        return await forward(state.client, request, url, key)
    except Exception as exc:  # noqa: BLE001 - surface upstream failures as JSON
        log.warning("upstream request failed for %s: %s", sub_path, exc)
        hint = state.tunnel.error_hint()
        message = f"Upstream request failed: {type(exc).__name__}"
        error_type = "upstream_error"
        if hint:
            # The tunnel is enabled but not healthy: say so instead of a bare
            # 502, so the agent user knows exactly where to look.
            message = f"{message}. {hint}"
            error_type = "proxy_tunnel_down"
        return JSONResponse(
            status_code=502,
            content={
                "error": {
                    "message": message,
                    "type": error_type,
                }
            },
        )


def ensure_config(path: Path) -> AppConfig:
    """Load config.ini, creating it from defaults on first run.

    Writes config.example.ini next to it only when missing, so an edited example
    is never clobbered.
    """
    example = path.parent / "config.example.ini"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        cfg = AppConfig(config_path=path)
        save_config(cfg, path)
        if not example.exists():
            write_example_config(example)
        log.info("first run: created %s with default settings", path)
        return cfg
    return load_config(path)


def port_in_use(host: str, port: int) -> bool:
    """Probe the bind address before doing any work, so a busy port fails fast."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    probe_host = "::" if host in {"::", ""} else ("0.0.0.0" if host == "0.0.0.0" else host)
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        try:
            sock.bind((probe_host, port))
        except OSError:
            return True
    return False


def admin_url(host: str, port: int) -> str:
    """Browsable admin URL. Wildcard bind hosts are not dialable, so use loopback."""
    dialable = "127.0.0.1" if host in {"0.0.0.0", "::", "", "*"} else host
    if ":" in dialable:  # IPv6 literal
        dialable = f"[{dialable}]"
    return f"http://{dialable}:{port}/admin"


def wait_for_port(host: str, port: int, timeout: float = 20.0) -> bool:
    """Block until something accepts a TCP connection on host:port (or timeout)."""
    dialable = "127.0.0.1" if host in {"0.0.0.0", "::", "", "*"} else host
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((dialable, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.15)
    return False


def open_browser_when_ready(
    url: str, host: str, port: int, timeout: float = 20.0, opener=webbrowser.open
) -> threading.Thread:
    """Open the admin panel once the server actually accepts connections.

    Runs on a daemon thread so it never delays or blocks startup, and never
    opens a tab that would just show a connection error.
    """

    def _run() -> None:
        if wait_for_port(host, port, timeout=timeout):
            try:
                opener(url)
                log.info("opened the admin panel in your browser: %s", url)
            except Exception as exc:  # noqa: BLE001 - no browser, headless box, etc.
                log.warning("could not open a browser (%s); admin panel is at %s", exc, url)
        else:
            log.warning(
                "server did not accept connections within %.0fs; not opening a browser",
                timeout,
            )

    thread = threading.Thread(target=_run, name="open-admin-browser", daemon=True)
    thread.start()
    return thread


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="surplus-router",
        description="Local price-filtered proxy for the Surplus Intelligence marketplace.",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="override the bind host from config.ini (this run only)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="override the proxy port from config.ini (this run only)",
    )
    parser.add_argument(
        "--config",
        default=None,
        help=f"path to config.ini (default: {CONFIG_PATH})",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="do not open the admin panel in a browser at startup",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    global CONFIG_PATH
    CONFIG_PATH = resolve_config_path(args.config)

    cfg = ensure_config(CONFIG_PATH)
    # The module-level state was built at import time; point it at the resolved
    # config so --config and the installed-tool layout both work.
    state.cfg = cfg
    state.catalog.base_url = cfg.upstream.base_url
    state.tunnel.upstream_base = cfg.upstream.base_url
    host = args.host or cfg.server.host
    port = args.port or cfg.server.port

    if port_in_use(host, port):
        print(
            f"error: {host}:{port} is already in use.\n"
            f"  another router instance is probably running, or another app holds the port.\n"
            f"  start on a different port for this run:  python -m surplus.main --port {port + 1}\n"
            f"  or change it permanently in {CONFIG_PATH} ([server] port), "
            f"or in the admin panel under Connection.",
            file=sys.stderr,
        )
        return 2

    url = admin_url(host, port)
    log.info("starting surplus-router on http://%s:%d (admin: %s)", host, port, url)
    log.info("using config file: %s", CONFIG_PATH)

    if cfg.server.open_browser and not args.no_browser:
        # Waits for the socket to accept connections, then opens the panel.
        open_browser_when_ready(url, host, port)
    else:
        log.info("browser auto-open disabled; admin panel is at %s", url)

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info",
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
