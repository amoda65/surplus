"""Unit tests for catalog parsing, joining, staleness and failure handling."""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest

from surplus.catalog import Catalog, _market_from_payload, _reference_from_prices
from surplus.filters import CLASS_MEDIA, CLASS_NO_OFFER, CLASS_TOKEN, CLASS_UNPRICED

FIXTURE = Path(__file__).parent / "fixtures" / "catalog_sample.json"
SAMPLE = json.loads(FIXTURE.read_text(encoding="utf-8"))
UPSTREAM = "https://api.surplusintelligence.ai"


def mock_transport(payloads: dict[str, dict] | None = None, fail: bool = False):
    """An httpx transport serving the fixture, or failing every request."""

    def handler(request: httpx.Request) -> httpx.Response:
        if fail:
            raise httpx.ConnectError("boom", request=request)
        path = request.url.path
        for suffix, body in (payloads or {}).items():
            if path.endswith(suffix):
                return httpx.Response(200, json=body)
        return httpx.Response(404, json={"error": "not found"})

    return httpx.MockTransport(handler)


@pytest.fixture
def payloads() -> dict[str, dict]:
    return {
        "/v1/models": SAMPLE["models"],
        "/v1/prices": SAMPLE["prices"],
        "/api/marketplace": SAMPLE["marketplace"],
    }


# ------------------------------------------------------------------- parsers


def test_reference_price_takes_highest_provider_list_price() -> None:
    references = _reference_from_prices(SAMPLE["prices"])
    # minimax-m2.1: providers quote output 0.95 and 1.5 -> reference is 1.5
    assert references["minimax-m2.1"]["output"] == pytest.approx(1.5)
    assert references["minimax-m2.1"]["input"] == pytest.approx(0.35)


def test_reference_ignores_malformed_payload() -> None:
    assert _reference_from_prices({}) == {}
    assert _reference_from_prices({"models": [{"providers": [{"pricing": {"input": "x"}}]}]}) == {}


def test_market_payload_indexed_by_model_with_raw_microdollars() -> None:
    markets = _market_from_payload(SAMPLE["marketplace"])
    qwen = markets["qwen3.8-max-0902"]
    assert qwen["input_micro_per_1m"] == 2163
    assert qwen["output_micro_per_1m"] == 6489
    assert qwen["direct_input_micro_per_1m"] == 2000000
    assert qwen["best_live_discount_pct"] == pytest.approx(99.89)
    assert qwen["healthy_seller_count"] == 233


def test_market_payload_handles_media_and_missing_prices() -> None:
    markets = _market_from_payload(SAMPLE["marketplace"])
    assert markets["flux-2-max-edit"]["media_unit"] == "image"
    assert markets["flux-2-max-edit"]["media_unit_price_micro"] == 96000
    assert markets["venice-flux-3-image"]["media_unit"] is None
    # a model with no market entry at all is simply absent
    assert "deepseek-r1" not in markets


def test_market_parser_ignores_malformed_payload() -> None:
    assert _market_from_payload({}) == {}
    assert _market_from_payload({"data": {}}) == {}
    assert _market_from_payload({"data": {"markets": [{}]}}) == {}


# -------------------------------------------------------------------- refresh


@pytest.mark.asyncio
async def test_refresh_joins_all_three_endpoints(payloads: dict[str, dict]) -> None:
    catalog = Catalog(UPSTREAM)
    async with httpx.AsyncClient(transport=mock_transport(payloads)) as client:
        assert await catalog.refresh(client) is True

    assert set(catalog.models) == {
        "qwen3.8-max-0902",
        "openai-gpt-oss-120b",
        "minimax-m2.1",
        "gpt-5.5-pro",
        "gpt-5-image-mini",
        "flux-2-max-edit",
        "venice-flux-3-image",
        "deepseek-r1",
    }
    qwen = catalog.models["qwen3.8-max-0902"]
    assert qwen.market_output_per_1m == pytest.approx(0.006489)
    assert qwen.list_output_per_1m == pytest.approx(6.0)
    assert catalog.market_count == 7
    assert catalog.market_computed_at == SAMPLE["marketplace"]["data"]["computed_at"]
    assert catalog.last_error is None


