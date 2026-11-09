# Surplus Router

A local proxy that sits between your AI agents and the Surplus Intelligence
marketplace (`api.surplusintelligence.ai`). Agents connect to the router; the
router only forwards models that pass your price filters, and keeps the catalog
prices fresh on its own.

- OpenAI-compatible: `http://127.0.0.1:8404/v1`
- Anthropic-compatible: `http://127.0.0.1:8404/anthropic` (stage 6)
- Admin panel: `http://127.0.0.1:8404/admin` — API key, filters, port, live catalog
- Bound to `127.0.0.1` only

## Install as a global command (recommended)

One command gives you `surplus` in any terminal, from any directory:

```powershell
cd D:\dev\surplus
uv tool install --editable .
uv tool update-shell        # adds %USERPROFILE%\.local\bin to PATH (once)
```

Then open a **new** terminal and just run:

```powershell
surplus                     # starts the router and opens the admin panel
surplus --port 8405         # one-off port override
surplus --no-browser        # start without opening the browser
surplus --config D:\other\config.ini
```

`--editable` means code changes in `D:\dev\surplus` take effect on the next run
with no reinstall. To pick up dependency changes, or to install a frozen copy
instead of a live one:

```powershell
uv tool install . --reinstall         # non-editable snapshot
uv tool upgrade surplus-router        # after pulling new code
uv tool uninstall surplus-router      # remove
```

Installed as a tool, the router reads/writes its config in the per-user dir:
`%APPDATA%\surplus-router\config.ini` (created with defaults on first run).

### Which config.ini is used

| precedence | source | when it applies |
|---|---|---|
| 1 | `--config PATH` | explicit, always wins |
| 2 | `SURPLUS_CONFIG` env var | set in the shell |
| 3 | `./config.ini` | a `config.ini` exists in the current directory (project-local override) |
| 4 | `%APPDATA%\surplus-router\config.ini` | everywhere else (the normal global case) |

The panel shows the file in use under **Connection → Config file**, and the
startup log prints it too — so you always know which one you are editing.

### Other install options (kept for reference)

- **pipx** (if you prefer it over uv): `pipx install --editable D:\dev\surplus`
- **PowerShell profile alias** — no install, always runs from source:
  ```powershell
  # notepad $PROFILE
  function surplus { uv run --project D:\dev\surplus python -m surplus.main @args }
  ```
- **`.bat` shim in a PATH dir** (e.g. `%LOCALAPPDATA%\Microsoft\WindowsApps`):
  ```bat
  @echo off
  uv run --project D:\dev\surplus python -m surplus.main %*
  ```
- **Auto-start at logon** — Task Scheduler, trigger "At log on", action:
  program `surplus`, arguments `--no-browser` (add `--port 8404`).
  Or NSSM/`sc.exe` if you want it as a real Windows service.
- **Single .exe for machines without Python** — PyInstaller:
  `uv run pyinstaller --onefile --add-data "surplus/static;surplus/static" -n surplus -m surplus.main`
  (heavier, and static assets must be bundled; only worth it for distribution).

## Run from source (no install)

```bash
uv sync --extra dev          # once: install fastapi, uvicorn, httpx
uv run python -m surplus.main  # start on the port from config.ini
```

The admin panel opens in your default browser as soon as the server accepts
connections (it waits for the socket, so you never get a connection-error tab).
Turn it off per run with `--no-browser`, or permanently with
`open_browser = false` under `[server]` in `config.ini` — there is also a switch
in the panel under Connection.

CLI flags override config.ini for one run (nothing is written to disk):

```bash
uv run python -m surplus.main --port 8405
uv run python -m surplus.main --host 127.0.0.1 --port 9000
uv run python -m surplus.main --config D:/other/config.ini
uv run python -m surplus.main --no-browser
```

The port is checked **before** the catalog is fetched, so a busy port fails in
about a second with exit code 2 and a message telling you how to fix it.

### "address already in use"

Another instance is still running. Stop it with `Ctrl+C` in its terminal. If the
port stays busy, find and kill the process holding it:

```powershell
# PowerShell
Get-NetTCPConnection -LocalPort 8404 | Select-Object -ExpandProperty OwningProcess
Stop-Process -Id <pid>
```

