from datetime import date, datetime, timedelta, timezone

import pytest

from stock_quant.alpha.contracts import OptionDatasetRequest
from stock_quant.alpha.data.catalog import DatasetStore
from stock_quant.alpha.data.option_dataset import OptionSnapshotBatch, prepare_option_dataset
from stock_quant.alpha.factors.option_factors import compute_option_factors
from stock_quant.alpha.options.contracts import OptionContract, option_contract_id
from stock_quant.alpha.options.pricing import OptionMarketInputs, OptionPricingConfig, calculate_option_analytics
from stock_quant.alpha.options.quotes import OptionQuote


def _contract(option_type="CALL", strike=200.0, expiry=date(2026, 12, 18), multiplier=100):
    fields = dict(underlying_id="AAPL", option_type=option_type, strike=strike, expiration=expiry,
                  exercise_style="american", settlement_type="physical", multiplier=multiplier,
                  currency="USD", deliverable=f"{multiplier} shares AAPL", adjustment_version="standard")
    return OptionContract(contract_id=option_contract_id(**fields), **fields)


def _quote(contract, at, bid, ask, iv=None, oi=100, volume=10):
    return OptionQuote(contract=contract, observed_at=at, quote_at=at, available_at=at + timedelta(seconds=1),
                       bid=bid, ask=ask, bid_size=10, ask_size=10, last_price=(bid + ask) / 2,
                       last_trade_at=None, volume=volume, open_interest=oi, implied_volatility=iv,
                       source="thetadata", source_version="v3", quality_flags=[])


def test_quantlib_call_and_put_analytics():
    at = datetime(2026, 11, 18, 15, tzinfo=timezone.utc)
    config = OptionPricingConfig(valuation_at=at, risk_free_rate=0.04, dividend_yield=0.01, model="binomial_crr", steps=128)
    market = OptionMarketInputs(spot=200.0, observed_at=at)
    call = calculate_option_analytics(_contract("CALL"), _quote(_contract("CALL"), at, 8.0, 8.2, 0.30), market, config)
    put = calculate_option_analytics(_contract("PUT"), _quote(_contract("PUT"), at, 7.5, 7.7, 0.32), market, config)
    assert call.valid and put.valid
    assert call.delta > 0 and put.delta < 0
    assert call.gamma > 0 and put.vega > 0
    assert call.model == "binomial_crr"
    assert call.diagnostics == []


def test_pricing_returns_diagnostics_for_expired_and_no_solution_iv():
    at = datetime(2026, 12, 19, 15, tzinfo=timezone.utc)
    config = OptionPricingConfig(valuation_at=at, risk_free_rate=0.04, dividend_yield=0.0, model="binomial_crr", steps=32)
    market = OptionMarketInputs(spot=200.0, observed_at=at)
    contract = _contract(expiry=date(2026, 12, 18))
    expired = calculate_option_analytics(contract, _quote(contract, at, 0.0, 0.0, None), market, config)
    assert expired.valid is False
    assert "EXPIRED" in expired.diagnostics
    assert expired.delta is None

    future = _contract(expiry=date(2026, 12, 18))
    no_iv = calculate_option_analytics(future, _quote(future, datetime(2026, 11, 18, 15, tzinfo=timezone.utc), 0.0, 0.0, None),
                                       OptionMarketInputs(spot=200.0, observed_at=datetime(2026, 11, 18, 15, tzinfo=timezone.utc)),
                                       OptionPricingConfig(valuation_at=datetime(2026, 11, 18, 15, tzinfo=timezone.utc), risk_free_rate=0.04, dividend_yield=0.0, model="binomial_crr", steps=32))
    assert no_iv.valid is False
    assert "NO_SOLUTION_IV" in no_iv.diagnostics


def test_option_factors_include_atm_skew_term_structure_ratios_and_greeks(tmp_path):
    at = datetime(2026, 11, 18, 15, tzinfo=timezone.utc)
    expiries = (date(2026, 12, 18), date(2027, 1, 15))
    rows = []
    for expiry_index, expiry in enumerate(expiries):
        for strike, iv in ((180.0, 0.36 + expiry_index * 0.01), (200.0, 0.30 + expiry_index * 0.01), (220.0, 0.27 + expiry_index * 0.01)):
            call = _contract("CALL", strike, expiry)
            put = _contract("PUT", strike, expiry)
            rows.append(_quote(call, at, 4.0, 4.2, iv, oi=100, volume=10))
            rows.append(_quote(put, at, 3.8, 4.0, iv + 0.01, oi=120, volume=12))
    request = OptionDatasetRequest(source="thetadata", underlyings=["AAPL"], start=datetime(2026, 11, 1, tzinfo=timezone.utc),
                                   end=datetime(2026, 11, 20, tzinfo=timezone.utc), quote_policy="historical_bid_ask")
    class Provider:
        def read(self, request):
            return OptionSnapshotBatch(rows=rows, source_version="v3", quality={})
    store = DatasetStore(tmp_path / "alpha")
    dataset = prepare_option_dataset(request, store=store, provider=Provider())
    prices = {"AAPL": [{"available_at": at - timedelta(days=1), "close": 200.0 + i * 0.1} for i in range(25)]}
    factors = compute_option_factors(dataset, store=store, underlying_prices=prices)
    names = {row["factor_name"] for row in store.read_rows(factors.dataset_id)}
    required = {"iv_term_structure", "realized_volatility", "iv_rv_spread", "delta", "gamma", "vega", "theta"}
    assert required.issubset(names)
    for prefix in ("atm_iv:", "iv_skew:", "put_call_oi_ratio:", "put_call_volume_ratio:", "relative_bid_ask_spread:"):
        assert any(name.startswith(prefix) for name in names)
