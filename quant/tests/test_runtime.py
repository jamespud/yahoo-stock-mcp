from datetime import timedelta

import pytest

from stock_quant.compiler import compile_strategy
from stock_quant.data import MarketBar, Snapshot
from stock_quant.runtime import ExecutionConfig, run_backtest
from test_data import T


def fixture():
    rows = []
    for i, (open_, close) in enumerate([(10., 11.), (12., 13.), (14., 15.), (16., 17.)]):
        start = T + timedelta(days=i)
        rows.append(MarketBar(symbol="AAPL", timeframe="1d", exchange="XNAS", start_time=start, end_time=start + timedelta(hours=6), available_at=start + timedelta(hours=6), open=open_, high=close, low=open_, close=close, volume=1000., source="golden", adjustment="raw", dataset_version="1"))
    return Snapshot(rows, calendar_version="golden-v1")


def plan():
    return compile_strategy({"schema_version": "1.0", "name": "hold", "decision_timeframe": "1d", "entry": {"kind": "const", "value": True}, "exit": {"kind": "const", "value": False}})


def test_next_open_costs_and_cash_position_equity():
    result = run_backtest(plan(), fixture(), symbol="AAPL", timeframe="1d", config=ExecutionConfig(initial_cash=1000., quantity=10., commission_bps=10., slippage_bps=100.))
    fill = result["fills"][0]
    assert fill["time"] == (T + timedelta(days=1)).isoformat()
    assert fill["price"] == pytest.approx(12.12)
    assert fill["commission"] == pytest.approx(.1212)
    assert result["equity_curve"][-1]["equity"] == pytest.approx(1000 - 121.2 - .1212 + 170)
    for row in result["equity_curve"]:
        assert row["equity"] == pytest.approx(row["cash"] + row["quantity"] * row["mark"])
    assert result == run_backtest(plan(), fixture(), symbol="AAPL", timeframe="1d", config=ExecutionConfig(initial_cash=1000., quantity=10., commission_bps=10., slippage_bps=100.))


def test_delayed_signal_skips_already_elapsed_opens():
    s = fixture()
    rows = list(s.bars)
    rows[0] = rows[0].model_copy(update={"available_at": T + timedelta(days=1, hours=1)})
    result = run_backtest(plan(), Snapshot(rows, calendar_version="v1"), symbol="AAPL", timeframe="1d")
    assert result["fills"][0]["time"] == (T + timedelta(days=2)).isoformat()


def test_insufficient_cash_rejects_order_and_adjusted_prices_fail():
    result = run_backtest(plan(), fixture(), symbol="AAPL", timeframe="1d", config=ExecutionConfig(initial_cash=1.))
    assert not result["fills"]
    assert result["orders"][0]["status"] == "rejected"
    rows = [b.model_copy(update={"adjustment": "split"}) for b in fixture().bars]
    with pytest.raises(ValueError, match="raw"):
        run_backtest(plan(), Snapshot(rows, calendar_version="v1"), symbol="AAPL", timeframe="1d")


def test_ema_warmup_and_exit():
    raw = {"schema_version": "1.0", "name": "ema", "decision_timeframe": "1d", "entry": {"kind": "binary", "op": "gt", "left": {"kind": "ref", "symbol": "AAPL", "field": "close", "timeframe": "1d"}, "right": {"kind": "call", "operator": "ema", "args": [{"kind": "ref", "symbol": "AAPL", "field": "close", "timeframe": "1d"}], "params": {"period": 2}}}, "exit": {"kind": "const", "value": False}}
    result = run_backtest(compile_strategy(raw), fixture(), symbol="AAPL", timeframe="1d")
    assert result["fills"][0]["time"] == (T + timedelta(days=2)).isoformat()


def test_simultaneous_signal_and_open_does_not_fill():
    rows = list(fixture().bars)
    rows[0] = rows[0].model_copy(update={"available_at": rows[1].start_time})
    result = run_backtest(plan(), Snapshot(rows, calendar_version="v1"), symbol="AAPL", timeframe="1d")
    assert result["fills"][0]["time"] == (T + timedelta(days=2)).isoformat()


def test_wrong_execution_timeframe_fails():
    with pytest.raises(ValueError, match="TIMEFRAME_MISMATCH"):
        run_backtest(plan(), fixture(), symbol="AAPL", timeframe="15m")


