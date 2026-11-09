# Surplus Router — Spec

## Goal

A local proxy so AI agents (OpenAI SDK and Anthropic SDK / Claude Code) reach the
Surplus Intelligence marketplace **only through price-filtered models**, with a
web control panel for the API key, filters and port.

```
                 ┌── control plane ──┐
 browser ──────► │ /admin  (web UI)  │──► config.ini (key, filters, port)
                 └───────────────────┘
 agent ──► http://127.0.0.1:{port}/v1 ─────────► filter gate ──► api.surplusintelligence.ai
 agent ──► http://127.0.0.1:{port}/anthropic ────────┘
                                                │
                                      reject 4xx (never forwarded)
```

One process, one port (default `8404`, bound to `127.0.0.1` only). The admin UI
is served by the same app at `/admin`.

## Decisions (owner-approved)

| topic | decision |
|---|---|
| admin UI location | same app/port, `/admin`, localhost bind only |
| API key at rest | plaintext in gitignored `config.ini`; masked in UI; env `SURPLUS_API_KEY` overrides file |
| UI language | English (matches the English-only code rule) |
| filters | every filter type has its own enable toggle; values apply only while enabled |
| price freshness | background refresh on TTL + refresh at startup + manual **Refresh now** button |
| price cap unit | per 1M tokens |
| zero-priced models | pass (0 is below any cap) |
| discount filter | delegated upstream via `/min{N}/v1/...` path rewrite |
| unknown model | reject (fail-closed) unless `allow_unknown_models = true` |

## Upstream API facts (verified 2026-11-09, live probes)

| endpoint | auth | shape |
|---|---|---|
| `GET /v1/models` | none | `{data: [{id, name, provider, context_length, architecture:{input_modalities, output_modalities}, pricing:{prompt, completion, input_cache_read, input_cache_write}, supported_features}]}` — 524 models; pricing is **per token as decimal strings** |
| `GET /v1/prices` | none | `{models: [{model, displayName, modelProvider, providers: [{provider, providerName, pricing:{input, output, cacheRead}}]}], providers: [...], updated_at}` — pricing **per 1M tokens**; reference list prices for discount math |
| `POST /v1/chat/completions` | Bearer key | OpenAI-compatible, SSE streaming |
| `/anthropic/*` | Bearer key | Anthropic Messages skin (Claude Code, Anthropic SDK) |
| `/min{N}/v1/...` | Bearer key | upstream minimum-discount routing; N integer 0..100, no leading zeros or decimals |

Snapshot at probe time: 293 of 524 models priced 0.00; priced range input ≤
$30/1M, output ≤ $180/1M; 18 reference providers.

## Filter types (each individually toggleable in the UI)

| id | filter | config keys |
|---|---|---|
| `price_input` | input price cap, per 1M | `max_input_per_1m` |
| `price_output` | output price cap, per 1M | `max_output_per_1m` |
| `discount` | minimum discount % below list price | `min_discount_pct` |
| `models_allow` | allow list of model ids | `allow_models` |
| `models_deny` | deny list of model ids (wins over allow) | `deny_models` |
| `providers_allow` | allow list of providers | `allow_providers` |
| `context_min` | minimum context length | `min_context_length` |

Rules:
- Price comparison: `price_per_token * 1e6 < cap` (strictly below); price 0 passes.
- A disabled filter contributes no constraint.
- `discount` maps to the upstream `/min{N}` rewrite; when `/v1/prices` carries a
  reference price for the model, the catalog view also shows the estimated
  discount so the UI can flag models that would fail upstream.
- Enforcement points:
  - `GET /v1/models` (OpenAI + Anthropic model list) returns **only passing models**.
  - Every completion/messages request: extract `model` from the body, run the gate,
    and reject locally with `403`
    `{"error":{"message":...,"type":"model_filtered_out","model":...,"failed_filters":[...]}}`
    **before** any upstream call. `failed_filters` names each filter that rejected.
  - Models absent from the catalog: reject unless `allow_unknown_models = true`.

## Price freshness (owner requirement)

- A background task refreshes the catalog (`/v1/models` + `/v1/prices`) every
  `catalog_ttl_seconds` (default **300**), once at startup, and on demand via
  `POST /admin/api/refresh` (the UI **Refresh now** button).
- The last-good copy stays in memory: a failed refresh keeps the proxy serving
  previous data while the UI shows a **stale** badge with the last successful
  fetch time and upstream `updated_at`.
- No request ever blocks on a refresh; the gate reads the cached catalog.
- UI shows counts: total / passing / filtered out.

## Control panel (`/admin`, static HTML + vanilla JS, English, no build step)

1. **Connection** — bind host, proxy port, upstream base URL, catalog TTL.
   Port changes need a restart; the UI says so explicitly.
2. **API key** — masked input; save writes `config.ini`; status shows
   `set (env)`, `set (file)` or `missing`. Never echoed back in full.