```bash
# git-bash
netstat -ano | grep :8404
MSYS_NO_PATHCONV=1 taskkill //PID <pid> //F
```

Note: `uv run` spawns a child Python process. Killing only the `uv` wrapper can
leave the server holding the port — kill the child PID shown by
`Started server process [...]`, or just use `Ctrl+C`.

First run uses built-in defaults; `config.example.ini` is written next to it.
Copy it to `config.ini` (gitignored) to change the port, filters, or API key.

```bash
cp config.example.ini config.ini
```

## Point an agent at it

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8404/v1", api_key="router")  # any placeholder
```

The router replaces the Authorization header with its own configured key, so
agents never need the real Surplus key.

## API key

Set it either way (env wins):

```bash
export SURPLUS_API_KEY="***"   # preferred, never touches disk
```

or `api_key = ...` under `[auth]` in `config.ini` (plaintext, gitignored,
masked in the admin UI).

## Filters

Caps apply to the **actual marketplace price** — what you pay — taken from
`/api/marketplace` (`best_input_per_1m` / `best_output_per_1m`, microdollars per
1M tokens). The list price from `/v1/models` is shown in the panel for reference
but is not what the caps check; marketplace discounts are commonly 95–99.9%, so
filtering on list price would let almost everything through.

Example: `qwen3.8-max-0902` lists at $2.0/$6.0 per 1M but trades at
**$0.0022/$0.0065** per 1M (99.9% discount, 233 healthy sellers).

Always enforced (no toggle, fail-closed):

| rule | meaning |
|---|---|
| `no_market_offer` | no live seller offer — the model cannot be routed at all |
| `not_token_priced` | priced per media unit (image / job / minute / 1K characters) or unpriced — token caps are meaningless |
| `unknown_model` | id absent from the catalog (unless `allow_unknown_models = true`) |

Toggleable, each with its own switch:

| filter | keys | rule |
|---|---|---|
| input price cap | `enable_price_input`, `max_input_per_1m` | market `$/1M <= cap` (inclusive) |
| output price cap | `enable_price_output`, `max_output_per_1m` | market `$/1M <= cap` (inclusive) |
| min discount | `enable_discount`, `min_discount_pct` | local check on `best_live_discount_pct` + upstream `/min{N}/v1/...` rewrite |
| allow models | `enable_models_allow`, `allow_models` | id must be listed |
| deny models | `enable_models_deny`, `deny_models` | wins over allow |
| allow providers | `enable_providers_allow`, `allow_providers` | provider must be listed |
| min context | `enable_context_min`, `min_context_length` | `context_length >= min` |

A rejected request returns `403` with `failed_filters` naming every rule that
refused it, plus the market and list prices in the message, and never reaches
upstream.

## Price freshness

The catalog (`/v1/models` + `/v1/prices` + `/api/marketplace`) is refreshed at
startup, every `catalog_ttl_seconds` (default 300), and on demand via
`POST /admin/api/refresh`. A failed refresh keeps serving the last-good copy and
flags `stale` in `/admin/api/status`. An empty marketplace response is treated as
a failed refresh, never as "everything is free". Requests never block on a
refresh.

## Verify

```bash
uv run python scripts/verify_local.py   # boots the router on port 8499, 13 checks
uv run python scripts/verify_admin.py   # admin panel + config API, 27 checks
uv run pytest -q                         # 48 unit tests
uv run ruff check surplus scripts tests
```

## Layout

```
surplus/config.py    config.ini load/save, atomic write, env key override, masking
surplus/catalog.py   TTL cache of upstream models + reference prices
surplus/filters.py   pure filter evaluation (unit-testable)
surplus/proxy.py     streaming passthrough, header handling
surplus/main.py      FastAPI app: admin API, gate, /v1 routes, refresh loop
surplus/static/      admin panel (index.html, app.js, style.css) - no build step
scripts/verify_local.py  end-to-end local verification against the live catalog
scripts/verify_admin.py  admin panel + config API verification over HTTP
```

See `SPEC.md` for the full design and `AGENTS.md` for project rules.
