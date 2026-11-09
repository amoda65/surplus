"""Unit tests for catalog parsing, staleness and failure handling."""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest

from surplus.catalog import Catalog, _reference_from_prices

FIXTURE = Path(__file__).parent / "fixtures" / "catalog_sample.json"
SAMPLE = json.loads(FIXTURE.read_text(encoding="utf-8"))
UPSTREAM = "https://api.surplusintelligence.ai"


def test_reference_price_takes_highest_provider_list_price() -> None:
    references = _reference_from_prices(SAMPLE["prices"])
    # minimax-m2.1: providers quote output 0.95 and 1.5 -> reference is 1.5
    assert references["minimax-m2.1"]["output"] == pytest.approx(1.5)
    assert references["minimax-m2.1"]["input"] == pytest.approx(0.35)


def test_reference_missing_entries_are_absent() -> None:
    references = _reference_from_prices(SAMPLE["prices"])
    # gpt-5-image-mini has no provider offers in the fixture
    assert references["gpt-5-image-mini"] == {"input": None, "output": None}


def test_reference_ignores_malformed_payload() -> None:
    assert _reference_from_prices({}) == {}
    assert _reference_from_prices({"models": [{"providers": [{"pricing": {"input": "x"}}]}]}) == {}


def mock_transport(payloads: dict[str, dict] | None = None, fail: bool = False):
    """An httpx transport serving the fixture, or failing every request."""

    def handler(request: httpx.Request) -> httpx.Response:
        if fail:
            raise httpx.ConnectError("boom", request=request)
        key = request.url.path
        for suffix, body in (payloads or {}).items():
            if key.endswith(suffix):
                return httpx.Response(200, json=body)
        return httpx.Response(404, json={"error": "not found"})

    return httpx.MockTransport(handler)


@pytest.fixture
def payloads() -> dict[str, dict]:
    return {"/v1/models": SAMPLE["models"], "/v1/prices": SAMPLE["prices"]}


@pytest.mark.asyncio
async def test_refresh_populates_catalog(payloads: dict[str, dict]) -> None:
    catalog = Catalog(UPSTREAM)
    async with httpx.AsyncClient(transport=mock_transport(payloads)) as client:
        assert await catalog.refresh(client) is True
    assert catalog.loaded
    assert set(catalog.models) == {
        "openai-gpt-oss-120b",
        "minimax-m2.1",
        "gpt-5-image-mini",
        "gpt-5.5-pro",
    }
    assert catalog.last_error is None
    assert catalog.upstream_updated_at == SAMPLE["prices"]["updated_at"]
    assert catalog.refresh_count == 1


@pytest.mark.asyncio
async def test_failed_refresh_keeps_last_good_copy(payloads: dict[str, dict]) -> None:
    catalog = Catalog(UPSTREAM)
    async with httpx.AsyncClient(transport=mock_transport(payloads)) as client:
        await catalog.refresh(client)
    before = dict(catalog.models)

    async with httpx.AsyncClient(transport=mock_transport(fail=True)) as client:
        assert await catalog.refresh(client) is False

    assert catalog.models == before, "a failed refresh must not wipe cached models"
    assert catalog.last_error is not None
    assert catalog.last_error.startswith("ConnectError")


@pytest.mark.asyncio
async def test_empty_catalog_response_is_rejected() -> None:
    catalog = Catalog(UPSTREAM)
    payloads = {"/v1/models": {"data": []}, "/v1/prices": {"models": []}}
    async with httpx.AsyncClient(transport=mock_transport(payloads)) as client:
        assert await catalog.refresh(client) is False
    assert not catalog.loaded
    assert "EmptyCatalog" in (catalog.last_error or "")


def test_staleness_window_is_two_ttls() -> None:
    catalog = Catalog(UPSTREAM)
    assert catalog.is_stale(300) is True  # never fetched
    catalog.last_fetch_at = time.time() - 500
    assert catalog.is_stale(300) is False  # within 2x TTL
    catalog.last_fetch_at = time.time() - 700
    assert catalog.is_stale(300) is True


def test_snapshot_has_no_secrets_and_reports_age() -> None:
    catalog = Catalog(UPSTREAM)
    catalog.last_fetch_at = time.time() - 12
    snapshot = catalog.snapshot(300)
    assert snapshot["age_seconds"] == pytest.approx(12, abs=1)
    assert snapshot["stale"] is False
    assert "api_key" not in json.dumps(snapshot).lower()


def test_priced_summary_counts_zero_priced_models(payloads: dict[str, dict]) -> None:
    catalog = Catalog(UPSTREAM)
    references = _reference_from_prices(payloads["/v1/prices"])
    from surplus.filters import ModelInfo

    catalog.models = {
        entry["id"]: ModelInfo.from_catalog_entry(entry, references.get(entry["id"]))
        for entry in payloads["/v1/models"]["data"]
    }
    summary = catalog.priced_summary()
    assert summary["total"] == 4
    assert summary["zero_priced"] == 1
    assert summary["priced"] == 3
