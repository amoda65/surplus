"""Unit tests for the filter engine, on a fixture captured from the live API.

The fixture joins three real endpoints for eight models chosen to cover every
case: token-priced cheap/expensive, token-priced with no list price, media-unit
priced, unpriced-but-listed, and no live offer at all.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from surplus.catalog import _market_from_payload, _reference_from_prices
from surplus.config import FilterConfig
from surplus.filters import (
    CLASS_MEDIA,
    CLASS_NO_OFFER,
    CLASS_TOKEN,
    CLASS_UNPRICED,
    NO_MARKET_OFFER,
    NOT_TOKEN_PRICED,
    UNKNOWN_MODEL,
    ModelInfo,
    evaluate,
    evaluate_by_id,
    minimum_discount_path_prefix,
)

FIXTURE = Path(__file__).parent / "fixtures" / "catalog_sample.json"


@pytest.fixture(scope="module")
def catalog() -> dict[str, ModelInfo]:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    references = _reference_from_prices(data["prices"])
    markets = _market_from_payload(data["marketplace"])
    return {
        entry["id"]: ModelInfo.from_catalog_entry(
            entry, references.get(entry["id"]), markets.get(entry["id"])
        )
        for entry in data["models"]["data"]
    }


def no_filters() -> FilterConfig:
    """Every toggleable filter disabled: only the always-on rules may fire."""
    return FilterConfig(
        enable_price_input=False,
        enable_price_output=False,
        enable_discount=False,
        enable_models_allow=False,
        enable_models_deny=False,
        enable_providers_allow=False,
        enable_context_min=False,
        allow_unknown_models=False,
    )


# ------------------------------------------------------------ price semantics


def test_market_prices_converted_from_microdollars(catalog: dict[str, ModelInfo]) -> None:
    """best_input_per_1m is microdollars per 1M tokens -> divide by 1e6."""
    qwen = catalog["qwen3.8-max-0902"]
    assert qwen.market_input_per_1m == pytest.approx(0.002163)
    assert qwen.market_output_per_1m == pytest.approx(0.006489)
    # the catalog list price is a different, much larger number
    assert qwen.list_input_per_1m == pytest.approx(2.0)
    assert qwen.list_output_per_1m == pytest.approx(6.0)


@pytest.mark.parametrize(
    ("micro", "typed"),
    [
        (2163, 0.002163),
        (6489, 0.006489),
        (69, 0.000069),
        (57000, 0.057),
        (7182000, 7.182),
    ],
)
def test_conversion_matches_the_decimal_a_human_would_type(micro: int, typed: float) -> None:
    """A cap typed as a decimal must compare exactly against the converted price."""
    entry = {
        "id": "m",
        "pricing": {"prompt": "0", "completion": "0"},
    }
    model = ModelInfo.from_catalog_entry(
        entry, None, {"input_micro_per_1m": micro, "output_micro_per_1m": micro}
    )
    assert model.market_input_per_1m == typed
    assert evaluate(model, FilterConfig(max_input_per_1m=typed, max_output_per_1m=typed)) == []


def test_price_classes(catalog: dict[str, ModelInfo]) -> None:
    assert catalog["qwen3.8-max-0902"].price_class == CLASS_TOKEN
    assert catalog["gpt-5-image-mini"].price_class == CLASS_TOKEN
    assert catalog["flux-2-max-edit"].price_class == CLASS_MEDIA
    assert catalog["venice-flux-3-image"].price_class == CLASS_UNPRICED
    assert catalog["deepseek-r1"].price_class == CLASS_NO_OFFER


def test_media_unit_price_is_surfaced(catalog: dict[str, ModelInfo]) -> None:
    flux = catalog["flux-2-max-edit"]
    assert flux.media_unit == "image"
    assert flux.media_unit_price_usd == pytest.approx(0.096)


# ------------------------------------------------------- always-on rejection


def test_unroutable_models_rejected_even_with_all_filters_off(
    catalog: dict[str, ModelInfo]
) -> None:
    filters = no_filters()
    assert evaluate(catalog["deepseek-r1"], filters) == [NO_MARKET_OFFER]
    assert evaluate(catalog["flux-2-max-edit"], filters) == [NOT_TOKEN_PRICED]
    assert evaluate(catalog["venice-flux-3-image"], filters) == [NOT_TOKEN_PRICED]
    assert evaluate(catalog["qwen3.8-max-0902"], filters) == []


def test_no_offer_rejection_is_not_the_unknown_model_opt_in(
    catalog: dict[str, ModelInfo]
) -> None:
    """allow_unknown_models must not smuggle in models with no live offer."""
    filters = no_filters()
    filters.allow_unknown_models = True
    assert evaluate(catalog["deepseek-r1"], filters) == [NO_MARKET_OFFER]


# ------------------------------------------------------------- price caps


def test_caps_use_market_price_not_list_price(catalog: dict[str, ModelInfo]) -> None:
    """qwen3.8-max-0902 lists at $2/$6 but trades at $0.0022/$0.0065 -> passes."""
    filters = FilterConfig(max_input_per_1m=0.1, max_output_per_1m=0.5)
    assert evaluate(catalog["qwen3.8-max-0902"], filters) == []
    assert evaluate(catalog["openai-gpt-oss-120b"], filters) == []
    assert evaluate(catalog["minimax-m2.1"], filters) == []


def test_expensive_models_rejected_on_market_price(catalog: dict[str, ModelInfo]) -> None:
    filters = FilterConfig(max_input_per_1m=0.1, max_output_per_1m=0.5)
    # market $7.182 / $43.092 per 1M
    assert evaluate(catalog["gpt-5.5-pro"], filters) == ["price_input", "price_output"]
    # market $0.625 in (over cap) / $0.50 out (exactly at cap -> passes)
    assert evaluate(catalog["gpt-5-image-mini"], filters) == ["price_input"]


def test_cap_is_inclusive_at_the_boundary(catalog: dict[str, ModelInfo]) -> None:
    """`max_*` means "at most": exactly at the cap passes (owner decision)."""
    qwen_out = catalog["qwen3.8-max-0902"].market_output_per_1m
    at_cap = FilterConfig(
        enable_price_input=False, max_output_per_1m=qwen_out, enable_discount=False
    )
    assert evaluate(catalog["qwen3.8-max-0902"], at_cap) == []
    just_under = FilterConfig(
        enable_price_input=False, max_output_per_1m=qwen_out - 1e-6, enable_discount=False
    )
    assert evaluate(catalog["qwen3.8-max-0902"], just_under) == ["price_output"]


def test_very_tight_caps_keep_only_the_cheapest(catalog: dict[str, ModelInfo]) -> None:
    filters = FilterConfig(max_input_per_1m=0.0001, max_output_per_1m=0.0003)
    assert evaluate(catalog["openai-gpt-oss-120b"], filters) == []  # 0.000069 / 0.000297
    assert evaluate(catalog["qwen3.8-max-0902"], filters) == ["price_input", "price_output"]


def test_zero_market_price_passes_caps(catalog: dict[str, ModelInfo]) -> None:
    """A genuinely $0 offer is the cheapest possible, so it passes any cap."""
    free = ModelInfo.from_catalog_entry(
        {"id": "free-model", "pricing": {"prompt": "0.0000030000", "completion": "0.0000090000"}},
        None,
        {"input_micro_per_1m": 0, "output_micro_per_1m": 0, "healthy_seller_count": 1},
    )
    # no token price at all -> unpriced, rejected by the always-on rule
    assert evaluate(free, FilterConfig(max_input_per_1m=0.0001, max_output_per_1m=0.0001)) == [
        NOT_TOKEN_PRICED
    ]
    priced_zero_in = ModelInfo.from_catalog_entry(
        {"id": "zero-in", "pricing": {"prompt": "0", "completion": "0"}},
        None,
        {"input_micro_per_1m": 0, "output_micro_per_1m": 500},
    )
    assert evaluate(
        priced_zero_in, FilterConfig(max_input_per_1m=0.0001, max_output_per_1m=0.001)
    ) == []


# ------------------------------------------------------------------ discount


def test_discount_uses_the_marketplace_figure(catalog: dict[str, ModelInfo]) -> None:
    filters = no_filters()
    filters.enable_discount = True
    filters.min_discount_pct = 90
    assert evaluate(catalog["qwen3.8-max-0902"], filters) == []  # 99.89%
    assert evaluate(catalog["gpt-5.5-pro"], filters) == ["discount"]  # 76.06%
    # no upstream figure, but /v1/prices has a reference -> discount is provable
    assert catalog["minimax-m2.1"].best_live_discount_pct is None
    assert catalog["minimax-m2.1"].estimated_discount_pct is not None
    assert evaluate(catalog["minimax-m2.1"], filters) == []


def test_unprovable_discount_fails_the_floor() -> None:
    """No marketplace figure and no list/reference price -> cannot prove a discount."""
    model = ModelInfo.from_catalog_entry(
        {"id": "opaque", "pricing": {"prompt": "0", "completion": "0"}},
        None,
        {"input_micro_per_1m": 1000, "output_micro_per_1m": 2000},
    )
    assert model.estimated_discount_pct is None
    assert evaluate(model, FilterConfig(enable_discount=True, min_discount_pct=1)) == ["discount"]
    # a zero floor is satisfied even without proof
    assert evaluate(model, FilterConfig(enable_discount=True, min_discount_pct=0)) == []


def test_discount_falls_back_to_list_vs_market() -> None:
    model = ModelInfo.from_catalog_entry(
        {
            "id": "fallback",
            "pricing": {"prompt": "0.0000020000", "completion": "0.0000060000"},
        },
        None,
        # market: $2 in / $3 out per 1M, against a list price of $2 / $6
        {"input_micro_per_1m": 2_000_000, "output_micro_per_1m": 3_000_000},
    )
    assert model.market_output_per_1m == pytest.approx(3.0)
    assert model.estimated_discount_pct == pytest.approx(50.0)
    discount_only = FilterConfig(
        enable_price_input=False, enable_price_output=False, enable_discount=True
    )
    discount_only.min_discount_pct = 50
    assert evaluate(model, discount_only) == []
    discount_only.min_discount_pct = 51
    assert evaluate(model, discount_only) == ["discount"]


def test_discount_prefers_marketplace_figure_over_computed() -> None:
    model = ModelInfo.from_catalog_entry(
        {"id": "both", "pricing": {"prompt": "0.0000020000", "completion": "0.0000060000"}},
        None,
        {
            "input_micro_per_1m": 2_000_000,
            "output_micro_per_1m": 3_000_000,
            "best_live_discount_pct": 42.0,
        },
    )
    # computed from prices would be 50.0; upstream's own figure wins
    assert model.estimated_discount_pct == pytest.approx(42.0)


# ------------------------------------------------------- list-based filters


def test_deny_wins_over_allow(catalog: dict[str, ModelInfo]) -> None:
    filters = no_filters()
    filters.enable_models_allow = True
    filters.allow_models = ["minimax-m2.1", "gpt-5.5-pro"]
    filters.enable_models_deny = True
    filters.deny_models = ["minimax-m2.1"]
    assert evaluate(catalog["minimax-m2.1"], filters) == ["models_deny"]
    assert evaluate(catalog["gpt-5.5-pro"], filters) == []
    assert evaluate(catalog["qwen3.8-max-0902"], filters) == ["models_allow"]


def test_provider_allow_list_is_case_insensitive(catalog: dict[str, ModelInfo]) -> None:
    filters = no_filters()
    filters.enable_providers_allow = True
    filters.allow_providers = ["qwen"]
    assert evaluate(catalog["qwen3.8-max-0902"], filters) == []
    assert evaluate(catalog["minimax-m2.1"], filters) == ["providers_allow"]


def test_context_min(catalog: dict[str, ModelInfo]) -> None:
    filters = no_filters()
    filters.enable_context_min = True
    filters.min_context_length = 32000
    assert evaluate(catalog["qwen3.8-max-0902"], filters) == []  # 1,000,000
    assert evaluate(catalog["gpt-5-image-mini"], filters) == ["context_min"]  # 0


# --------------------------------------------------------------------- gate


def test_unknown_model_fails_closed(catalog: dict[str, ModelInfo]) -> None:
    passed, failed = evaluate_by_id("no-such-model", catalog, FilterConfig())
    assert passed is False
    assert failed == [UNKNOWN_MODEL]


def test_unknown_model_allowed_when_opted_in(catalog: dict[str, ModelInfo]) -> None:
    filters = FilterConfig(allow_unknown_models=True)
    passed, failed = evaluate_by_id("no-such-model", catalog, filters)
    assert passed is True
    assert failed == []


def test_gate_reports_every_failed_rule(catalog: dict[str, ModelInfo]) -> None:
    filters = FilterConfig(max_input_per_1m=0.001, max_output_per_1m=0.005)
    filters.enable_context_min = True
    filters.min_context_length = 2_000_000
    passed, failed = evaluate_by_id("qwen3.8-max-0902", catalog, filters)
    assert passed is False
    assert failed == ["price_input", "price_output", "context_min"]


def test_gate_blocks_a_model_with_no_offer(catalog: dict[str, ModelInfo]) -> None:
    passed, failed = evaluate_by_id("deepseek-r1", catalog, FilterConfig())
    assert passed is False
    assert failed == [NO_MARKET_OFFER]


@pytest.mark.parametrize(
    ("enabled", "pct", "expected"),
    [
        (True, 30, "/min30"),
        (True, 0, "/min0"),
        (True, 100, "/min100"),
        (False, 30, ""),
        (True, -1, ""),
        (True, 101, ""),
        (True, 30.5, ""),
    ],
)
def test_minimum_discount_prefix(enabled: bool, pct, expected: str) -> None:
    filters = no_filters()
    filters.enable_discount = enabled
    filters.min_discount_pct = pct
    assert minimum_discount_path_prefix(filters) == expected


def test_admin_row_exposes_market_list_and_class(catalog: dict[str, ModelInfo]) -> None:
    row = catalog["qwen3.8-max-0902"].to_admin_row([])
    assert row["price_class"] == CLASS_TOKEN
    assert row["market_input_per_1m"] == pytest.approx(0.002163)
    assert row["market_output_per_1m"] == pytest.approx(0.006489)
    assert row["list_input_per_1m"] == pytest.approx(2.0)
    assert row["list_output_per_1m"] == pytest.approx(6.0)
    assert row["estimated_discount_pct"] == pytest.approx(99.89)
    assert row["healthy_seller_count"] == 233
    assert row["passed"] is True

    blocked = catalog["gpt-5.5-pro"].to_admin_row(["price_input", "price_output"])
    assert blocked["passed"] is False
    assert blocked["failed_filters"] == ["price_input", "price_output"]
