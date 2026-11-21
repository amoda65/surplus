# Surplus Router

**English** · [فارسی](README.fa.md)

![CI](https://github.com/ZaniarManochehri/surplus/actions/workflows/ci.yml/badge.svg)
![License: MIT](https://img.shields.io/badge/license-MIT-blue)
![Python](https://img.shields.io/badge/python-3.11%2B-green)

A tiny local proxy that sits between your AI agents and the
[Surplus Intelligence](https://www.surplusintelligence.ai) marketplace — a
decentralized exchange for LLM inference where the same model can trade at
95–99.9% below list price.

Point your agents at the router instead of the marketplace. It shows them
**only the models that pass your filters**, blocks everything else locally
before it can cost you a cent, and keeps prices fresh on its own.

```
your agent ──► http://127.0.0.1:8404/v1 ──► filter gate ──► api.surplusintelligence.ai
                                                  │
                                        reject 403 (never forwarded)
```

## Why

- Marketplace prices move constantly. One model can have 300+ sellers with
  wildly different prices, uptime, and speed.
- The catalog list price is **not** what you pay — actual market offers run up
  to ~1000× cheaper. Filtering on the wrong number is useless.
- Some "cheap" offers come from untrusted providers, thin markets that vanish
  mid-job, or capped liquidity that runs dry.
- You want one knob: *"route my agents only to models that are cheap **and**
  reliable **and** capable"* — enforced locally, fail-closed.

## Features

- **OpenAI-compatible** endpoint at `/v1` — works with the OpenAI SDK, Cursor,
  Aider, Continue, OpenCode, and anything that speaks `/v1/chat/completions`
  (streaming passes through unbuffered)
- **Filters on the real market price** (what you actually pay), with inclusive
  caps in USD per 1M tokens
- **16 filter rules**, each with its own toggle: price caps, cache-write cap,
  trusted-providers-only mode, minimum discount, uptime / TTFT / TPS quality
  gates, seller & liquidity depth, required features (tools, streaming…),
  minimum context, allow/deny lists, tightening-trend exclusion
- **Admin panel** at `/admin` — API key, filters, port, live catalog table with
  sorting/filtering, a per-model detail drawer (per-provider offers, quality
  samples, discount trend), and your **account balance + usage log** read live
  from Surplus
- **Always-fresh prices**: background refresh on a TTL, manual refresh button,
  stale-data badge; a failed refresh never wipes the last good catalog
- **Fail-closed**: unknown models, models with no live offer, and media-priced
  models are rejected locally with a `403` naming every rule that refused them
- Zero database, zero JS build step, three runtime deps (fastapi, uvicorn,
  httpx); your API key never leaves your machine

## Install

The router is a Python package; [uv](https://docs.astral.sh/uv/) is the easiest
way to install it as a global command.

```bash
# 1) get uv (once) — Windows:
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
# macOS/Linux:
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2) install the router straight from GitHub:
uv tool install "git+https://github.com/ZaniarManochehri/surplus.git"

# 3) run it from anywhere:
surplus
```

That's it. The router starts on `http://127.0.0.1:8404` and opens the admin
panel in your browser as soon as it's ready.

Alternatives:

```bash
pipx install "git+https://github.com/ZaniarManochehri/surplus.git"
pip  install "git+https://github.com/ZaniarManochehri/surplus.git"

# or from source
git clone https://github.com/ZaniarManochehri/surplus.git
cd surplus
uv sync && uv run python -m surplus.main
```

Useful flags:

```bash
surplus --port 8405        # one-off port override
surplus --no-browser       # don't open the admin panel
surplus --config path.ini  # use a specific config file
```

## Quick start

1. Run `surplus`. The admin panel opens at `http://127.0.0.1:8404/admin`.
2. Paste your Surplus API key under **API key** (get one at
   [surplusintelligence.ai](https://www.surplusintelligence.ai/app)). It is
   stored in a gitignored config file on your machine, masked everywhere, and
   used server-side only.
3. Set your filters (defaults: pay at most **$0.10 / 1M input** and
   **$0.50 / 1M output** at real market prices).
4. Point your agent at the router:

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8404/v1", api_key="***")  # any placeholder

models = client.models.list()          # only models passing your filters
resp = client.chat.completions.create(
    model=models.data[0].id,
    messages=[{"role": "user", "content": "hello"}],
    stream=True,
)
```

Or with environment variables (Aider, Continue, OpenCode, …):

```bash
OPENAI_API_BASE=http://127.0.0.1:8404/v1
OPENAI_API_KEY=***
```

The router replaces the Authorization header with your real key, so agents
never see it.

## Filters

Every filter has its own enable switch; a model is forwarded only if it passes
every enabled filter. Caps apply to the **actual market price** from the live
order book (microdollar-precise), not the list price.

| group | filters |
|---|---|
| Price | input cap, output cap, cache-write cap, trusted-providers-only |
| Discount | minimum discount % (delegated upstream via `/min{N}` routing too), exclude tightening trends |
| Quality | min uptime %, max TTFT (ms), min TPS — unmeasured markets pass (fail-open) |
| Depth | min healthy sellers, min uncapped offers |
| Capability | min context length, required features (e.g. `streaming, tools`) |
| Lists | model allow list, model deny list, provider allow list |

Always enforced (fail-closed): no live offer → `no_market_offer`; media-unit
pricing (image/video/audio jobs) → `not_token_priced`; unknown model →
`unknown_model`. A rejected request gets a local `403` whose
`failed_filters` names every rule that refused it — it never reaches upstream.

## Account panel

With an API key set, the panel also shows your live Surplus account
(read-only): spendable credits (fiat + withdrawable USDC), 7d/30d spend
overviews, and the per-request log (model, provider, status, latency, cost).

## Proxy tunnel (censored networks)

On networks where `api.surplusintelligence.ai` is unreachable, the router can
push **only its own upstream traffic** through a proxy. Nothing else on the
machine is routed, so a corporate VPN, git remotes and internal services keep
working exactly as before — no TUN device, no system proxy, no VPN app.

```
agents ──► 127.0.0.1:8404 ──► [proxy tunnel] ──► api.surplusintelligence.ai
                    everything else on the machine: untouched
```

Two modes, configured in the admin panel (**Proxy tunnel** card) or in
`config.ini` under `[proxy]`:

- **Managed Xray (recommended)** — paste your V2Ray subscription link once.
  The router downloads Xray-core itself (latest release, from GitHub), builds
  a loopback-only client config from the subscription, and runs Xray as a
  child process. Every ~30s it probes Surplus *through* the tunnel; after two
  consecutive failures it rotates to the next subscribed route automatically,
  and re-fetches the subscription every 6 hours. A dead tunnel is never
  silent: the admin panel shows a colored state badge and the exact last
  error, and `/v1` requests return a `proxy_tunnel_down` JSON error instead of
  a bare timeout.
- **External proxy** — point at a VPN client running in proxy-only mode
  (v2rayN/Nekobox/Hiddify with TUN and system proxy off), e.g.
  `proxy_url = http://127.0.0.1:10809` or `socks5://127.0.0.1:10808`.

Enable/disable is a button in the admin panel and applies live — no restart.
When the tunnel is disabled or fully down, the router falls back to a direct
connection and keeps reporting why the tunnel is not up.

The subscription URL is stored only in `config.ini` (gitignored) and is shown
masked in the panel, exactly like the API key. If your subscription panel is
itself unreachable, paste raw share links (`vless://…`, one per line) into
`subscription_links` instead.

Verified end-to-end by `scripts/verify_proxy.py` (subscription link supplied
via the `VERIFY_SUBSCRIPTION_URL` environment variable, never committed):

```
uv run python scripts/verify_proxy.py
```

## Configuration

`config.ini` is resolved in this order: `--config` → `SURPLUS_CONFIG` →
`./config.ini` → `%APPDATA%\surplus-router\config.ini` (Windows) /
`~/.config/surplus-router/config.ini`. It is created from defaults on first
run; see [`config.example.ini`](config.example.ini) for every key. The panel
writes it atomically, and edits on disk are hot-reloaded without a restart
(only host/port changes need one).

The API key can also come from the environment (`SURPLUS_API_KEY`), which wins
over the file and never touches disk.

## Troubleshooting

**`address already in use`** — another instance is running. Close it
(`Ctrl+C`), or find the process: `netstat -ano | findstr :8404` (Windows) /
`lsof -i :8404`, then kill that PID. The router checks the port *before*
doing any work and exits with a clear message.

**Everything is blocked** — check the caps: market prices are per **1M
tokens**; `$0.10/1M` is genuinely cheap. The catalog table shows exactly why
each model failed, and the detail drawer shows the live offers.

## Development

```bash
git clone https://github.com/ZaniarManochehri/surplus.git && cd surplus
uv sync --extra dev
uv run pytest -q                        # unit tests (offline, fixture-based)
uv run ruff check surplus scripts tests
uv run python scripts/verify_local.py   # end-to-end checks against the live API
uv run python scripts/verify_admin.py   # admin panel checks against the live API
```

Design notes live in [`SPEC.md`](SPEC.md); project conventions in
[`AGENTS.md`](AGENTS.md).

Anthropic-compatible endpoint (`/anthropic` for Claude Code / Anthropic SDK)
is on the roadmap.

## License

[MIT](LICENSE)
