"""Read-only account surface: balance and usage, proxied with the owner's key.

Upstream endpoints used (all authenticated with the buyer key from config):

- ``GET /v1/payments/balance``                 spendable / withdrawable figures
- ``GET /v1/buyer/overview?range=7d|30d``      spend, savings, top models
- ``GET /v1/requests``                         per-call log (cost, model, latency)

Rules honoured here:

- Every money value upstream is an integer-microdollar STRING. Conversion to
  USD happens exactly once, for display, in ``_with_usd_fields``.
- ``coverage: false`` on the overview means "not computed", NOT zero - it is
  passed through untouched so the UI can say "no data" instead of "$0".
- The API key is used server-side only and never appears in a response.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

log = logging.getLogger("surplus.account")

# Balance/overview are precomputed reads upstream; poll at human intervals.
ACCOUNT_CACHE_TTL_SECONDS = 30

# Balance fields that are integer-microdollar strings (docs: /v1/payments/balance).
_BALANCE_MICRO_FIELDS = (
    "credit_balance_usdc",
    "fiat_available_usdc",
    "usdc_available_usdc",
    "fiat_held_usdc",
    "usdc_held_usdc",
    "pending_deposit_usdc",
    "pending_withdrawal_usdc",
)

MICROUSD_PER_USD = 1_000_000


def _micro_to_usd(value: object) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value) / MICROUSD_PER_USD, 6)
    except (TypeError, ValueError):
        return None


def _with_usd_fields(obj: dict, fields: tuple[str, ...]) -> dict:
    """Copy of ``obj`` plus a ``*_usd`` float for every microdollar field.

    Defensive by design: fields the upstream response does not carry stay
    absent instead of crashing the panel.
    """
    out = dict(obj)
    for name in fields:
        if name in obj:
            out[name.replace("_usdc", "") + "_usd"] = _micro_to_usd(obj.get(name))
    return out


def _normalize_balance(raw: dict) -> dict:
    keep = ("account_status", "withdrawals_paused", "pending_deposit_count")
    out = {key: raw.get(key) for key in keep if key in raw}
    return _with_usd_fields({**out, **{k: raw.get(k) for k in _BALANCE_MICRO_FIELDS if k in raw}},
                            _BALANCE_MICRO_FIELDS)


def _normalize_overview(raw: dict) -> dict:
    """Pass the overview through, converting any ``*_micro`` money fields."""
    out = dict(raw)
    for key, value in list(raw.items()):
        if key.endswith("_micro"):
            out[key.replace("_micro", "_usd")] = _micro_to_usd(value)
    return out


def _normalize_requests(raw: dict) -> dict:
    """Request log page: convert per-entry microdollar costs for display."""
    out = dict(raw)
    entries = raw.get("data") or raw.get("requests") or []
    normalized = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        item = dict(entry)
        for key, value in list(entry.items()):
            if key.endswith("_micro"):
                item[key.replace("_micro", "_usd")] = _micro_to_usd(value)
        normalized.append(item)
    if normalized or entries:
        out["entries"] = normalized
    return out


class AccountReader:
    """Fetches account data with a short TTL cache; degrades to 'unavailable'."""

    def __init__(self) -> None:
        self._cache: dict | None = None
        self._cache_at: float = 0.0
        self._lock = asyncio.Lock()

    async def _get_json(
        self, client: httpx.AsyncClient, url: str, key: str, params: dict | None = None
    ) -> tuple[dict | None, str | None]:
        """(payload, None) on success, (None, reason) on any handled failure."""
        try:
            resp = await client.get(
                url, params=params, headers={"Authorization": f"Bearer {key}"}
            )
        except httpx.HTTPError as exc:
            return None, f"network_error: {type(exc).__name__}"
        if resp.status_code in (401, 403):
            return None, f"unauthorized_{resp.status_code}"
        if resp.status_code == 404:
            return None, "not_found"
        if resp.status_code >= 400:
            return None, f"upstream_{resp.status_code}"
        try:
            payload = resp.json()
        except ValueError:
            return None, "invalid_json"
        if not isinstance(payload, dict):
            return None, "unexpected_shape"
        return payload, None

    async def account(self, client: httpx.AsyncClient, base_url: str, key: str | None,
                      force: bool = False) -> dict:
        """Balance + 7d/30d overview, cached for ACCOUNT_CACHE_TTL_SECONDS."""
        if not key:
            return {"available": False, "reason": "no_api_key"}
        async with self._lock:
            fresh = (
                self._cache is not None
                and (time.time() - self._cache_at) < ACCOUNT_CACHE_TTL_SECONDS
            )
            if fresh and not force:
                return self._cache  # type: ignore[return-value]

            base = base_url.rstrip("/")
            balance_task = self._get_json(client, f"{base}/v1/payments/balance", key)
            overview_7d_task = self._get_json(
                client, f"{base}/v1/buyer/overview", key, {"range": "7d"}
            )
            overview_30d_task = self._get_json(
                client, f"{base}/v1/buyer/overview", key, {"range": "30d"}
            )
            (balance, bal_err), (ov7, ov7_err), (ov30, ov30_err) = await asyncio.gather(
                balance_task, overview_7d_task, overview_30d_task
            )

        if balance is None and ov7 is None:
            result = {
                "available": False,
                "reason": bal_err or ov7_err or "upstream_unavailable",
            }
        else:
            result = {
                "available": True,
                "reason": None,
                "balance": _normalize_balance(balance) if balance else None,
                "balance_error": None if balance else bal_err,
                "overview_7d": _normalize_overview(ov7) if ov7 else None,
                "overview_7d_error": None if ov7 else ov7_err,
                "overview_30d": _normalize_overview(ov30) if ov30 else None,
                "overview_30d_error": None if ov30 else ov30_err,
            }
        result["fetched_at"] = time.time()
        async with self._lock:
            self._cache = result
            self._cache_at = time.time()
        return result

    async def requests(self, client: httpx.AsyncClient, base_url: str, key: str | None,
                       params: dict) -> dict:
        """Pass-through of GET /v1/requests (never cached; it is the live log)."""
        if not key:
            return {"available": False, "reason": "no_api_key"}
        allowed = {}
        for name in ("limit", "cursor", "model", "provider", "statuses", "search",
                     "from", "to", "endpoint", "funding", "cache"):
            if params.get(name):
                allowed[name] = params[name]
        allowed.setdefault("limit", "50")
        payload, err = await self._get_json(
            client, f"{base_url.rstrip('/')}/v1/requests", key, allowed
        )
        if payload is None:
            return {"available": False, "reason": err or "upstream_unavailable"}
        return {"available": True, "reason": None, **_normalize_requests(payload)}


account_reader = AccountReader()
