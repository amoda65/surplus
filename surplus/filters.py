"""Filter evaluation: pure functions, no I/O, no globals.

Price semantics (owner-approved, verified against the live API):

- ``/v1/models`` pricing is the **list/reference** price in per-token decimal
  strings (docs: "multiply by 1M for $/1M tokens rate"). It is NOT what a buyer
  pays, so it is informational here.
- ``/api/markets`` carries the **actual best live offer** per model in
  microdollars per 1M tokens (``best_input_per_1m`` / ``best_output_per_1m``),
  plus performance, liquidity and per-provider (``trusted`` flag) breakdowns.
  Price caps are applied to these numbers - what the owner actually pays.
- Models with no live offer, or priced per media unit instead of per token
  (image / video / audio jobs), are rejected fail-closed.
- Quality filters (uptime / TTFT / TPS) fail **open** when the marketplace has
  no measurement for a model: "not measured" is not "bad".

``evaluate`` returns the ids of every rule that rejected a model so callers can
surface ``failed_filters``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import FilterConfig

# Rule ids. Keep in sync with the admin UI.
NO_MARKET_OFFER = "no_market_offer"
NOT_TOKEN_PRICED = "not_token_priced"
NO_TRUSTED_OFFER = "no_trusted_offer"
UNKNOWN_MODEL = "unknown_model"

FILTER_IDS = (
    NO_MARKET_OFFER,
    NOT_TOKEN_PRICED,
    NO_TRUSTED_OFFER,
    "models_deny",
    "models_allow",
    "providers_allow",
    "price_input",
    "price_output",
    "discount",
    "context_min",
    "min_uptime",
    "max_ttft",
    "min_tps",
    "min_sellers",
    "min_uncapped",
    "cache_write_cap",
    "require_features",
    "exclude_tightening",
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
    """``/api/markets`` price: microdollars per 1M tokens -> USD per 1M tokens."""
    if value is None:
        return None
    try:
        return float(value) / MICROUSD_PER_USD
    except (TypeError, ValueError):
        return None


def _num(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _trusted_best_prices(providers: list | None) -> tuple[float | None, float | None]:
    """Best (lowest > 0) input/output USD-per-1M offered by a trusted provider."""
    best_in: float | None = None
    best_out: float | None = None
    for entry in providers or []:
        if not isinstance(entry, dict) or not entry.get("trusted"):
            continue
        price_in = _microd_per_1m_to_usd(entry.get("best_input_per_1m"))
        price_out = _microd_per_1m_to_usd(entry.get("best_output_per_1m"))
        if price_in and price_in > 0:
            best_in = price_in if best_in is None else min(best_in, price_in)
        if price_out and price_out > 0:
            best_out = price_out if best_out is None else min(best_out, price_out)
    return best_in, best_out


def _compact_providers(providers: list | None) -> tuple[dict, ...]:
    """Per-provider offer summary for the admin UI (prices in USD per 1M)."""
    rows = []
    for entry in providers or []:
        if not isinstance(entry, dict):
            continue
        rows.append(
            {
                "provider": entry.get("provider"),
                "trusted": bool(entry.get("trusted")),
                "input_per_1m": _microd_per_1m_to_usd(entry.get("best_input_per_1m")),
                "output_per_1m": _microd_per_1m_to_usd(entry.get("best_output_per_1m")),
                "healthy_sellers": _int(entry.get("healthy_seller_count")),
                "uncapped_offers": _int(entry.get("uncapped_discounted_offer_count")),
            }
        )
    rows.sort(key=lambda r: (not r["trusted"], r["output_per_1m"] is None, r["output_per_1m"] or 0))
    return tuple(rows)


@dataclass(frozen=True)
class ModelInfo:
    """One catalog entry joined with its live market offer."""

    id: str
    name: str
    provider: str
    context_length: int
    # list/reference prices, USD per 1M tokens (from /v1/models)
    list_input_per_1m: float = 0.0
    list_output_per_1m: float = 0.0
    # actual best live offer, USD per 1M tokens (from /api/markets)
    market_input_per_1m: float | None = None
    market_output_per_1m: float | None = None
    # best offer restricted to trusted providers, USD per 1M tokens
    trusted_input_per_1m: float | None = None
    trusted_output_per_1m: float | None = None
    # media pricing (image / job / minute / 1K characters ...)
    media_unit: str | None = None
    media_unit_price_usd: float | None = None
    # market metadata
    has_market_offer: bool = False
    best_live_discount_pct: float | None = None
    best_discount_pct: float | None = None
    best_trusted_offer_discount_pct: float | None = None
    healthy_seller_count: int = 0
    num_sellers: int = 0
    uncapped_offer_count: int = 0
    liquidity_usd: float | None = None
    direct_input_per_1m: float | None = None
    direct_output_per_1m: float | None = None
    # cache economics, USD per 1M tokens
    cache_read_per_1m: float | None = None
    cache_write_per_1m: float | None = None
    cache_hit_rate_pct: float | None = None
    # quality measurements (None = marketplace has no data; filters fail open)
    uptime_pct: float | None = None
    ttft_p50_ms: float | None = None
    tps_p50: float | None = None
    tps_samples: int = 0
    ttft_samples: int = 0
    # activity
    requests_24h: int = 0
    volume_24h_usd: float | None = None
    # discount trend
    trend_direction: str | None = None
    trend_current_pct: float | None = None
    trend_previous_pct: float | None = None
    # capabilities (from /v1/models)
    modality: str = ""
    features: tuple[str, ...] = ()
    parameters: tuple[str, ...] = ()
    max_completion_tokens: int = 0
    is_moderated: bool = False
    # reference list prices from /v1/prices (highest provider price = conservative)
    reference_input_per_1m: float | None = None
    reference_output_per_1m: float | None = None
    providers: tuple[dict, ...] = ()
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
        arch = entry.get("architecture") or {}
        top = entry.get("top_provider") or {}
        trend = mk.get("discount_trend") or {}
        trusted_in, trusted_out = _trusted_best_prices(mk.get("providers"))
        return cls(
            id=str(entry.get("id") or ""),
            name=str(entry.get("name") or entry.get("id") or ""),
            provider=str(entry.get("provider") or ""),
            context_length=_int(entry.get("context_length")),
            list_input_per_1m=_per_token_to_per_1m(pricing.get("prompt")),
            list_output_per_1m=_per_token_to_per_1m(pricing.get("completion")),
            market_input_per_1m=_microd_per_1m_to_usd(mk.get("input_micro_per_1m")),
            market_output_per_1m=_microd_per_1m_to_usd(mk.get("output_micro_per_1m")),
            trusted_input_per_1m=trusted_in,
            trusted_output_per_1m=trusted_out,
            media_unit=mk.get("media_unit"),
            media_unit_price_usd=_microd_per_1m_to_usd(mk.get("media_unit_price_micro")),
            has_market_offer=bool(mk),
            best_live_discount_pct=_num(mk.get("best_live_discount_pct")),
            best_discount_pct=_num(mk.get("best_discount_pct")),
            best_trusted_offer_discount_pct=_num(mk.get("best_trusted_offer_discount_pct")),
            healthy_seller_count=_int(mk.get("healthy_seller_count")),
            num_sellers=_int(mk.get("num_sellers")),
            uncapped_offer_count=_int(mk.get("uncapped_offer_count")),
            liquidity_usd=_microd_per_1m_to_usd(mk.get("liquidity_micro_usd")),
            direct_input_per_1m=_microd_per_1m_to_usd(mk.get("direct_input_micro_per_1m")),
            direct_output_per_1m=_microd_per_1m_to_usd(mk.get("direct_output_micro_per_1m")),
            cache_read_per_1m=_microd_per_1m_to_usd(mk.get("cache_read_micro_per_1m")),
            cache_write_per_1m=_microd_per_1m_to_usd(mk.get("cache_write_micro_per_1m")),
            cache_hit_rate_pct=_num(mk.get("cache_hit_rate_pct")),
            uptime_pct=_num(mk.get("uptime_pct")),
            ttft_p50_ms=_num(mk.get("ttft_p50_ms")),
            tps_p50=_num(mk.get("tps_p50")),
            tps_samples=_int(mk.get("tps_samples")),
            ttft_samples=_int(mk.get("ttft_samples")),
            requests_24h=_int(mk.get("requests_24h")),
            volume_24h_usd=_microd_per_1m_to_usd(mk.get("volume_24h_micro_usd")),
            trend_direction=(trend.get("direction") if isinstance(trend, dict) else None),
            trend_current_pct=_num(
                trend.get("current_discount_pct") if isinstance(trend, dict) else None
            ),
            trend_previous_pct=_num(
                trend.get("previous_discount_pct") if isinstance(trend, dict) else None
            ),
            modality=str(arch.get("modality") or ""),
            features=tuple(str(f) for f in (entry.get("supported_features") or [])),
            parameters=tuple(str(p) for p in (entry.get("supported_parameters") or [])),
            max_completion_tokens=_int(top.get("max_completion_tokens")),
            is_moderated=bool(top.get("is_moderated")),
            reference_input_per_1m=_num(ref.get("input")),
            reference_output_per_1m=_num(ref.get("output")),
            providers=_compact_providers(mk.get("providers")),
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

    def effective_prices(self, filters: FilterConfig) -> tuple[float | None, float | None]:
        """Prices the caps apply to: trusted-only offers when that mode is on."""
        if filters.enable_trusted_only:
            return self.trusted_input_per_1m, self.trusted_output_per_1m
        return self.market_input_per_1m, self.market_output_per_1m

    def effective_discount_pct(self, filters: FilterConfig) -> float | None:
        """Discount the floor applies to; trusted-only swaps the source figure."""
        if filters.enable_trusted_only:
            return self.best_trusted_offer_discount_pct
        return self.estimated_discount_pct

    def to_admin_row(self, failed: list[str]) -> dict:
        discount = self.estimated_discount_pct
        return {
            "id": self.id,
            "name": self.name,
            "provider": self.provider,
            "context_length": self.context_length,
            "modality": self.modality,
            "features": list(self.features),
            "price_class": self.price_class,
            # what the owner actually pays
            "market_input_per_1m": _round6(self.market_input_per_1m),
            "market_output_per_1m": _round6(self.market_output_per_1m),
            # best trusted-provider offer
            "trusted_input_per_1m": _round6(self.trusted_input_per_1m),
            "trusted_output_per_1m": _round6(self.trusted_output_per_1m),
            # list price, for reference only
            "list_input_per_1m": round(self.list_input_per_1m, 6),
            "list_output_per_1m": round(self.list_output_per_1m, 6),
            "media_unit": self.media_unit,
            "media_unit_price_usd": self.media_unit_price_usd,
            "estimated_discount_pct": round(discount, 2) if discount is not None else None,
            "best_live_discount_pct": self.best_live_discount_pct,
            "best_trusted_offer_discount_pct": self.best_trusted_offer_discount_pct,
            "healthy_seller_count": self.healthy_seller_count,
            "num_sellers": self.num_sellers,
            "uncapped_offer_count": self.uncapped_offer_count,
            "liquidity_usd": self.liquidity_usd,
            "cache_read_per_1m": _round6(self.cache_read_per_1m),
            "cache_write_per_1m": _round6(self.cache_write_per_1m),
            "uptime_pct": self.uptime_pct,
            "ttft_p50_ms": self.ttft_p50_ms,
            "tps_p50": self.tps_p50,
            "requests_24h": self.requests_24h,
            "volume_24h_usd": self.volume_24h_usd,
            "trend_direction": self.trend_direction,
            "trend_current_pct": self.trend_current_pct,
            "max_completion_tokens": self.max_completion_tokens,
            "passed": not failed,
            "failed_filters": failed,
        }

    def to_detail(self, failed: list[str]) -> dict:
        """Full model dossier for the admin drawer."""
        detail = self.to_admin_row(failed)
        detail.update(
            {
                "description": str(self.raw.get("description") or ""),
                "parameters": list(self.parameters),
                "providers": list(self.providers),
                "is_moderated": self.is_moderated,
                "cache_hit_rate_pct": self.cache_hit_rate_pct,
                "tps_samples": self.tps_samples,
                "ttft_samples": self.ttft_samples,
                "trend_previous_pct": self.trend_previous_pct,
                "direct_input_per_1m": _round6(self.direct_input_per_1m),
                "direct_output_per_1m": _round6(self.direct_output_per_1m),
                "reference_input_per_1m": self.reference_input_per_1m,
                "reference_output_per_1m": self.reference_output_per_1m,
            }
        )
        return detail


def _round6(value: float | None) -> float | None:
    return None if value is None else round(value, 6)


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

    # trusted_only: a model nobody trusted serves is not routable for us.
    if filters.enable_trusted_only:
        if model.has_market_offer and model.is_token_priced:
            if model.trusted_input_per_1m is None and model.trusted_output_per_1m is None:
                failed.append(NO_TRUSTED_OFFER)

    # Price caps apply to the ACTUAL marketplace price; inclusive maxima.
    price_in, price_out = model.effective_prices(filters)
    if (
        filters.enable_price_input
        and price_in is not None
        and price_in > filters.max_input_per_1m + CAP_EPSILON
    ):
        failed.append("price_input")

    if (
        filters.enable_price_output
        and price_out is not None
        and price_out > filters.max_output_per_1m + CAP_EPSILON
    ):
        failed.append("price_output")

    if filters.enable_discount:
        discount = model.effective_discount_pct(filters)
        # An unprovable discount fails a floor, mirroring upstream behaviour.
        if discount is None:
            if filters.min_discount_pct > 0:
                failed.append("discount")
        elif discount < filters.min_discount_pct:
            failed.append("discount")

    if filters.enable_context_min and model.context_length < filters.min_context_length:
        failed.append("context_min")

    # Quality filters: missing measurements fail open (not measured != bad).
    if (
        filters.enable_min_uptime
        and model.uptime_pct is not None
        and model.uptime_pct < filters.min_uptime_pct - CAP_EPSILON
    ):
        failed.append("min_uptime")

    if (
        filters.enable_max_ttft
        and model.ttft_p50_ms is not None
        and model.ttft_p50_ms > filters.max_ttft_ms + CAP_EPSILON
    ):
        failed.append("max_ttft")

    if (
        filters.enable_min_tps
        and model.tps_p50 is not None
        and model.tps_p50 < filters.min_tps - CAP_EPSILON
    ):
        failed.append("min_tps")

    # Market depth: thin markets flip to no_market_offer mid-request.
    if filters.enable_min_sellers and model.healthy_seller_count < filters.min_healthy_sellers:
        failed.append("min_sellers")

    if (
        filters.enable_min_uncapped
        and model.uncapped_offer_count < filters.min_uncapped_offers
    ):
        failed.append("min_uncapped")

    # Cache writes often cost ~10x the input price; cap them separately.
    if (
        filters.enable_cache_write_cap
        and model.cache_write_per_1m is not None
        and model.cache_write_per_1m > filters.max_cache_write_per_1m + CAP_EPSILON
    ):
        failed.append("cache_write_cap")

    if filters.enable_require_features and filters.require_features:
        have = {f.lower() for f in model.features}
        want = {f.strip().lower() for f in filters.require_features if f.strip()}
        if want and not want.issubset(have):
            failed.append("require_features")

    if filters.enable_exclude_tightening and model.trend_direction == "tightening":
        failed.append("exclude_tightening")

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