def test_division_by_zero_does_not_generate_signal():
    raw = {"schema_version": "1.0", "name": "zero", "decision_timeframe": "1d", "entry": {"kind": "binary", "op": "gt", "left": {"kind": "binary", "op": "div", "left": {"kind": "const", "value": 1}, "right": {"kind": "const", "value": 0}}, "right": {"kind": "const", "value": 0}}, "exit": {"kind": "const", "value": False}}
    result = run_backtest(compile_strategy(raw), fixture(), symbol="AAPL", timeframe="1d")
    assert result["fills"] == []
    assert result["diagnostics"]["undefined_entry_decisions"] == 4


def test_period_keeps_warmup_but_never_trades_before_start():
    from stock_quant.runtime import Evaluator
    snapshot = fixture()
    start = T + timedelta(days=2)
    result = run_backtest(plan(), snapshot, symbol="AAPL", timeframe="1d", start=start, end=T + timedelta(days=4))
    assert result["orders"][0]["submitted_at"] == (start + timedelta(hours=6)).isoformat()
    assert result["fills"][0]["time"] == (T + timedelta(days=3)).isoformat()
    assert len(result["equity_curve"]) == 4


def test_multitimeframe_daily_value_unavailable_until_publication():
    from stock_quant.runtime import Evaluator
    from stock_quant.data import Snapshot
    from test_compiler import strategy
    s = strategy()
    # A day-long daily bar is not published until after the first 15m close.
    s["signals"]["trend"]["params"]["period"] = 1
    compiled = compile_strategy(s)
    daily = fixture().bars[0]
    first = daily.model_copy(update={"timeframe": "15m", "start_time": T, "end_time": T + timedelta(minutes=15), "available_at": T + timedelta(minutes=15), "open": 20., "high": 21., "low": 19., "close": 20.})
    second = first.model_copy(update={"start_time": T + timedelta(days=1), "end_time": T + timedelta(days=1, minutes=15), "available_at": T + timedelta(days=1, minutes=15)})
    snapshot = Snapshot([daily, first, second], calendar_version="test")
    evaluator = Evaluator(compiled, snapshot)
    assert evaluator.value(compiled.entry.id, first.available_at) is None
    assert evaluator.value(compiled.entry.id, second.available_at) is True


def test_shared_dag_analysis_is_linear_and_checks_cancellation():
    from stock_quant.runtime import Evaluator
    signals = {"s0": {"kind": "ref", "symbol": "AAPL", "field": "close", "timeframe": "1d"}}
    for i in range(1, 31):
        signals[f"s{i}"] = {"kind": "binary", "op": "add", "left": {"kind": "signal", "name": f"s{i-1}"}, "right": {"kind": "signal", "name": f"s{i-1}"}}
    raw = {"schema_version": "1.0", "name": "shared", "decision_timeframe": "1d", "signals": signals,
           "entry": {"kind": "binary", "op": "gt", "left": {"kind": "call", "operator": "sma", "args": [{"kind": "signal", "name": "s30"}], "params": {"period": 1}}, "right": {"kind": "const", "value": 0}}, "exit": {"kind": "const", "value": False}}
    compiled = compile_strategy(raw)
    checks = 0
    def probe():
        nonlocal checks
        checks += 1
        assert checks < 200  # An exponential graph walk must fail promptly.
    evaluator = Evaluator(compiled, fixture(), checkpoint=probe)
    assert evaluator.value(compiled.entry.id, T + timedelta(days=1)) is True
    assert checks < 4 * len(compiled.nodes)
    def cancelled():
        raise InterruptedError("cancelled")
    with pytest.raises(InterruptedError):
        Evaluator(compiled, fixture(), checkpoint=cancelled)


def test_cutoff_after_fill_keeps_terminal_account_and_costs():
    result = run_backtest(plan(), fixture(), symbol="AAPL", timeframe="1d", config=ExecutionConfig(initial_cash=1000., quantity=10., commission_bps=100.), end=T + timedelta(days=1, hours=1))
    assert len(result["fills"]) == 1
    terminal = result["final_account"]
    assert terminal["time"] == (T + timedelta(days=1)).isoformat()
    assert terminal["cash"] == pytest.approx(878.8)
    assert terminal["quantity"] == 10
    assert terminal["mark"] == 12
    assert terminal["equity"] == pytest.approx(998.8)
    assert result["summary"]["total_return"] == pytest.approx(-.0012)
    assert result["equity_curve"][-1] == terminal
