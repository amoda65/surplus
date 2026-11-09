"""Filter evaluation: pure functions, no I/O, no globals.

Every filter type is independently switchable. ``evaluate`` returns the ids of
the filters that rejected a model so callers can surface ``failed_filters``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import FilterConfig

# Filter ids, in evaluation order. Keep in sync with the admin UI cards.
FILTER_IDS = (
    "models_deny",
    "models_allow",
    "providers_allow",
    "price_input",
    "price_output",
    "discount",
    "context_min",
)

UNKNOWN_MODEL = "unknown_model"

# Prices arrive as per-token decimal strings and are multiplied by 1e6, which
# can land a hair off the intended value (0.00000057 * 1e6 == 0.5700000000000001,
# 0.0000021 * 1e6 == 2.0999999999999996). Caps are inclusive maxima, so compare
# with this tolerance to keep a price that is exactly at the cap passing.
CAP_EPSILON = 1e-9


def _per_token_to_per_1m(value: object) -> float:
    """Upstream pricing is a per-token decimal string; convert to per-1M tokens."""
    try:
        return float(value or 0) * 1_000_000
    except (TypeError, ValueError):
        return 0.0


@dataclass(frozen=True)
class ModelInfo:
    """One catalog entry, normalized to per-1M-token pricing."""

    id: str
    name: str
    provider: str
    context_length: int
    input_per_1m: float
    output_per_1m: float
    reference_input_per_1m: float | None = None
    reference_output_per_1m: float | None = None
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_catalog_entry(cls, entry: dict, reference: dict | None = None) -> ModelInfo:
        pricing = entry.get("pricing") or {}
        ref = reference or {}
        return cls(
            id=str(entry.get("id") or ""),
            name=str(entry.get("name") or entry.get("id") or ""),
            provider=str(entry.get("provider") or ""),
            context_length=int(entry.get("context_length") or 0),
            input_per_1m=_per_token_to_per_1m(pricing.get("prompt")),
            output_per_1m=_per_token_to_per_1m(pricing.get("completion")),
            reference_input_per_1m=ref.get("input"),
            reference_output_per_1m=ref.get("output"),
            raw=entry,
        )

    @property
    def estimated_discount_pct(self) -> float | None:
        """Estimated buyer discount below the reference list price, or None if unknown.

        Mirrors upstream ``estimated_discount_pct = (1 - buyer_cost / direct_cost) * 100``
        using output pricing, which dominates most bills. Reference price is the
        highest provider list price seen in /v1/prices (conservative).
        """
        if self.reference_output_per_1m is None or self.reference_output_per_1m <= 0:
            return None
        return (1 - self.output_per_1m / self.reference_output_per_1m) * 100

    def to_admin_row(self, failed: list[str]) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "provider": self.provider,
            "context_length": self.context_length,
            "input_per_1m": round(self.input_per_1m, 6),
            "output_per_1m": round(self.output_per_1m, 6),
            "reference_input_per_1m": self.reference_input_per_1m,
            "reference_output_per_1m": self.reference_output_per_1m,
            "estimated_discount_pct": (
                round(self.estimated_discount_pct, 1)
                if self.estimated_discount_pct is not None
                else None
            ),
            "passed": not failed,
            "failed_filters": failed,
        }


def evaluate(model: ModelInfo, filters: FilterConfig) -> list[str]:
    """Return the ids of filters this model fails (empty list = passes)."""
    failed: list[str] = []

    if filters.enable_models_deny and model.id in filters.deny_models:
        failed.append("models_deny")

    if (
        filters.enable_models_allow
        and filters.allow_models
        and model.id not in filters.allow_models
    ):
        failed.append("models_allow")

    if filters.enable_providers_allow and filters.allow_providers:
        allowed = {p.strip().lower() for p in filters.allow_providers}
        if model.provider.strip().lower() not in allowed:
            failed.append("providers_allow")

    # Price caps are inclusive maxima ("at most"): a price equal to the cap
    # passes, matching the max_* config names. A price of 0 always passes.
    if filters.enable_price_input and model.input_per_1m > filters.max_input_per_1m + CAP_EPSILON:
        failed.append("price_input")

    if (
        filters.enable_price_output
        and model.output_per_1m > filters.max_output_per_1m + CAP_EPSILON
    ):
        failed.append("price_output")

    if filters.enable_discount:
        discount = model.estimated_discount_pct
        # Unknown reference price cannot prove a discount; upstream skips such
        # offers too, so treat it as a local failure only when a cap is set > 0.
        if discount is None:
            if filters.min_discount_pct > 0:
                failed.append("discount")
        elif discount < filters.min_discount_pct:
            failed.append("discount")

    if filters.enable_context_min and model.context_length < filters.min_context_length:
        failed.append("context_min")

    return failed


def passes(model: ModelInfo, filters: FilterConfig) -> bool:
    return not evaluate(model, filters)


def evaluate_by_id(
    model_id: str, catalog: dict[str, ModelInfo], filters: FilterConfig
) -> tuple[bool, list[str]]:
    """Gate used per request. Fail-closed on unknown models unless allowed."""
    model = catalog.get(model_id)
    if model is None:
        if filters.allow_unknown_models:
            return True, []
        return False, [UNKNOWN_MODEL]
    failed = evaluate(model, filters)
    return (not failed), failed


def minimum_discount_path_prefix(filters: FilterConfig) -> str:
    """Upstream minimum-discount routing prefix, or '' when disabled/invalid.

    Grammar upstream accepts: min0..min100, integer only, no leading zeros.
    """
    if not filters.enable_discount:
        return ""
    n = filters.min_discount_pct
    if not isinstance(n, int) or n < 0 or n > 100:
        return ""
    return f"/min{n}"
