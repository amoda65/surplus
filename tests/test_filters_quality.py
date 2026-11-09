"""Unit tests for the quality/depth/trusted filter rules added on top of price.

Synthetic market rows go through catalog._market_from_payload first, so the raw
/api/markets field names and microdollar units are exercised end to end.
"""

from __future__ import annotations

import pytest

from surplus.catalog import _market_from_payload
from surplus.config import FilterConfig
from surplus.filters import ModelInfo, evaluate


def build_model(market_row: dict, entry: dict | None = None) -> ModelInfo:
    """ModelInfo from one raw /api/markets row (plus a minimal catalog entry)."""
    base_entry = {
        "id": "test-model",
        "name": "Test Model",
        "provider": "TestProv",
        "context_length": 128000,
        "pricing": {"prompt": "0.0000010000", "completion": "0.0000030000"},
        "supported_features": ["streaming", "tools"],
        "architecture": {"modality": "text->text"},
        "top_provider": {"max_completion_tokens": 8192, "is_moderated": False},
    }
    if entry:
        base_entry.update(entry)
    markets = _market_from_payload({"markets": [{"model": base_entry["id"], **market_row}]})
    return ModelInfo.from_catalog_entry(base_entry, None, markets.get(base_entry["id"]))


def off(**overrides) -> FilterConfig:
    """All filters off except what the test enables."""
    base = {
        "enable_price_input": False,
        "enable_price_output": False,
        "enable_discount": False,
        "enable_models_allow": False,
        "enable_models_deny": False,
        "enable_providers_allow": False,
        "enable_context_min": False,
        "enable_trusted_only": False,
        "enable_min_uptime": False,
        "enable_max_ttft": False,
        "enable_min_tps": False,
        "enable_min_sellers": False,
        "enable_min_uncapped": False,
        "enable_cache_write_cap": False,
        "enable_require_features": False,
        "enable_exclude_tightening": False,
    }
    base.update(overrides)
    return FilterConfig(**base)


HEALTHY_ROW = {
    "best_input_per_1m": 2000,       # $0.002 / 1M
    "best_output_per_1m": 6000,      # $0.006 / 1M
    "healthy_seller_count": 50,
    "num_sellers": 60,
    "uncapped_discounted_offer_count": 20,
    "discounted_liquidity_usd": 500_000_000,   # $500 (microdollars)
    "best_cache_read_per_1m": 200,
    "best_cache_write_per_1m": 2_000_000,      # $2.0 / 1M
    "uptime_pct": 99.2,
    "ttft_p50_ms": 800,
    "tps_p50": 120,
    "tps_samples": 900,
    "ttft_samples": 950,
    "requests_24h": 12345,
    "volume_24h": 9_000_000,
    "best_live_discount_pct": 99.5,
    "discount_trend": {"direction": "deepening", "current_discount_pct": 99.5},
}


# ------------------------------------------------------------ quality gates


def test_uptime_gate_rejects_below_floor() -> None:
    model = build_model({**HEALTHY_ROW, "uptime_pct": 42.0})
    assert "min_uptime" in evaluate(model, off(enable_min_uptime=True, min_uptime_pct=90))


def test_uptime_gate_passes_at_boundary_and_above() -> None:
    at = build_model({**HEALTHY_ROW, "uptime_pct": 90.0})
    above = build_model({**HEALTHY_ROW, "uptime_pct": 99.9})
    cfg = off(enable_min_uptime=True, min_uptime_pct=90)
    assert evaluate(at, cfg) == []
    assert evaluate(above, cfg) == []


def test_quality_gates_fail_open_on_missing_measurements() -> None:
    """No measurement must never mean rejection (upstream only samples traffic)."""
    model = build_model({**HEALTHY_ROW, "uptime_pct": None, "ttft_p50_ms": None, "tps_p50": None})
    cfg = off(enable_min_uptime=True, enable_max_ttft=True, enable_min_tps=True)
    assert evaluate(model, cfg) == []


def test_ttft_and_tps_gates() -> None:
    slow_start = build_model({**HEALTHY_ROW, "ttft_p50_ms": 9000})
    slow_gen = build_model({**HEALTHY_ROW, "tps_p50": 5})
    assert "max_ttft" in evaluate(slow_start, off(enable_max_ttft=True, max_ttft_ms=5000))
    assert "min_tps" in evaluate(slow_gen, off(enable_min_tps=True, min_tps=20))
    ok = build_model(HEALTHY_ROW)
    assert evaluate(ok, off(enable_max_ttft=True, enable_min_tps=True)) == []


# ------------------------------------------------------------- market depth


def test_min_sellers_and_uncapped_gates() -> None:
    thin = build_model(
        {**HEALTHY_ROW, "healthy_seller_count": 2, "uncapped_discounted_offer_count": 0}
    )
    failed = evaluate(thin, off(enable_min_sellers=True, enable_min_uncapped=True))
    assert "min_sellers" in failed
    assert "min_uncapped" in failed
    healthy = build_model(HEALTHY_ROW)
    assert evaluate(healthy, off(enable_min_sellers=True, enable_min_uncapped=True)) == []


# ---------------------------------------------------------- cache economics


