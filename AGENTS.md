# Surplus Router — Project Rules

## What this is

A local proxy (localhost) that sits between AI agents and the Surplus Intelligence
marketplace (`https://api.surplusintelligence.ai`). Agents connect to the proxy
with OpenAI-compatible or Anthropic-compatible protocols. The proxy:

1. Serves a **filtered** model list (`GET /v1/models`, Anthropic equivalent).
2. Enforces price filters on every request **before** forwarding.
3. Forwards passing requests to Surplus with streaming passthrough.
4. Maps the discount filter onto Surplus' built-in `/min{N}` routing prefix.

## Non-negotiable rules

- **No secrets in code or commits.** API key comes from environment/config file
  (`config.ini`), which is gitignored. A `config.example.ini` is committed.
- **Code, comments, CLI output: English only.**
- **Staged delivery:** one capability at a time; each stage verified against the
  real API before the next stage starts. Do not build ahead of the agreed plan.
- **Simple, flat design.** No database. No heavy frameworks. In-memory cache
  with TTL for model/price data.
- **Avoid unstable third-party deps.** Core deps: fastapi, uvicorn, httpx.
- **Verify against the live API**, never against assumed response shapes.
  Probe first, code second.

## Filter semantics (agreed with owner)

- Primary filter: **per-1M-token price caps** — e.g. `max_input_per_1m = 0.02`,
  `max_output_per_1m = 0.1`. A model passes only if BOTH caps are satisfied.
- The upstream `/v1/models` returns per-token pricing (`pricing.prompt`,
  `pricing.completion`); multiply by 1e6 to compare against the caps.
- Secondary filter: minimum discount percent, delegated to upstream via the
  `/min{N}/v1/...` path prefix rather than computed locally when possible.
- Optional: model allow/deny lists.
- A request whose model fails the filter must be rejected locally (4xx with a
  clear JSON error) — never forwarded.

## Protocols

- OpenAI-compatible: `/v1/chat/completions`, `/v1/models` (+ other `/v1/*` passthrough).
- Anthropic-compatible: `/anthropic` skin passthrough (`/anthropic/v1/messages`,
  model list) so Claude Code / Anthropic SDK work.
- Streaming (SSE) must pass through unbuffered.

## Verification discipline

Every stage ends with an executable check (curl/probe script) run against the
live API, with the real output shown — no fabricated examples.