3. **Filters** — one card per filter type with an enable toggle and inputs, plus
   a live preview of how many cached models pass.
4. **Catalog** — refresh button, last refresh time, staleness badge, searchable
   table: model, provider, input/output price per 1M, context, pass/fail + reason.
5. **Connect your agent** — copy-paste snippets: OpenAI SDK `base_url`,
   Claude Code env vars, generic OpenAI-compatible client.

Admin API (JSON, localhost only): `GET/POST /admin/api/config`,
`POST /admin/api/refresh`, `GET /admin/api/models` (annotated pass/fail reasons),
`GET /admin/api/status`.

## Config — `config.ini` (gitignored), `config.example.ini` (committed)

Location is resolved at startup, in this order:

1. `--config PATH`
2. `SURPLUS_CONFIG` environment variable
3. `./config.ini` in the current working directory, if it exists (project-local override)
4. `%APPDATA%\surplus-router\config.ini` (Windows) / `~/.config/surplus-router/config.ini`

The file is created from defaults on first run, and the resolved path is printed
in the startup log and shown in the admin panel under Connection, so it is always
clear which file is in effect.

```ini
[server]
host = 127.0.0.1
port = 8404
open_browser = true          ; open /admin in the default browser at startup

[upstream]
base_url = https://api.surplusintelligence.ai
catalog_ttl_seconds = 300        ; refresh interval for /v1/models + /v1/prices
request_timeout_seconds = 600    ; long for streaming completions

[auth]
api_key_env = SURPLUS_API_KEY
; api_key = ...        ; written by the admin UI; env value wins when set

[filter]
enable_price_input = true
max_input_per_1m = 2.0
enable_price_output = true
max_output_per_1m = 10.0
enable_discount = false
min_discount_pct = 30
enable_models_allow = false
allow_models =
enable_models_deny = false
deny_models =
enable_providers_allow = false
allow_providers =
enable_context_min = false
min_context_length = 32000
allow_unknown_models = false
```

UI writes are atomic (temp file → replace) so a crash cannot truncate
`config.ini`. Saving filters reloads them in-process — no proxy restart. Only a
port/host change requires a restart.

## Architecture (flat, single package)

```
surplus/
  main.py        FastAPI app: admin API, proxy routes, static UI, refresh task
  catalog.py     fetch + TTL cache of /v1/models and /v1/prices; per-1M pricing
  filters.py     pure functions: filter defs + evaluate(model) -> pass/fail reasons
  proxy.py       httpx streaming passthrough (OpenAI + Anthropic paths)
  config.py      config.ini load/save (stdlib configparser), env override, atomic write
  static/
    index.html   admin panel
    app.js
    style.css
  config.example.ini
tests/
  test_filters.py    filter unit tests against fixture from live probe
  test_config.py     config round-trip, atomic write, env override
```

Deps (uv): `fastapi`, `uvicorn`, `httpx`. Dev: `pytest`, `ruff`. No database, no
JS build toolchain.

## Stages (each verified before the next starts)

1. ✅ Repo rules, git, workspace
2. ✅ Live API probe + filter decisions
3. ✅ **Base proxy**: config loader, catalog loader with TTL cache, filtered
   `GET /v1/models`, `POST /v1/chat/completions` streaming passthrough
   (13/13 local checks green against the live catalog)
4. ✅ **Filter engine**: all 7 filter types + toggles, `failed_filters` errors,
   `/min{N}` rewrite, 48 unit tests green
5. ✅ **Admin panel**: config API, filter cards with toggles, masked key entry,
   catalog table (search/sort/pass-fail reasons), refresh + staleness badge,
   restart warning, agent snippets — 28/28 admin checks green, UI driven live
5b. ✅ **Global install + startup ergonomics**: `surplus` console script via
   `uv tool install --editable .`, busy-port fail-fast, `--host/--port/--config/
   --no-browser`, browser auto-open once the socket accepts connections, and
   config resolution that works from any directory (`%APPDATA%\surplus-router`)
6. **Anthropic skin**: `/anthropic/*` passthrough + filtered model list
7. **End-to-end verification**: OpenAI SDK and Claude Code through the proxy;
   rejection test for a capped-out model; streaming test; refresh test
   (needs a real API key from the owner)

## Acceptance criteria (final)

- `/admin` sets the API key, port and every filter type with toggles, and shows
  live pass/fail counts from the cached catalog.
- OpenAI SDK pointed at `http://127.0.0.1:{port}/v1` lists only passing models.
- A chat completion for a passing model streams back correctly.
- A completion naming a capped-out model returns local `403` with
  `failed_filters` and never reaches upstream.
- Claude Code / Anthropic SDK works through `/anthropic`.
- Prices refresh on TTL, at startup, and on demand; a failed refresh keeps the
  last-good catalog and flags staleness.
- No API key appears in code, logs, responses, or git history.
