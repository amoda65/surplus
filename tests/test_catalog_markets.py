"""Tests for the /api/markets source and the /api/marketplace fallback."""

from __future__ import annotations

import httpx
import pytest

from surplus.catalog import Catalog, _market_from_payload, _market_rows

UPSTREAM = "https://api.surplusintelligence.ai"

MARKETS_PAYLOAD = {
    "markets": [
        {
            "model": "m-token",
            "best_input_per_1m": 2000,
            "best_output_per_1m": 6000,
            "best_cache_write_per_1m": 2000000,
            "best_trusted_offer_discount_pct": 40.0,
            "healthy_seller_count": 12,
            "num_sellers": 20,
            "uncapped_discounted_offer_count": 9,
            "discounted_liquidity_usd": 1000000000,
            "uptime_pct": 98.5,
            "ttft_p50_ms": 900,
            "tps_p50": 90.5,
            "tps_samples": 100,
            "ttft_samples": 120,
            "requests_24h": 500,
            "volume_24h": 2500000,
            "discount_trend": {"direction": "deepening", "current_discount_pct": 95.0},
            "providers": [
                {"provider": "trusty", "trusted": True, "best_input_per_1m": 300000,
                 "best_output_per_1m": 900000, "healthy_seller_count": 4,
                 "uncapped_discounted_offer_count": 4},
            ],
        }
    ]
}

MARKETPLACE_PAYLOAD = {
    "data": {
        "computed_at": 1762650000000,
        "markets": [
            {"model": "m-token", "best_input_per_1m": 2000, "best_output_per_1m": 6000,
             "healthy_seller_count": 12, "num_sellers": 20},
        ],
    }
}

MODELS_PAYLOAD = {
    "data": [
        {"id": "m-token", "name": "M Token", "provider": "P", "context_length": 1000,
         "pricing": {"prompt": "0.0000010000", "completion": "0.0000030000"}},
    ]
}
PRICES_PAYLOAD = {"models": [], "providers": ["p1"], "updated_at": "x"}


def test_market_rows_accepts_both_envelopes() -> None:
    assert len(_market_rows(MARKETS_PAYLOAD)) == 1          # /api/markets
    assert len(_market_rows(MARKETPLACE_PAYLOAD)) == 1      # /api/marketplace
    assert _market_rows({}) == []
    assert _market_rows({"markets": "nope"}) == []


def test_markets_payload_maps_quality_and_liquidity_fields() -> None:
    markets = _market_from_payload(MARKETS_PAYLOAD)
    row = markets["m-token"]
    assert row["input_micro_per_1m"] == 2000
    assert row["cache_write_micro_per_1m"] == 2000000
    assert row["uncapped_offer_count"] == 9
    assert row["liquidity_micro_usd"] == 1000000000
    assert row["uptime_pct"] == 98.5
    assert row["discount_trend"]["direction"] == "deepening"
    assert row["providers"][0]["trusted"] is True


def transport_for(payloads: dict[str, dict]):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        for suffix, body in payloads.items():
            if path.endswith(suffix):
                return httpx.Response(200, json=body)
        return httpx.Response(404, json={"error": "not found"})

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_refresh_prefers_api_markets() -> None:
    payloads = {
        "/v1/models": MODELS_PAYLOAD,
        "/v1/prices": PRICES_PAYLOAD,
        "/api/markets": MARKETS_PAYLOAD,
        "/api/marketplace": MARKETPLACE_PAYLOAD,
    }
    catalog = Catalog(UPSTREAM)
    async with httpx.AsyncClient(transport=transport_for(payloads)) as client:
        assert await catalog.refresh(client) is True
    assert catalog.market_source == "/api/markets"
    model = catalog.models["m-token"]
    assert model.uptime_pct == 98.5
    assert model.trusted_output_per_1m == pytest.approx(0.9)
    assert model.liquidity_usd == pytest.approx(1000.0)


@pytest.mark.asyncio
async def test_refresh_falls_back_to_marketplace_when_markets_missing() -> None:
    payloads = {
        "/v1/models": MODELS_PAYLOAD,
        "/v1/prices": PRICES_PAYLOAD,
        "/api/marketplace": MARKETPLACE_PAYLOAD,   # /api/markets -> 404
    }
    catalog = Catalog(UPSTREAM)
    async with httpx.AsyncClient(transport=transport_for(payloads)) as client:
        assert await catalog.refresh(client) is True
    assert catalog.market_source == "/api/marketplace"
    assert catalog.market_computed_at == 1762650000000
    # fields only /api/markets carries stay None and fail open in the filters
    assert catalog.models["m-token"].uptime_pct is None


@pytest.mark.asyncio
async def test_refresh_falls_back_when_markets_returns_empty_rows() -> None:
    payloads = {
        "/v1/models": MODELS_PAYLOAD,
        "/v1/prices": PRICES_PAYLOAD,
        "/api/markets": {"markets": []},
        "/api/marketplace": MARKETPLACE_PAYLOAD,
    }
    catalog = Catalog(UPSTREAM)
    async with httpx.AsyncClient(transport=transport_for(payloads)) as client:
        assert await catalog.refresh(client) is True
    assert catalog.market_source == "/api/marketplace"
