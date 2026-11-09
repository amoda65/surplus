"""Unit tests for the filter engine, on a fixture captured from the live API."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from surplus.catalog import _reference_from_prices
from surplus.config import FilterConfig
from surplus.filters import (
    UNKNOWN_MODEL,
    ModelInfo,
    evaluate,
    evaluate_by_id,
    minimum_discount_path_prefix,
)

FIXTURE = Path(__file__).parent / "fixtures" / "catalog_sample.json"


@pytest.fixture(scope="module")
def catalog() -> dict[str, ModelInfo]:
    """Real catalog entries + real reference prices, normalized."""
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    references = _reference_from_prices(data["prices"])
    return {
        entry["id"]: ModelInfo.from_catalog_entry(entry, references.get(entry["id"]))
        for entry in data["models"]["data"]
    }


def no_filters() -> FilterConfig:
    """Every filter disabled: nothing may be rejected."""
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


def test_per_token_pricing_converted_to_per_1m(catalog: dict[str, ModelInfo]) -> None:
    # 0.0000000700 per token == $0.07 per 1M tokens
    assert catalog["openai-gpt-oss-120b"].input_per_1m == pytest.approx(0.07)
    assert catalog["openai-gpt-oss-120b"].output_per_1m == pytest.approx(0.30)
    assert catalog["gpt-5.5-pro"].output_per_1m == pytest.approx(180.0)


def test_all_disabled_passes_everything(catalog: dict[str, ModelInfo]) -> None:
    filters = no_filters()
    assert all(evaluate(model, filters) == [] for model in catalog.values())


def test_default_caps_match_live_expectations(catalog: dict[str, ModelInfo]) -> None:
    filters = FilterConfig(max_input_per_1m=2.0, max_output_per_1m=10.0)
    assert evaluate(catalog["openai-gpt-oss-120b"], filters) == []
    assert evaluate(catalog["minimax-m2.1"], filters) == []  # zero priced passes
    assert "price_input" in evaluate(catalog["gpt-5-image-mini"], filters)  # $2.50/1M in
    assert evaluate(catalog["gpt-5.5-pro"], filters) == ["price_input", "price_output"]


def test_zero_priced_model_passes_any_cap(catalog: dict[str, ModelInfo]) -> None:
    filters = FilterConfig(max_input_per_1m=0.000001, max_output_per_1m=0.000001)
    assert evaluate(catalog["minimax-m2.1"], filters) == []


def test_cap_is_strict_not_inclusive(catalog: dict[str, ModelInfo]) -> None:
    model = catalog["openai-gpt-oss-120b"]  # 0.07 in / 0.30 out per 1M
    assert evaluate(model, FilterConfig(max_input_per_1m=0.07, max_output_per_1m=10.0)) == [
        "price_input"
    ]
    assert evaluate(model, FilterConfig(max_input_per_1m=0.0701, max_output_per_1m=10.0)) == []


def test_deny_wins_over_allow(catalog: dict[str, ModelInfo]) -> None:
    filters = no_filters()
    filters.enable_models_allow = True
    filters.allow_models = ["minimax-m2.1", "gpt-5.5-pro"]
    filters.enable_models_deny = True
    filters.deny_models = ["minimax-m2.1"]
    # listed in BOTH: deny wins, the model is rejected
    assert evaluate(catalog["minimax-m2.1"], filters) == ["models_deny"]
    assert evaluate(catalog["gpt-5.5-pro"], filters) == []
    # not in the allow list at all: rejected by the allow filter
    assert evaluate(catalog["openai-gpt-oss-120b"], filters) == ["models_allow"]


def test_allow_list_filters_unlisted_models(catalog: dict[str, ModelInfo]) -> None:
    filters = no_filters()
    filters.enable_models_allow = True
    filters.allow_models = ["openai-gpt-oss-120b"]
    assert evaluate(catalog["gpt-5.5-pro"], filters) == ["models_allow"]


def test_provider_allow_list_is_case_insensitive(catalog: dict[str, ModelInfo]) -> None:
    filters = no_filters()
    filters.enable_providers_allow = True
    filters.allow_providers = ["minimax"]
    assert evaluate(catalog["minimax-m2.1"], filters) == []
    assert evaluate(catalog["openai-gpt-oss-120b"], filters) == ["providers_allow"]


def test_context_min_rejects_small_and_zero_context(catalog: dict[str, ModelInfo]) -> None:
    filters = no_filters()
    filters.enable_context_min = True
    filters.min_context_length = 32000
    assert evaluate(catalog["minimax-m2.1"], filters) == []  # 198000
    assert evaluate(catalog["gpt-5-image-mini"], filters) == ["context_min"]  # 0


def test_discount_filter_uses_reference_prices(catalog: dict[str, ModelInfo]) -> None:
    filters = no_filters()
    filters.enable_discount = True
    filters.min_discount_pct = 30

    # minimax-m2.1: 0.0 output vs 1.5 reference -> 100% discount
    assert evaluate(catalog["minimax-m2.1"], filters) == []
    # gpt-5.5-pro: 180 output vs 180 reference -> 0% discount
    assert evaluate(catalog["gpt-5.5-pro"], filters) == ["discount"]
    # gpt-5-image-mini has no reference price: cannot prove a discount
    assert evaluate(catalog["gpt-5-image-mini"], filters) == ["discount"]


def test_discount_none_when_no_reference_price(catalog: dict[str, ModelInfo]) -> None:
    assert catalog["gpt-5-image-mini"].estimated_discount_pct is None


def test_estimated_discount_value(catalog: dict[str, ModelInfo]) -> None:
    # openai-gpt-oss-120b: 0.30 output vs 0.30 reference -> 0% discount
    assert catalog["openai-gpt-oss-120b"].estimated_discount_pct == pytest.approx(0.0)
    assert catalog["minimax-m2.1"].estimated_discount_pct == pytest.approx(100.0)


def test_unknown_model_fails_closed(catalog: dict[str, ModelInfo]) -> None:
    passed, failed = evaluate_by_id("no-such-model", catalog, FilterConfig())
    assert passed is False
    assert failed == [UNKNOWN_MODEL]


def test_unknown_model_allowed_when_opted_in(catalog: dict[str, ModelInfo]) -> None:
    filters = FilterConfig(allow_unknown_models=True)
    passed, failed = evaluate_by_id("no-such-model", catalog, filters)
    assert passed is True
    assert failed == []


def test_known_model_gate_reports_every_failure(catalog: dict[str, ModelInfo]) -> None:
    filters = FilterConfig(max_input_per_1m=1.0, max_output_per_1m=5.0)
    filters.enable_context_min = True
    filters.min_context_length = 50000
    passed, failed = evaluate_by_id("gpt-5-image-mini", catalog, filters)
    assert passed is False
    assert failed == ["price_input", "context_min"]


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


def test_admin_row_exposes_reasons(catalog: dict[str, ModelInfo]) -> None:
    row = catalog["gpt-5.5-pro"].to_admin_row(["price_input", "price_output"])
    assert row["passed"] is False
    assert row["failed_filters"] == ["price_input", "price_output"]
    assert row["input_per_1m"] == pytest.approx(30.0)
    assert row["output_per_1m"] == pytest.approx(180.0)