@pytest.mark.asyncio
async def test_class_summary_counts_every_bucket(payloads: dict[str, dict]) -> None:
    catalog = Catalog(UPSTREAM)
    async with httpx.AsyncClient(transport=mock_transport(payloads)) as client:
        await catalog.refresh(client)
    summary = catalog.class_summary()
    assert summary[CLASS_TOKEN] == 5
    assert summary[CLASS_MEDIA] == 1
    assert summary[CLASS_UNPRICED] == 1
    assert summary[CLASS_NO_OFFER] == 1


@pytest.mark.asyncio
async def test_failed_refresh_keeps_last_good_copy(payloads: dict[str, dict]) -> None:
    catalog = Catalog(UPSTREAM)
    async with httpx.AsyncClient(transport=mock_transport(payloads)) as client:
        await catalog.refresh(client)
    before = dict(catalog.models)
    before_count = catalog.market_count

    async with httpx.AsyncClient(transport=mock_transport(fail=True)) as client:
        assert await catalog.refresh(client) is False

    assert catalog.models == before, "a failed refresh must not wipe cached models"
    assert catalog.market_count == before_count
    assert (catalog.last_error or "").startswith("ConnectError")


@pytest.mark.asyncio
async def test_empty_catalog_response_is_rejected() -> None:
    catalog = Catalog(UPSTREAM)
    payloads = {
        "/v1/models": {"data": []},
        "/v1/prices": {"models": []},
        "/api/marketplace": SAMPLE["marketplace"],
    }
    async with httpx.AsyncClient(transport=mock_transport(payloads)) as client:
        assert await catalog.refresh(client) is False
    assert not catalog.loaded
    assert "EmptyCatalog" in (catalog.last_error or "")


@pytest.mark.asyncio
async def test_empty_marketplace_is_rejected_not_treated_as_free(payloads) -> None:
    """No offers must not publish a catalog where every price looks like $0."""
    catalog = Catalog(UPSTREAM)
    broken = dict(payloads)
    broken["/api/marketplace"] = {"data": {"markets": []}}
    async with httpx.AsyncClient(transport=mock_transport(broken)) as client:
        assert await catalog.refresh(client) is False
    assert not catalog.loaded
    assert "EmptyMarketplace" in (catalog.last_error or "")


@pytest.mark.asyncio
async def test_missing_marketplace_endpoint_is_rejected(payloads) -> None:
    catalog = Catalog(UPSTREAM)
    partial = {k: v for k, v in payloads.items() if "marketplace" not in k}
    async with httpx.AsyncClient(transport=mock_transport(partial)) as client:
        assert await catalog.refresh(client) is False
    assert not catalog.loaded


# ------------------------------------------------------------- staleness/UI


def test_staleness_window_is_two_ttls() -> None:
    catalog = Catalog(UPSTREAM)
    assert catalog.is_stale(300) is True  # never fetched
    catalog.last_fetch_at = time.time() - 500
    assert catalog.is_stale(300) is False  # within 2x TTL
    catalog.last_fetch_at = time.time() - 700
    assert catalog.is_stale(300) is True


def test_snapshot_reports_market_state_and_no_secrets() -> None:
    catalog = Catalog(UPSTREAM)
    catalog.last_fetch_at = time.time() - 12
    catalog.market_count = 480
    catalog.market_computed_at = int((time.time() - 30) * 1000)
    snapshot = catalog.snapshot(300)
    assert snapshot["age_seconds"] == pytest.approx(12, abs=1)
    assert snapshot["market_offer_count"] == 480
    assert snapshot["market_age_seconds"] == pytest.approx(30, abs=2)
    assert snapshot["stale"] is False
    assert "api_key" not in json.dumps(snapshot).lower()
