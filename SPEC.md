# Surplus Router — Spec

## Goal

A local proxy so AI agents (OpenAI SDK and Anthropic SDK / Claude Code) reach the
Surplus Intelligence marketplace **only through price-filtered models**.

```
agent ──► http://127.0.0.1:8404  ──► filter gate ──► https://api.surplusintelligence.ai
                                        │
                                   reject 4xx (never forwarded)
```

## Upstream API facts (verified 2026-11-09, live probes)

| endpoint | auth | shape |
|---|---|---|
| `GET /v1/models` | none | `{data: [{id, name, pricing:{prompt, completion, input_cache_read, input_cache_write}, provider, ...}]}` — 524 models, pricing is **per token as decimal strings** |
| `GET /v1/prices` | none | `{models: [{model, displayName, modelProvider, providers: [{provider, pricing:{input, output, cacheRead}}]}], providers: [...], updated_at}` — pricing **per 1M tokens**; reference list prices used for discount math |
| `POST /v1/chat/completions` | Bearer key | OpenAI-compatible, SSE streaming |
| `/anthropic/*` | Bearer key | Anthropic Messages skin (Claude Code, Anthropic SDK) |
| `/min{N}/v1/...` | Bearer key | upstream minimum-discount routing (N = 0..100, no leading zeros/decimals) |

Live data snapshot: 293 of 524 models priced 0.00; priced range input ≤ $30/1M,
output ≤ $180/1M.

## Filter rules (owner-approved)

1. **Price caps, per 1M tokens** — primary filter.
   `max_input_per_1m` and `max_output_per_1m`; model passes iff both hold
   (`price_per_token * 1e6 <= cap`). Caps are inclusive-max via `<` semantics:
   price strictly below the cap passes.
2. **Zero-priced models pass** (free/no-active-offer treated as cheapest).
3. **Minimum discount percent** — secondary; delegated upstream by rewriting the
   forwarded path to `/min{N}/v1/...`. N validated as integer 0..100 locally;
   invalid config falls back to no prefix.
4. **Allow/deny lists** — optional exact model ids; deny wins over allow.
5. Enforcement points:
   - `GET /v1/models` (and Anthropic model list): serve **filtered** list only.
   - Every chat/completions or messages request: extract `model` from the JSON
     body, check against the filter, reject with `403` + JSON error
     `{"error": {"message": "...", "type": "model_filtered_out", "model": "..."}}`
     before any upstream call.
6. Models absent from the catalog (unknown id): **reject** (fail-closed) unless
   `allow_unknown_models = true` in config.

## Config — `config.ini` (gitignored), `config.example.ini` (committed)

```ini
[server]
host = 127.0.0.1
port = 8404

[upstream]
base_url = https://api.surplusintelligence.ai
catalog_ttl_seconds = 300        ; refresh interval for /v1/models + /v1/prices
request_timeout_seconds = 600    ; long for streaming completions

[auth]
; API key is read from env SURPLUS_API_KEY first; this is only a fallback.
api_key_env = SURPLUS_API_KEY

[filter]
max_input_per_1m = 2.0
max_output_per_1m = 10.0
min_discount_pct = 0             ; 0 = disabled; else rewritten to /min{N}/v1
allow_unknown_models = false
allow_models =                   ; comma-separated ids, empty = all allowed
deny_models =                    ; comma-separated ids, always wins
```

## Architecture (flat, single package)

```
surplus/
  main.py        FastAPI app: routes, startup catalog loader
  catalog.py     fetch + TTL cache of /v1/models and /v1/prices; per-1M pricing
  filters.py     pure functions: model passes caps/discount/lists (unit-testable)
  proxy.py       httpx streaming passthrough for OpenAI + Anthropic paths
  config.py      config.ini loader (stdlib configparser), env override for key
  config.example.ini
tests/
  test_filters.py    filter unit tests against fixture from live probe
```

Deps (uv): `fastapi`, `uvicorn`, `httpx`. Dev: `pytest`, `ruff`. No database.

## Stages (each verified before the next starts)

1. ✅ Repo rules, git, workspace
2. ✅ Live API probe, spec (this file)
3. **Base proxy**: catalog loader + filtered `/v1/models` + chat completions
   passthrough with streaming (OpenAI path only), config + env key
4. **Filter engine complete**: price caps, deny/allow, unknown-model policy,
   `/min{N}` rewrite, unit tests
5. **Anthropic skin**: `/anthropic/*` passthrough + model list
6. **End-to-end verification**: real request from an agent SDK through the
   proxy; filtered-model rejection test; streaming test

## Acceptance criteria (stage 6)

- OpenAI SDK pointed at `http://127.0.0.1:8404/v1` lists only passing models.
- A chat completion for a passing model streams back correctly.
- A chat completion naming a capped-out model returns local 403, no upstream hit.
- Claude Code / Anthropic SDK works through `/anthropic`.
- No API key appears in code, logs, or git history.
