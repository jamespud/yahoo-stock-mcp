from datetime import datetime, timezone, timedelta
from math import sqrt
import statistics

import pytest

from stock_quant.analytics import analyze

T = datetime(2026, 1, 1, tzinfo=timezone.utc)


def row(hours, value):
    return {"time": (T + timedelta(hours=hours)).isoformat(), "equity": value, "positions": {}}


def test_calendar_daily_returns_exclude_partial_intervals_from_sharpe():
    result = analyze([row(12, 110), row(24, 120), row(48, 108), row(60, 118)], initial_cash=100., start=T, fills=[], actions=[])
    returns = result["daily_returns"]
    assert [r["return"] for r in returns] == pytest.approx([.2, -.1, 118/108-1])
    assert [r["full_utc_day"] for r in returns] == [True, True, False]
    assert result["analytics"]["metrics"]["sharpe"] == pytest.approx(statistics.mean([.2, -.1]) / statistics.stdev([.2, -.1]) * sqrt(365))
    assert result["analytics"]["metrics"]["total_return"] == pytest.approx(.18)


def test_intraday_samples_do_not_create_extra_daily_returns_and_weekends_carry_marks():
    a = analyze([row(24, 110), row(72, 121)], initial_cash=100, start=T, fills=[], actions=[])
    b = analyze([row(6, 105), row(24, 110), row(36, 110), row(72, 121)], initial_cash=100, start=T, fills=[], actions=[])
    assert a["daily_returns"] == b["daily_returns"]
    assert [r["return"] for r in a["daily_returns"]] == pytest.approx([.1, 0., .1])


def test_flat_equity_has_undefined_sharpe_and_explicit_accounting_attribution():
    curve = [row(24, 100), row(48, 100)]
    curve[-1]["positions"] = {"AAPL": {"quantity": 2, "mark": 10, "basis": 23, "realized_pnl": 1}}
    result = analyze(curve, initial_cash=100, start=T, fills=[{"quantity": 2, "price": 10, "commission": 1., "side": "buy"}], actions=[{"kind": "dividend_entitlement", "amount": 2}])
    assert result["analytics"]["metrics"]["sharpe"] is None
    assert result["analytics"]["metrics"]["annualized_volatility"] == 0
    assert result["attribution"]["accounting_residual"] == 0
    assert result["attribution"]["commission_paid"] == 1
    assert result["attribution"]["turnover_to_initial_equity"] == .2


def test_zero_equity_no_nonfinite_metrics_and_short_history_nulls():
    result = analyze([row(1, 0)], initial_cash=100, start=T, fills=[], actions=[])
    assert result["analytics"]["metrics"]["cagr"] == -1
    assert result["analytics"]["metrics"]["sharpe"] is None
    assert result["analytics"]["metrics"]["annualized_volatility"] is None


def test_shared_broker_dividend_costs_and_delisting_reconcile():
    from stock_quant.compiler import compile_strategy
    from stock_quant.data import Snapshot
    from stock_quant.portfolio import run_portfolio
    from stock_quant.runtime import ExecutionConfig
    from test_runtime import fixture
    raw = {"schema_version": "1.0", "name": "hold", "decision_timeframe": "1d", "entry": {"kind": "const", "value": True}, "exit": {"kind": "const", "value": False}}
    bars = fixture().bars
    actions = [{"kind": "cash_dividend", "id": "income", "symbol": "AAPL", "amount": .5, "currency": "USD", "effective_at": bars[2].start_time.isoformat(), "available_at": bars[0].start_time.isoformat(), "pay_at": bars[3].start_time.isoformat()},
               {"kind": "cash_delisting", "id": "delist", "symbol": "AAPL", "amount": 12., "currency": "USD", "effective_at": bars[3].end_time.isoformat(), "available_at": bars[0].start_time.isoformat()}]
    result = run_portfolio({"AAPL": compile_strategy(raw)}, Snapshot(list(bars), calendar_version="metrics", actions=actions), config=ExecutionConfig(initial_cash=1000, quantity=2, commission_bps=10, slippage_bps=10))
    assert result["attribution"]["accounting_residual"] == pytest.approx(0, abs=1e-10)
    assert result["attribution"]["dividend_income"] == 1
    assert result["attribution"]["commission_paid"] > 0
    assert result["summary"]["total_return"] == result["analytics"]["metrics"]["total_return"]
    assert result["analytics"]["elapsed_seconds"] > 0


