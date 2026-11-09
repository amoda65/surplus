"""Catalog cache: fetch upstream model and price data, keep the last-good copy.

A failed refresh never wipes existing data; the caller marks the cache stale.
Nothing here blocks request handling - the gate always reads the cache.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from .filters import ModelInfo

log = logging.getLogger("surplus.catalog")


def _per_token_to_per_1m(value: object) -> float:
    try:
        return float(value or 0) * 1_000_000
    except (TypeError, ValueError):
        return 0.0


def _reference_from_prices(prices_payload: dict) -> dict[str, dict[str, float | None]]:
    """Build {model_id: {input, output}} reference list prices per 1M tokens.

    /v1/prices lists every provider's price for a model. The highest provider
    price is used as the reference (list) price, which is the conservative
    choice for estimating a discount.
    """
    references: dict[str, dict[str, float | None]] = {}
    for entry in prices_payload.get("models") or []:
        model_id = entry.get("model")
        if not model_id:
            continue
        best_in: float | None = None
        best_out: float | None = None
        for provider in entry.get("providers") or []:
            pricing = provider.get("pricing") or {}
            price_in = pricing.get("input")
            price_out = pricing.get("output")
            if isinstance(price_in, (int, float)):
                best_in = float(price_in) if best_in is None else max(best_in, float(price_in))
            if isinstance(price_out, (int, float)):
                best_out = float(price_out) if best_out is None else max(best_out, float(price_out))
        references[str(model_id)] = {"input": best_in, "output": best_out}
    return references


class Catalog:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.models: dict[str, ModelInfo] = {}
        self.providers: list[str] = []
        self.last_fetch_at: float | None = None
        self.upstream_updated_at: str | None = None
        self.last_error: str | None = None
        self.refresh_count = 0

    @property
    def loaded(self) -> bool:
        return bool(self.models)

    def is_stale(self, ttl_seconds: int) -> bool:
        """Stale = never fetched, or older than 2x the refresh interval."""
        if self.last_fetch_at is None:
            return True
        return (time.time() - self.last_fetch_at) > (ttl_seconds * 2)

    async def refresh(self, client: httpx.AsyncClient) -> bool:
        """Fetch /v1/models and /v1/prices. Returns True on success."""
        try:
            models_resp, prices_resp = await asyncio.gather(
                client.get(f"{self.base_url}/v1/models"),
                client.get(f"{self.base_url}/v1/prices"),
            )
            models_resp.raise_for_status()
            prices_resp.raise_for_status()
        except httpx.HTTPError as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("catalog refresh failed, keeping last-good copy: %s", self.last_error)
            return False

        try:
            models_payload = models_resp.json()
            prices_payload = prices_resp.json()
        except ValueError as exc:
            self.last_error = f"InvalidJSON: {exc}"
            log.warning("catalog refresh got invalid JSON: %s", self.last_error)
            return False

        entries = models_payload.get("data") or []
        if not entries:
            self.last_error = "EmptyCatalog: upstream returned no models"
            log.warning(self.last_error)
            return False

        references = _reference_from_prices(prices_payload)
        parsed: dict[str, ModelInfo] = {}
        for entry in entries:
            model_id = str(entry.get("id") or "")
            if not model_id:
                continue
            parsed[model_id] = ModelInfo.from_catalog_entry(entry, references.get(model_id))

        self.models = parsed
        self.providers = sorted(
            {str(p) for p in (prices_payload.get("providers") or []) if isinstance(p, str)}
        )
        self.upstream_updated_at = prices_payload.get("updated_at")
        self.last_fetch_at = time.time()
        self.last_error = None
        self.refresh_count += 1
        log.info(
            "catalog refreshed: %d models, %d with reference prices",
            len(parsed),
            len(references),
        )
        return True

    def snapshot(self, ttl_seconds: int) -> dict:
        """Admin-facing cache state. Contains no secrets."""
        return {
            "model_count": len(self.models),
            "providers": self.providers,
            "last_fetch_at": self.last_fetch_at,
            "last_fetch_iso": (
                time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.last_fetch_at))
                if self.last_fetch_at
                else None
            ),
            "age_seconds": (
                round(time.time() - self.last_fetch_at, 1) if self.last_fetch_at else None
            ),
            "upstream_updated_at": self.upstream_updated_at,
            "stale": self.is_stale(ttl_seconds),
            "last_error": self.last_error,
            "refresh_count": self.refresh_count,
        }

    def priced_summary(self) -> dict:
        zero = sum(
            1 for m in self.models.values() if m.input_per_1m == 0 and m.output_per_1m == 0
        )
        return {
            "total": len(self.models),
            "zero_priced": zero,
            "priced": len(self.models) - zero,
        }
