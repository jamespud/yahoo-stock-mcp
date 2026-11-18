"""Option volatility factors with explicit quote and availability semantics."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import numpy as np

from ...compiler import digest
from ..contracts import DatasetManifest
from ..data.catalog import DatasetStore
from ..options.contracts import OptionContract
from ..options.pricing import OptionMarketInputs, OptionPricingConfig, calculate_option_analytics
from ..options.quotes import OptionQuote
from ..options.volatility import realized_volatility

OPTION_FACTOR_VERSION = "option-factors-v1"


def _utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _quote_from_row(row) -> OptionQuote:
    from ..options.quotes import quote_from_dataset_row
    return quote_from_dataset_row(row)


def _spot_at(prices: list[dict], decision_at: datetime) -> float | None:
    candidates = [row for row in prices if _utc(row["available_at"]) <= decision_at]
    if not candidates:
        return None
    return float(max(candidates, key=lambda row: _utc(row["available_at"]))["close"])


def _interpolate_delta(rows, target):
    points = sorted(
        (abs(float(row["analytics"].delta)), float(row["quote"].implied_volatility))
        for row in rows
        if row["analytics"].delta is not None and row["quote"].implied_volatility is not None
    )
    if len(points) < 2:
        return None
    return float(np.interp(target, [p[0] for p in points], [p[1] for p in points]))


def compute_option_factors(
    option_dataset: DatasetManifest,
    *,
    store: DatasetStore,
    underlying_prices: dict[str, list[dict]],
    pricing_config: OptionPricingConfig | None = None,
    selected_outputs: list[str] | None = None,
    quote_tolerance_seconds: int = 60,
    checkpoint=None,
) -> DatasetManifest:
    if option_dataset.dataset_kind != "option_quotes":
        from ..errors import AlphaError, AlphaErrorCode
        raise AlphaError(AlphaErrorCode.DATASET_MISMATCH, "Option factors require an option_quotes dataset")
    rows = [_quote_from_row(row) for row in store.read_rows(option_dataset.dataset_id)]
    if type(quote_tolerance_seconds) is not int or not 0 <= quote_tolerance_seconds <= 3600:
        from ..errors import AlphaError, AlphaErrorCode
        raise AlphaError(AlphaErrorCode.DATASET_MISMATCH, "quote_tolerance_seconds must be in 0..3600")
    grouped: list[tuple[str, datetime, list[OptionQuote]]] = []
    by_underlying = defaultdict(list)
    for quote in rows:
        by_underlying[quote.contract.underlying_id].append(quote)
    tolerance = timedelta(seconds=quote_tolerance_seconds)
    for underlying, underlying_quotes in sorted(by_underlying.items()):
        ordered = sorted(underlying_quotes, key=lambda quote: quote.quote_at or quote.observed_at)
        bucket = []
        anchor = None
        for quote in ordered:
            stamp = quote.quote_at or quote.observed_at
            if anchor is not None and stamp - anchor > tolerance:
                grouped.append((underlying, anchor, bucket))
                bucket, anchor = [], None
            if anchor is None:
                anchor = stamp
            bucket.append(quote)
        if bucket:
            grouped.append((underlying, anchor, bucket))
    feature_rows: list[dict] = []
    for index, (underlying, decision, raw_quotes) in enumerate(grouped):
        # One contract can have several quotes inside the tolerance window; use its latest observation.
        latest = {}
        for quote in raw_quotes:
            latest[quote.contract.contract_id] = quote
        quotes = list(latest.values())
        if checkpoint:
            checkpoint(index / max(1, len(groups)))
        prices = underlying_prices.get(underlying, [])
        spot = _spot_at(prices, decision)
        if spot is None:
            continue
        config = pricing_config or OptionPricingConfig(valuation_at=decision, risk_free_rate=0.04, dividend_yield=0.0, model="binomial_crr", steps=64)
        if config.valuation_at != decision:
            config = OptionPricingConfig(**{**config.model_dump(), "valuation_at": decision})
        available_at = max(quote.available_at for quote in quotes)
        factor_decision = available_at
        closes = [float(row["close"]) for row in prices if _utc(row["available_at"]) <= decision]
        rv = realized_volatility(closes[-25:])
        by_expiry = defaultdict(list)
        analytic_rows = []
        for quote in quotes:
            analytics = calculate_option_analytics(quote.contract, quote, OptionMarketInputs(spot=spot, observed_at=min(decision, quote.observed_at)), config)
            enriched = {"quote": quote, "analytics": analytics, "_delta": analytics.delta}
            analytic_rows.append(enriched)
            by_expiry[quote.contract.expiration].append(enriched)
            for name in ("delta", "gamma", "vega", "theta"):
                value = getattr(analytics, name)
                feature_rows.append({
                    "decision_at": factor_decision, "instrument_id": quote.contract.contract_id, "factor_name": name,
                    "factor_value": value, "feature_available_at": available_at,
                    "eligible": value is not None, "quality_flags": [] if value is not None else analytics.diagnostics,
                    "factor_version": OPTION_FACTOR_VERSION,
                })
        atm_by_expiry = {}
        for expiration, expiry_rows in sorted(by_expiry.items()):
            strike_groups = defaultdict(list)
            for row in expiry_rows:
                strike_groups[row["quote"].contract.strike].append(row)
            atm_strike = min(strike_groups, key=lambda strike: abs(strike - spot))
            atm_rows = strike_groups[atm_strike]
            ivs = [row["quote"].implied_volatility for row in atm_rows if row["quote"].implied_volatility is not None]
            atm_iv = float(np.mean(ivs)) if ivs else None
            atm_by_expiry[expiration] = atm_iv
            calls = [row for row in expiry_rows if row["quote"].contract.option_type == "CALL"]
            puts = [row for row in expiry_rows if row["quote"].contract.option_type == "PUT"]
            def complete_sum(rows, field):
                values = [getattr(row["quote"], field) for row in rows]
                return None if not values or any(value is None for value in values) else sum(values)
            call_oi = complete_sum(calls, "open_interest")
            put_oi = complete_sum(puts, "open_interest")
            call_volume = complete_sum(calls, "volume")
            put_volume = complete_sum(puts, "volume")
            spreads = []
            for row in expiry_rows:
                quote = row["quote"]
                if quote.bid and quote.ask and quote.ask > quote.bid:
                    spreads.append((quote.ask - quote.bid) / ((quote.ask + quote.bid) / 2))
            put25 = _interpolate_delta(puts, 0.25)
            call25 = _interpolate_delta(calls, 0.25)
            skew = put25 - call25 if put25 is not None and call25 is not None else None
            expiry_key = expiration.strftime("%Y%m%d")
            values = {
                f"atm_iv__{expiry_key}": atm_iv,
                f"iv_skew__{expiry_key}": skew,
                f"put_call_oi_ratio__{expiry_key}": (put_oi / call_oi) if call_oi and put_oi is not None else None,
                f"put_call_volume_ratio__{expiry_key}": (put_volume / call_volume) if call_volume and put_volume is not None else None,
                f"relative_bid_ask_spread__{expiry_key}": float(np.mean(spreads)) if spreads else None,
            }
            for name, value in values.items():
                feature_rows.append({
                    "decision_at": factor_decision, "instrument_id": underlying, "factor_name": name,
                    "factor_value": value, "feature_available_at": available_at,
                    "eligible": value is not None, "quality_flags": [] if value is not None else ["INSUFFICIENT_HISTORY"],
                    "factor_version": OPTION_FACTOR_VERSION,
                })
        if len(atm_by_expiry) >= 2:
            values = list(atm_by_expiry.values())
            term = values[0] - values[-1] if values[0] is not None and values[-1] is not None else None
        else:
            term = None
        aggregate = {
            "iv_term_structure": term,
            "realized_volatility": rv,
            "iv_rv_spread": (next(iter(atm_by_expiry.values())) - rv) if atm_by_expiry and next(iter(atm_by_expiry.values())) is not None and rv is not None else None,
        }
        for name, value in aggregate.items():
            feature_rows.append({
                "decision_at": factor_decision, "instrument_id": underlying, "factor_name": name,
                "factor_value": value, "feature_available_at": available_at,
                "eligible": value is not None, "quality_flags": [] if value is not None else ["INSUFFICIENT_HISTORY"],
                "factor_version": OPTION_FACTOR_VERSION,
            })
    if selected_outputs is not None:
        selected = set(selected_outputs)
        feature_rows = [row for row in feature_rows if row["factor_name"].split("__", 1)[0] in selected]
    if not feature_rows:
        from ..errors import AlphaError, AlphaErrorCode
        raise AlphaError(AlphaErrorCode.DATA_UNAVAILABLE, "No option factors could be computed")
    feature_rows.sort(key=lambda row: (row["decision_at"], row["instrument_id"], row["factor_name"]))
    spec_hash = digest({"version": OPTION_FACTOR_VERSION, "pricing": (pricing_config or {}).__str__()})
    return store.publish_feature_panel(
        feature_rows, parent_dataset_id=option_dataset.dataset_id, factor_spec_hash=spec_hash,
        instrument_ids=sorted({row["instrument_id"] for row in feature_rows}),
        start_at=min(row["decision_at"] for row in feature_rows), end_at=max(row["decision_at"] for row in feature_rows) + timedelta(microseconds=1),
        calendar_version="US-OPTION-EVENT-TIME-V1", price_basis=option_dataset.price_basis, checkpoint=checkpoint,
    )