def test_attribution_overflow_is_reported_as_null_without_breaking_json():
    import json
    fills = [{"quantity": 1., "price": 1e308, "commission": 0., "side": "buy"}] * 3
    result = analyze([row(48, 100)], initial_cash=100, start=T, fills=fills, actions=[])
    assert result["attribution"]["gross_traded_notional"] is None
    assert result["attribution"]["turnover_to_initial_equity"] is None
    json.dumps(result, allow_nan=False)
    result = analyze([row(48, 1e-200)], initial_cash=1e-200, start=T, fills=[{"quantity": 1., "price": 1e200, "commission": 0.}], actions=[])
    assert result["attribution"]["turnover_to_initial_equity"] is None
    json.dumps(result, allow_nan=False)


def test_supported_datetime_ceiling_does_not_overflow_daily_sampling():
    start = datetime(9999, 12, 30, 12, tzinfo=timezone.utc)
    end = datetime(9999, 12, 31, 12, tzinfo=timezone.utc)
    result = analyze([{"time": end.isoformat(), "equity": 100., "positions": {}}], initial_cash=100, start=start, fills=[], actions=[])
    assert len(result["daily_returns"]) == 2
    assert result["analytics"]["metrics"]["total_return"] == 0
    result = analyze([{"time": end.isoformat(), "equity": 100., "positions": {}}], initial_cash=100, start=end, fills=[], actions=[])
    assert result["daily_returns"] == []


def test_gap_marks_new_fills_existing_holdings_and_delayed_close_clocks():
    from stock_quant.compiler import compile_strategy
    from stock_quant.data import Snapshot
    from stock_quant.portfolio import run_portfolio
    from stock_quant.runtime import ExecutionConfig
    from test_data import bar
    raw = {"schema_version": "1.0", "name": "hold", "decision_timeframe": "1h", "entry": {"kind": "const", "value": True}, "exit": {"kind": "const", "value": False}}
    bars = []
    for i, price in enumerate([100., 100., 200., 300.]):
        bars.append(bar().model_copy(update={"timeframe": "1h", "start_time": T + timedelta(hours=i), "end_time": T + timedelta(hours=i+1),
                                          "available_at": T + timedelta(hours=5 if i == 2 else i+1),
                                          "open": price, "close": 50. if i == 2 else 100., "high": price, "low": 50.}))
    result = run_portfolio({"AAPL": compile_strategy(raw)}, Snapshot(bars, calendar_version="quotes"), config=ExecutionConfig(initial_cash=1000., quantity=1.))
    rows = {r["time"]: r for r in result["equity_curve"]}
    purchase = rows[(T + timedelta(hours=2)).isoformat()]
    assert purchase["equity"] == 1000.  # New shares valued at their actual raw fill.
    assert purchase["quotes"]["AAPL"]["kind"] == "open"  # Equal source-time precedence.
    assert rows[(T + timedelta(hours=3)).isoformat()]["equity"] == 1100.  # Gap without a fill.
    assert len(result["fills"]) == 1
    assert rows[(T + timedelta(hours=4)).isoformat()]["equity"] == 900.  # Observed reversion.
    late = rows[(T + timedelta(hours=5)).isoformat()]
    assert late["equity"] == 900.
    assert late["quotes"]["AAPL"]["source_time"] == (T + timedelta(hours=4)).isoformat()
    assert late["quotes"]["AAPL"]["observed_at"] == (T + timedelta(hours=4)).isoformat()


def test_midnight_mark_cannot_consume_later_observation():
    result = analyze([row(23, 100), row(25, 200)], initial_cash=100, start=T, fills=[], actions=[])
    assert result["daily_returns"][0]["equity"] == 100
    assert result["daily_returns"][1]["equity"] == 200
