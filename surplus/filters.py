"""Filter evaluation: pure functions, no I/O, no globals.

Price semantics (owner-approved, verified against the live API):

- ``/v1/models`` pricing is the **list/reference** price in per-token decimal
  strings (docs: "multiply by 1M for $/1M tokens rate"). It is NOT what a buyer
  pays, so it is informational here.
- ``/api/marketplace`` carries the **actual best live offer** per model in
  microdollars per 1M tokens (``best_input_per_1m`` / ``best_output_per_1m``).
  Price caps are applied to these numbers - what the owner actually pays.
- Models with no live offer, or priced per media unit instead of per token
  (image / video / audio jobs), are rejected fail-closed.

``evaluate`` returns the ids of every rule that rejected a model so callers can
surface ``failed_filters``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import FilterConfig

# Rule ids. Keep in sync with the admin UI.
NO_MARKET_OFFER = "no_market_offer"
NOT_TOKEN_PRICED = "not_token_priced"
UNKNOWN_MODEL = "unknown_model"

FILTER_IDS = (
    NO_MARKET_OFFER,
    NOT_TOKEN_PRICED,
    "models_deny",
    "models_allow",
    "providers_allow",
    "price_input",
    "price_output",
    "discount",
    "context_min",
)

# Rules with no enable toggle: the owner chose fail-closed for anything that
# cannot be routed at a known per-token price.
ALWAYS_ON_RULES = (NO_MARKET_OFFER, NOT_TOKEN_PRICED)

# Price classes reported to the admin UI.
CLASS_TOKEN = "token"
CLASS_MEDIA = "media_unit"
CLASS_UNPRICED = "unpriced"
CLASS_NO_OFFER = "no_offer"

# Caps are inclusive maxima, and converted prices can land a hair off the decimal
# value they came from, so compare with this tolerance.
CAP_EPSILON = 1e-9

MICROUSD_PER_USD = 1_000_000


def _per_token_to_per_1m(value: object) -> float:
    """``/v1/models`` list price: per-token decimal string -> USD per 1M tokens."""
    try:
        return float(value or 0) * MICROUSD_PER_USD
    except (TypeError, ValueError):
        return 0.0


def _microd_per_1m_to_usd(value: object) -> float | None:
    """``/api/marketplace`` price: microdollars per 1M tokens -> USD per 1M tokens."""
    if value is None:
        return None
    try:
        return float(value) / MICROUSD_PER_USD
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class ModelInfo:
    """One catalog entry joined with its live marketplace offer."""

    id: str
    name: str
    provider: str
    context_length: int
    # list/reference prices, USD per 1M tokens (from /v1/models)
    list_input_per_1m: float = 0.0
    list_output_per_1m: float = 0.0
    # actual best live offer, USD per 1M tokens (from /api/marketplace)
    market_input_per_1m: float | None = None
    market_output_per_1m: float | None = None
    # media pricing (image / job / minute / 1K characters ...)
    media_unit: str | None = None
    media_unit_price_usd: float | None = None
    # marketplace metadata
    has_market_offer: bool = False
    best_live_discount_pct: float | None = None
    best_discount_pct: float | None = None
    healthy_seller_count: int = 0
    num_sellers: int = 0
    direct_input_per_1m: float | None = None
    direct_output_per_1m: float | None = None
    # reference list prices from /v1/prices (highest provider price = conservative)
    reference_input_per_1m: float | None = None
    reference_output_per_1m: float | None = None
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_catalog_entry(
        cls,
        entry: dict,
        reference: dict | None = None,
        market: dict | None = None,
    ) -> ModelInfo:
        pricing = entry.get("pricing") or {}
        ref = reference or {}
        mk = market or {}
        return cls(
            id=str(entry.get("id") or ""),
            name=str(entry.get("name") or entry.get("id") or ""),
            provider=str(entry.get("provider") or ""),
            context_length=int(entry.get("context_length") or 0),
            list_input_per_1m=_per_token_to_per_1m(pricing.get("prompt")),
            list_output_per_1m=_per_token_to_per_1m(pricing.get("completion")),
            market_input_per_1m=_microd_per_1m_to_usd(mk.get("input_micro_per_1m")),
            market_output_per_1m=_microd_per_1m_to_usd(mk.get("output_micro_per_1m")),
            media_unit=mk.get("media_unit"),
            media_unit_price_usd=_microd_per_1m_to_usd(mk.get("media_unit_price_micro")),
            has_market_offer=bool(mk),
            best_live_discount_pct=mk.get("best_live_discount_pct"),
            best_discount_pct=mk.get("best_discount_pct"),
            healthy_seller_count=int(mk.get("healthy_seller_count") or 0),
            num_sellers=int(mk.get("num_sellers") or 0),
            direct_input_per_1m=_microd_per_1m_to_usd(mk.get("direct_input_micro_per_1m")),
            direct_output_per_1m=_microd_per_1m_to_usd(mk.get("direct_output_micro_per_1m")),
            reference_input_per_1m=ref.get("input"),
            reference_output_per_1m=ref.get("output"),
            raw=entry,
        )

    @property
    def is_token_priced(self) -> bool:
        """True when the live offer quotes a per-token price we can cap."""
        if not self.has_market_offer:
            return False
        return bool((self.market_input_per_1m or 0) > 0 or (self.market_output_per_1m or 0) > 0)

    @property
    def price_class(self) -> str:
        if not self.has_market_offer:
            return CLASS_NO_OFFER
        if self.is_token_priced:
            return CLASS_TOKEN
        if self.media_unit or (self.media_unit_price_usd or 0) > 0:
            return CLASS_MEDIA
        return CLASS_UNPRICED

    @property
    def estimated_discount_pct(self) -> float | None:
        """Discount below list price, preferring the marketplace's own figure.

        Falls back to computing it from the list price and the live offer. None
        means "cannot be proven", which upstream also treats as unroutable when a
        minimum-discount floor is set.
        """
        if self.best_live_discount_pct is not None:
            return float(self.best_live_discount_pct)
        list_out = self.list_output_per_1m or (self.reference_output_per_1m or 0)
        market_out = self.market_output_per_1m
        if not list_out or list_out <= 0 or market_out is None:
            return None
        return (1 - market_out / list_out) * 100

    def to_admin_row(self, failed: list[str]) -> dict:
        discount = self.estimated_discount_pct
        return {
            "id": self.id,
            "name": self.name,
            "provider": self.provider,
            "context_length": self.context_length,
            "price_class": self.price_class,
            # what the owner actually pays
            "market_input_per_1m": (
                round(self.market_input_per_1m, 8)
                if self.market_input_per_1m is not None
                else None
            ),
            "market_output_per_1m": (
                round(self.market_output_per_1m, 8)
                if self.market_output_per_1m is not None
                else None
            ),
            # list price, for reference only
            "list_input_per_1m": round(self.list_input_per_1m, 6),
            "list_output_per_1m": round(self.list_output_per_1m, 6),
            "media_unit": self.media_unit,
            "media_unit_price_usd": self.media_unit_price_usd,
            "estimated_discount_pct": round(discount, 2) if discount is not None else None,
            "best_live_discount_pct": self.best_live_discount_pct,
            "healthy_seller_count": self.healthy_seller_count,
            "num_sellers": self.num_sellers,
            "passed": not failed,
            "failed_filters": failed,
        }


def evaluate(model: ModelInfo, filters: FilterConfig) -> list[str]:
    """Return the ids of every rule this model fails (empty list = passes)."""
    failed: list[str] = []

    # Routability first: no live offer, or not priced per token -> never forward.
    if not model.has_market_offer:
        failed.append(NO_MARKET_OFFER)
    elif not model.is_token_priced:
        failed.append(NOT_TOKEN_PRICED)

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

    # Price caps apply to the ACTUAL marketplace price; inclusive maxima.
    market_in = model.market_input_per_1m
    market_out = model.market_output_per_1m
    if (
        filters.enable_price_input
        and market_in is not None
        and market_in > filters.max_input_per_1m + CAP_EPSILON
    ):
        failed.append("price_input")

    if (
        filters.enable_price_output
        and market_out is not None
        and market_out > filters.max_output_per_1m + CAP_EPSILON
    ):
        failed.append("price_output")

    if filters.enable_discount:
        discount = model.estimated_discount_pct
        # An unprovable discount fails a floor, mirroring upstream behaviour.
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
