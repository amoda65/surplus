"""Unit tests for the account reader: microdollar conversion and degradation.

No network: only the pure normalization helpers plus a MockTransport for the
fetch paths, so these never need a real API key.
"""

from __future__ import annotations

import httpx
import pytest

from surplus.account import (
    AccountReader,
    _normalize_balance,
    _normalize_overview,
    _normalize_requests,
)

BASE = "https://api.example.test"


# ------------------------------------------------------------ normalization


def test_balance_converts_microdollar_strings_once_for_display() -> None:
    raw = {
        "credit_balance_usdc": "142180000",   # $142.18
        "fiat_available_usdc": "120000000",   # $120.00
        "usdc_available_usdc": "22180000",    # $22.18
        "fiat_held_usdc": "0",
        "usdc_held_usdc": "0",
        "pending_deposit_usdc": "5000000",
        "account_status": "active",
        "withdrawals_paused": False,
    }
    out = _normalize_balance(raw)
    assert out["credit_balance_usd"] == pytest.approx(142.18)
    assert out["fiat_available_usd"] == pytest.approx(120.0)
    assert out["usdc_available_usd"] == pytest.approx(22.18)
    assert out["pending_deposit_usd"] == pytest.approx(5.0)
    assert out["account_status"] == "active"


def test_balance_tolerates_missing_and_bad_fields() -> None:
    out = _normalize_balance({"credit_balance_usdc": "not-a-number", "account_status": "frozen"})
    assert out["credit_balance_usd"] is None
    assert out["account_status"] == "frozen"


def test_overview_converts_micro_fields_and_keeps_coverage_flag() -> None:
    out = _normalize_overview({"coverage": False, "spend_micro": "1500000", "requests": 12})
    assert out["coverage"] is False           # false means "not computed", not zero
    assert out["spend_usd"] == pytest.approx(1.5)
    assert out["requests"] == 12


def test_requests_normalizes_entries_and_costs() -> None:
    raw = {
        "data": [
            {"model": "m1", "status": "succeeded", "buyer_cost_micro": "250000", "latency_ms": 900},
            {"model": "m2", "status": "failed"},
        ],
        "cursor": "abc",
    }
    out = _normalize_requests(raw)
    assert len(out["entries"]) == 2
    assert out["entries"][0]["buyer_cost_usd"] == pytest.approx(0.25)
    assert out["cursor"] == "abc"


# --------------------------------------------------------------- fetch paths


def mock_transport(routes: dict[str, tuple[int, dict]]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        for suffix, (status, body) in routes.items():
            if request.url.path.endswith(suffix):
                return httpx.Response(status, json=body)
        return httpx.Response(404, json={"error": "not found"})

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_account_without_key_is_unavailable_not_an_error() -> None:
    reader = AccountReader()
    async with httpx.AsyncClient(transport=mock_transport({})) as client:
        out = await reader.account(client, BASE, None)
    assert out == {"available": False, "reason": "no_api_key"}


@pytest.mark.asyncio
async def test_account_joins_balance_and_overviews() -> None:
    routes = {
        "/v1/payments/balance": (
            200,
            {"credit_balance_usdc": "1000000", "account_status": "active"},
        ),
        "/v1/buyer/overview": (200, {"coverage": True, "spend_micro": "250000"}),
    }
    reader = AccountReader()
    async with httpx.AsyncClient(transport=mock_transport(routes)) as client:
        out = await reader.account(client, BASE, "si_test")
    assert out["available"] is True
    assert out["balance"]["credit_balance_usd"] == pytest.approx(1.0)
    assert out["overview_7d"]["spend_usd"] == pytest.approx(0.25)


@pytest.mark.asyncio
async def test_account_degrades_when_upstream_refuses_the_key() -> None:
    routes = {
        "/v1/payments/balance": (401, {"error": {"code": "unauthorized"}}),
        "/v1/buyer/overview": (401, {"error": {"code": "unauthorized"}}),
    }
    reader = AccountReader()
    async with httpx.AsyncClient(transport=mock_transport(routes)) as client:
        out = await reader.account(client, BASE, "si_bad")
    assert out["available"] is False
    assert out["reason"] == "unauthorized_401"


@pytest.mark.asyncio
async def test_requests_passes_filters_and_never_caches() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": [], "cursor": None})

    reader = AccountReader()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await reader.requests(
            client, BASE, "si_test", {"limit": "10", "model": "m1", "evil": "x"}
        )
    assert out["available"] is True
    query = seen[0].url.params
    assert query["limit"] == "10"
    assert query["model"] == "m1"
    assert "evil" not in query                      # only the allow-list goes upstream
    assert seen[0].headers["authorization"] == "Bearer si_test"
