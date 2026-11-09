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
import time
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .catalog import Catalog
from .config import (
    AppConfig,
    load_config,
    mask_key,
    public_view,
    resolve_api_key,
    save_config,
    write_example_config,
)
from .filters import UNKNOWN_MODEL, evaluate, evaluate_by_id, minimum_discount_path_prefix
from .proxy import build_client, forward

log = logging.getLogger("surplus")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.ini"

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
        self.client = build_client(self.cfg.upstream.request_timeout_seconds)
        self.refresh_task: asyncio.Task | None = None
        self.started_at = time.time()


state = State()


def reload_config() -> AppConfig:
    """Re-read config.ini from disk (used after the admin UI saves)."""
    state.cfg = load_config(CONFIG_PATH)
    return state.cfg


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
        detail = (
            f" input=${model.input_per_1m:.4f}/1M output=${model.output_per_1m:.4f}/1M"
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


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    """Load the catalog at startup, then refresh it on a TTL loop forever."""
    if not CONFIG_PATH.exists():
        write_example_config(CONFIG_PATH.parent / "config.example.ini")
        log.info("config.ini not found; using built-in defaults")

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
    try:
        yield
    finally:
        if state.refresh_task:
            state.refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await state.refresh_task
        await state.client.aclose()


async def _refresh_loop() -> None:
    while True:
        ttl = max(5, state.cfg.upstream.catalog_ttl_seconds)
        await asyncio.sleep(ttl)
        await state.catalog.refresh(state.client)


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
    cfg = state.cfg
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
            "restart_required": (host, port) != (cfg.server.host, cfg.server.port),
            "catalog": state.catalog.snapshot(cfg.upstream.catalog_ttl_seconds),
            "counts": {
                "total": total,
                "passing": len(passing),
                "filtered_out": total - len(passing),
                **state.catalog.priced_summary(),
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
) -> JSONResponse:
    """Annotated catalog for the admin table: price per 1M + pass/fail reasons."""
    filters = state.cfg.filters
    needle = q.strip().lower()
    rows = []
    for model in state.catalog.models.values():
        if needle and needle not in model.id.lower() and needle not in model.name.lower():
            continue
        failed = evaluate(model, filters)
        if passed_only and failed:
            continue
        rows.append(model.to_admin_row(failed))

    sort_keys = {
        "output": lambda r: (r["output_per_1m"], r["input_per_1m"], r["id"]),
        "input": lambda r: (r["input_per_1m"], r["output_per_1m"], r["id"]),
        "name": lambda r: r["id"].lower(),
        "discount": lambda r: (
            -(r["estimated_discount_pct"] if r["estimated_discount_pct"] is not None else -1e9)
        ),
        "context": lambda r: -r["context_length"],
    }
    rows.sort(key=sort_keys.get(sort, sort_keys["output"]))
    return JSONResponse(
        {
            "count": len(rows),
            "returned": min(len(rows), max(1, limit)),
            "models": rows[: max(1, limit)],
        }
    )


@app.post("/admin/api/refresh")
async def admin_refresh() -> JSONResponse:
    ok = await state.catalog.refresh(state.client)
    return JSONResponse(
        {
            "ok": ok,
            "catalog": state.catalog.snapshot(state.cfg.upstream.catalog_ttl_seconds),
            "counts": {"total": len(state.catalog.models), "passing": len(filtered_models())},
        }
    )


@app.get("/admin/api/config")
async def admin_get_config() -> JSONResponse:
    return JSONResponse(public_view(state.cfg))


@app.post("/admin/api/config")
async def admin_set_config(request: Request, payload: dict[str, Any]) -> JSONResponse:
    """Save config from the UI. The API key is only written when supplied."""
    reload_config()
    cfg = state.cfg
    # What the process is actually listening on right now: a host/port change
    # cannot take effect without a restart, so the UI must say so.
    running_host, running_port = listening_on(request, cfg)

    server = payload.get("server") or {}
    if "host" in server and isinstance(server["host"], str) and server["host"].strip():
        cfg.server.host = server["host"].strip()
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
        "allow_unknown_models",
    )
    for key in bool_keys:
        if key in incoming:
            setattr(filters, key, bool(incoming[key]))

    list_keys = ("allow_models", "deny_models", "allow_providers")
    for key in list_keys:
        if key in incoming:
            value = incoming[key]
            if isinstance(value, str):
                setattr(filters, key, [i.strip() for i in value.split(",") if i.strip()])
            elif isinstance(value, list):
                setattr(filters, key, [str(i).strip() for i in value if str(i).strip()])

    float_keys = ("max_input_per_1m", "max_output_per_1m")
    for key in float_keys:
        if key in incoming:
            with contextlib.suppress(TypeError, ValueError):
                setattr(filters, key, max(0.0, float(incoming[key])))

    int_keys = ("min_discount_pct", "min_context_length")
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

    save_config(cfg, CONFIG_PATH)
    reload_config()

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
        }
    )


# ------------------------------------------------------------ OpenAI routes


@app.get("/v1/models")
async def list_models() -> JSONResponse:
    return JSONResponse({"object": "list", "data": filtered_models()})


@app.get("/v1/models/{model_id}")
async def retrieve_model(model_id: str) -> JSONResponse:
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
        return JSONResponse(
            status_code=502,
            content={
                "error": {
                    "message": f"Upstream request failed: {type(exc).__name__}",
                    "type": "upstream_error",
                }
            },
        )


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
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    global CONFIG_PATH
    if args.config:
        CONFIG_PATH = Path(args.config).resolve()

    cfg = load_config(CONFIG_PATH)
    # The module-level state was built at import time from the default path; if
    # --config pointed elsewhere, hand the new config to the running app state.
    state.cfg = cfg
    state.catalog.base_url = cfg.upstream.base_url
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

    write_example_config(CONFIG_PATH.parent / "config.example.ini")
    log.info("starting surplus-router on http://%s:%d (admin: /admin)", host, port)
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