def test_cache_write_cap() -> None:
    model = build_model(HEALTHY_ROW)  # cache write = $2.0 / 1M
    assert model.cache_write_per_1m == pytest.approx(2.0)
    cfg = off(enable_cache_write_cap=True, max_cache_write_per_1m=1.0)
    assert "cache_write_cap" in evaluate(model, cfg)
    assert evaluate(model, off(enable_cache_write_cap=True, max_cache_write_per_1m=2.0)) == []
    # missing cache pricing fails open
    no_cache = build_model({**HEALTHY_ROW, "best_cache_write_per_1m": None})
    strict = off(enable_cache_write_cap=True, max_cache_write_per_1m=0.001)
    assert evaluate(no_cache, strict) == []


# --------------------------------------------------------------- capability


def test_require_features_all_must_match() -> None:
    model = build_model(HEALTHY_ROW)  # streaming + tools
    cfg = off(enable_require_features=True, require_features=["streaming", "tools"])
    assert evaluate(model, cfg) == []
    cfg = off(enable_require_features=True, require_features=["reasoning"])
    assert "require_features" in evaluate(model, cfg)
    cfg = off(enable_require_features=True, require_features=["STREAMING"])
    assert evaluate(model, cfg) == []


# -------------------------------------------------------------------- trend


def test_exclude_tightening() -> None:
    tightening = build_model(
        {**HEALTHY_ROW, "discount_trend": {"direction": "tightening", "current_discount_pct": 80}}
    )
    deepening = build_model(HEALTHY_ROW)
    cfg = off(enable_exclude_tightening=True)
    assert "exclude_tightening" in evaluate(tightening, cfg)
    assert evaluate(deepening, cfg) == []
    # missing trend data fails open
    no_trend = build_model({**HEALTHY_ROW, "discount_trend": None})
    assert evaluate(no_trend, cfg) == []


# ------------------------------------------------------------- trusted only


TRUSTED_PROVIDERS = [
    {
        "provider": "cheap-untrusted", "trusted": False,
        "best_input_per_1m": 100, "best_output_per_1m": 300,
        "healthy_seller_count": 5, "uncapped_discounted_offer_count": 5,
    },
    {
        "provider": "pricier-trusted", "trusted": True,
        "best_input_per_1m": 500_000, "best_output_per_1m": 1_500_000,
        "healthy_seller_count": 4, "uncapped_discounted_offer_count": 4,
    },
    {
        "provider": "best-trusted", "trusted": True,
        "best_input_per_1m": 300_000, "best_output_per_1m": 900_000,
        "healthy_seller_count": 3, "uncapped_discounted_offer_count": 3,
    },
]


def test_trusted_only_switches_the_price_source() -> None:
    row = {**HEALTHY_ROW, "providers": TRUSTED_PROVIDERS}
    model = build_model(row)
    # best trusted offer: $0.30 in / $0.90 out (lowest >0 among trusted)
    assert model.trusted_input_per_1m == pytest.approx(0.30)
    assert model.trusted_output_per_1m == pytest.approx(0.90)
    # overall market price is the untrusted $0.002/$0.006
    assert model.market_input_per_1m == pytest.approx(0.002)

    # caps that the market price passes but the trusted price fails
    cfg = off(enable_price_input=True, enable_price_output=True,
              max_input_per_1m=0.1, max_output_per_1m=0.5)
    assert evaluate(model, cfg) == []                      # trusted_only off: cheap price passes
    trusted_cfg = off(enable_trusted_only=True, enable_price_input=True, enable_price_output=True,
                      max_input_per_1m=0.1, max_output_per_1m=0.5)
    failed = evaluate(model, trusted_cfg)
    assert "price_input" in failed and "price_output" in failed


def test_trusted_only_rejects_models_without_trusted_offers() -> None:
    row = {
        **HEALTHY_ROW,
        "providers": [
            {"provider": "cheap-untrusted", "trusted": False, "best_input_per_1m": 100,
             "best_output_per_1m": 300, "healthy_seller_count": 5},
        ],
    }
    model = build_model(row)
    assert model.trusted_input_per_1m is None
    failed = evaluate(model, off(enable_trusted_only=True))
    assert "no_trusted_offer" in failed


def test_trusted_only_discount_floor_uses_trusted_figure() -> None:
    row = {**HEALTHY_ROW, "providers": TRUSTED_PROVIDERS, "best_trusted_offer_discount_pct": 40.0}
    model = build_model(row)
    cfg = off(enable_trusted_only=True, enable_discount=True, min_discount_pct=50)
    assert "discount" in evaluate(model, cfg)
    cfg = off(enable_discount=True, min_discount_pct=50)
    # without trusted_only the live discount (99.5) passes the same floor
    assert evaluate(model, cfg) == []


# ------------------------------------------------------------- detail view


def test_detail_exposes_providers_and_quality() -> None:
    model = build_model({**HEALTHY_ROW, "providers": TRUSTED_PROVIDERS})
    detail = model.to_detail([])
    assert detail["providers"][0]["trusted"] is True      # trusted first
    assert detail["uptime_pct"] == pytest.approx(99.2)
    assert detail["ttft_samples"] == 950
    assert detail["liquidity_usd"] == pytest.approx(500.0)
    assert detail["requests_24h"] == 12345
    assert detail["volume_24h_usd"] == pytest.approx(9.0)
    assert detail["modality"] == "text->text"
    assert "tools" in detail["features"]
