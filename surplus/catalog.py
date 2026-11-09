"""Catalog cache: fetch upstream model, reference-price and market data.

Three public endpoints are joined per model:

- ``GET /v1/models``   catalog + list prices (per-token decimal strings)
- ``GET /v1/prices``   provider reference prices (USD per 1M tokens)
- ``GET /api/markets`` the actual best live offer per model, in microdollars
                       per 1M tokens, plus quality (uptime/TTFT/TPS), liquidity,
                       discount trend and per-provider offers with trust flags.
                       ``GET /api/marketplace`` is kept as a fallback: it carries
                       the same core price fields in a ``data``-wrapped envelope.

A failed refresh never wipes existing data; the caller marks the cache stale.
Nothing here blocks request handling - the gate always reads the cache.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from .filters import (
    CLASS_MEDIA,
    CLASS_NO_OFFER,
    CLASS_TOKEN,
    CLASS_UNPRICED,
    ModelInfo,
)

log = logging.getLogger("surplus.catalog")

# Tried in order; the first endpoint that returns market rows wins.
MARKET_ENDPOINTS = ("/api/markets", "/api/marketplace")


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


def _market_rows(payload: dict) -> list[dict]:
    """Market rows from either envelope: /api/markets or /api/marketplace."""
    if not isinstance(payload, dict):
        return []
    rows = payload.get("markets")
    if rows is None:
        rows = (payload.get("data") or {}).get("markets")
    return rows if isinstance(rows, list) else []


def _market_from_payload(marketplace_payload: dict) -> dict[str, dict]:
    """Index live market offers by model id, keeping microdollar values.

    Unit conversion to USD happens in filters.ModelInfo so this stays a faithful
    copy of what upstream returned. Works for both /api/markets and the older
    /api/marketplace envelope; fields the older endpoint lacks stay None.
    """
    markets: dict[str, dict] = {}
    for entry in _market_rows(marketplace_payload):
        model_id = entry.get("model")
        if not model_id:
            continue
        markets[str(model_id)] = {
            "input_micro_per_1m": entry.get("best_input_per_1m"),
            "output_micro_per_1m": entry.get("best_output_per_1m"),
            "media_unit": entry.get("media_unit"),
            "media_unit_price_micro": entry.get("best_media_unit_price"),
            "direct_input_micro_per_1m": entry.get("direct_input_per_1m"),
            "direct_output_micro_per_1m": entry.get("direct_output_per_1m"),
            "best_live_discount_pct": entry.get("best_live_discount_pct"),
            "best_discount_pct": entry.get("best_discount_pct"),
            "best_trusted_offer_discount_pct": entry.get("best_trusted_offer_discount_pct"),
            "healthy_seller_count": entry.get("healthy_seller_count"),
            "num_sellers": entry.get("num_sellers"),
            "uncapped_offer_count": entry.get("uncapped_discounted_offer_count"),
            "liquidity_micro_usd": entry.get("discounted_liquidity_usd"),
            "cache_read_micro_per_1m": entry.get("best_cache_read_per_1m"),
            "cache_write_micro_per_1m": entry.get("best_cache_write_per_1m"),
            "cache_hit_rate_pct": entry.get("cache_hit_rate_pct"),
            "uptime_pct": entry.get("uptime_pct"),
            "ttft_p50_ms": entry.get("ttft_p50_ms"),
            "tps_p50": entry.get("tps_p50"),
            "tps_samples": entry.get("tps_samples"),
            "ttft_samples": entry.get("ttft_samples"),
            "requests_24h": entry.get("requests_24h"),
            "volume_24h_micro_usd": entry.get("volume_24h"),
            "discount_trend": entry.get("discount_trend"),
            "providers": entry.get("providers"),
        }
    return markets


class Catalog:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.models: dict[str, ModelInfo] = {}
        self.providers: list[str] = []
        self.last_fetch_at: float | None = None
        self.upstream_updated_at: str | None = None
        self.market_computed_at: int | None = None
        self.market_count = 0
        self.market_source: str | None = None
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

    async def _fetch_markets(self, client: httpx.AsyncClient) -> tuple[str, dict] | None:
        """First market endpoint that answers with rows; None when all fail."""
        for path in MARKET_ENDPOINTS:
            try:
                resp = await client.get(f"{self.base_url}{path}")
                resp.raise_for_status()
                payload = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("market endpoint %s failed: %s", path, exc)
                continue
            if _market_rows(payload):
                return path, payload
            log.warning("market endpoint %s returned no rows", path)
        return None

    async def refresh(self, client: httpx.AsyncClient) -> bool:
        """Fetch catalog, reference prices and live markets. True on success."""
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

        fetched = await self._fetch_markets(client)
        if fetched is None:
            # Without offers we cannot price anything; refuse to publish a catalog
            # that would reject or accept every model for the wrong reason.
            self.last_error = "EmptyMarketplace: upstream returned no market offers"
            log.warning(self.last_error)
            return False
        market_path, market_payload = fetched

        references = _reference_from_prices(prices_payload)
        markets = _market_from_payload(market_payload)

        parsed: dict[str, ModelInfo] = {}
        for entry in entries:
            model_id = str(entry.get("id") or "")
            if not model_id:
                continue
            parsed[model_id] = ModelInfo.from_catalog_entry(
                entry, references.get(model_id), markets.get(model_id)
            )

        self.models = parsed
        self.providers = sorted(
            {str(p) for p in (prices_payload.get("providers") or []) if isinstance(p, str)}
        )
        self.upstream_updated_at = prices_payload.get("updated_at")
        self.market_computed_at = (market_payload.get("data") or {}).get("computed_at")
        self.market_count = len(markets)
        self.market_source = market_path
        self.last_fetch_at = time.time()
        self.last_error = None
        self.refresh_count += 1
        summary = self.class_summary()
        log.info(
            "catalog refreshed from %s: %d models, %d with live offers "
            "(%d token-priced, %d media-unit, %d unpriced, %d without offer)",
            market_path,
            len(parsed),
            len(markets),
            summary[CLASS_TOKEN],
            summary[CLASS_MEDIA],
            summary[CLASS_UNPRICED],
            summary[CLASS_NO_OFFER],
        )
        return True

    def class_summary(self) -> dict[str, int]:
        counts = {CLASS_TOKEN: 0, CLASS_MEDIA: 0, CLASS_UNPRICED: 0, CLASS_NO_OFFER: 0}
        for model in self.models.values():
            counts[model.price_class] = counts.get(model.price_class, 0) + 1
        return counts

    def snapshot(self, ttl_seconds: int) -> dict:
        """Admin-facing cache state. Contains no secrets."""
        market_age = None
        if self.market_computed_at:
            market_age = round(time.time() - self.market_computed_at / 1000, 1)
        return {
            "model_count": len(self.models),
            "market_offer_count": self.market_count,
            "market_source": self.market_source,
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
            "market_computed_at": self.market_computed_at,
            "market_age_seconds": market_age,
            "stale": self.is_stale(ttl_seconds),
            "last_error": self.last_error,
            "refresh_count": self.refresh_count,
            "price_classes": self.class_summary(),
        }
