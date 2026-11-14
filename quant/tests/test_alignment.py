"""Decision-grid goldens: future/late observations must not repaint past samples."""
from datetime import timedelta

import pytest

from stock_quant.compiler import compile_strategy
from stock_quant.data import MarketBar, Snapshot
from stock_quant.runtime import Evaluator, run_backtest
from test_data import T


def bar(timeframe, start, end, close, *, delay=0, symbol="AAPL"):
    return MarketBar(symbol=symbol, timeframe=timeframe, exchange="XNAS",
                     start_time=T + timedelta(minutes=start), end_time=T + timedelta(minutes=end),
                     available_at=T + timedelta(minutes=end + delay), open=close, high=close,
                     low=close, close=close, volume=100., source="golden", adjustment="raw", dataset_version="v1")


def aligned_strategy(*, mixed=False):
    aligned = {"kind": "align", "value": {"kind": "ref", "symbol": "AAPL", "field": "close", "timeframe": "1d"}, "timeframe": "15m", "policy": "last_completed"}
    value = {"kind": "binary", "op": "add", "left": aligned,
             "right": {"kind": "ref", "symbol": "AAPL", "field": "close", "timeframe": "15m"}} if mixed else aligned
    return {"schema_version": "1.0", "name": "grid", "decision_timeframe": "15m",
            "signals": {"score": {"kind": "call", "operator": "sma", "args": [value], "params": {"period": 2}}},
            "entry": {"kind": "binary", "op": "gt", "left": {"kind": "signal", "name": "score"}, "right": {"kind": "const", "value": 15}},
            "exit": {"kind": "const", "value": False}}


def fixture(*, late=False, future=200.):
    rows = [bar("1d", -1440, 0, 10.), bar("1d", 0, 60, 30.), bar("1d", 60, 120, future)]
    rows += [bar("15m", start, start + 15, float(10 + start // 15)) for start in range(0, 105, 15)]
    if late:
        # A bar ending before the 30m sample is only published at minute 50.
        rows = [b for b in rows if not (b.timeframe == "15m" and b.end_time == T + timedelta(minutes=30))]
        rows.append(bar("15m", 15, 30, 100., delay=20))
    return Snapshot(rows, calendar_version="golden-grid-v1")


def test_rolling_alignment_samples_target_bars_and_source_publication():
    plan = compile_strategy(aligned_strategy())
    evaluator = Evaluator(plan, fixture())
    node = plan.named_nodes["score"]
    # Undefined at 15; then [10,10], [10,10], [10,30], [30,30].
    assert [evaluator.value(node, T + timedelta(minutes=m)) for m in (15, 30, 45, 60, 75)] == [None, 10., 10., 20., 30.]
    assert evaluator.value(node, T + timedelta(minutes=59)) == 10.
    assert evaluator.value(node, T + timedelta(minutes=75)) == Evaluator(plan, fixture(future=999999.)).value(node, T + timedelta(minutes=75))


def test_alignment_warmup_counts_target_bars_separately_from_daily_history():
    plan = compile_strategy(aligned_strategy())
    assert plan.requirements == [{"symbol": "AAPL", "field": "close", "timeframe": "1d", "warmup_bars": 1}]
    assert plan.grid_requirements == [{"timeframe": "15m", "warmup_bars": 2}]
    assert next(n for n in plan.nodes if n.kind == "call").lookback == 2


def test_late_target_data_does_not_repaint_mixed_rolling_samples():
    plan = compile_strategy(aligned_strategy(mixed=True))
    evaluator = Evaluator(plan, fixture(late=True))
    # The decision grid skips the stale 30m close arriving at 50; it does not
    # retroactively replace the already observed 15m and 45m values (20,22).
    assert evaluator.value(plan.named_nodes["score"], T + timedelta(minutes=50)) == 21.
    assert evaluator.value(plan.named_nodes["score"], T + timedelta(minutes=60)) == 32.5


def test_rolling_alignment_uses_execution_symbol_grid():
    plan = compile_strategy(aligned_strategy())
    # MSFT's decision schedule is offset from AAPL's.
    snapshot = Snapshot([*fixture().bars, bar("15m", 5, 20, 1., symbol="MSFT"), bar("15m", 55, 70, 1., symbol="MSFT")], calendar_version="grid")
    evaluator = Evaluator(plan, snapshot, symbol="MSFT")
    assert evaluator.value(plan.named_nodes["score"], T + timedelta(minutes=70)) == 20.


def test_missing_target_grid_is_explicit_and_cancellation_propagates():
    plan = compile_strategy(aligned_strategy())
    snapshot = Snapshot([bar("1d", -1440, 0, 10.)], calendar_version="grid")
    with pytest.raises(ValueError, match="DATA_UNAVAILABLE.*grid"):
        Evaluator(plan, snapshot)
    def cancel():
        raise InterruptedError("cancelled")
    with pytest.raises(InterruptedError):
        Evaluator(plan, fixture(), checkpoint=cancel)


def test_backtest_rolling_alignment_fills_strictly_after_first_defined_signal():
    plan = compile_strategy(aligned_strategy())
    result = run_backtest(plan, fixture(), symbol="AAPL", timeframe="15m")
    assert result["orders"][0]["submitted_at"] == (T + timedelta(minutes=60)).isoformat()
    assert result["fills"][0]["time"] == (T + timedelta(minutes=75)).isoformat()


def test_signal_api_reports_grid_warmup_and_accepts_execution_symbol(tmp_path):
    from stock_quant.jobs import JobStore
    with JobStore(tmp_path / "registry.db") as store:
        registered = store.register(aligned_strategy())
        data = fixture()
        imported = store.import_snapshot([b.model_dump(mode="json") for b in data.bars], data.calendar_version)
        early = store.evaluate(registered["strategy_id"], imported["data_snapshot_id"], (T + timedelta(minutes=15)).isoformat(), symbol="AAPL")
        assert not early["valid"]
        assert early["errors"][0]["code"] == "INSUFFICIENT_HISTORY"
        assert early["grid_dependencies"] == [{"symbol": "AAPL", "timeframe": "15m", "warmup_bars": 2, "available_bars": 1}]
        ready = store.evaluate(registered["strategy_id"], imported["data_snapshot_id"], (T + timedelta(minutes=60)).isoformat(), symbol="AAPL")
        assert ready["valid"]
        assert ready["signals"] == {"entry": True, "exit": False}


def test_same_timeframe_alignment_keeps_source_and_execution_grid_warmup_distinct():
    raw = aligned_strategy()
    raw["signals"]["score"]["args"][0]["value"]["timeframe"] = "15m"
    plan = compile_strategy(raw)
    assert plan.requirements[0]["warmup_bars"] == 1
    data = Snapshot([bar("15m", 0, 15, 10.), bar("15m", 5, 20, 1., symbol="MSFT"), bar("15m", 20, 35, 1., symbol="MSFT")], calendar_version="same-tf")
    assert Evaluator(plan, data, symbol="MSFT").value(plan.named_nodes["score"], T + timedelta(minutes=35)) == 10.


def test_two_thousand_grid_samples_do_not_materialize_quadratic_prefixes():
    plan = compile_strategy(aligned_strategy())
    data = Snapshot([bar("1d", -1440, 0, 10.), *[bar("15m", i * 15, (i + 1) * 15, 10.) for i in range(2000)]], calendar_version="grid-2000")
    checks = 0
    def checkpoint():
        nonlocal checks
        checks += 1
        assert checks < 100000
    evaluator = Evaluator(plan, data, checkpoint=checkpoint)
    assert evaluator.value(plan.named_nodes["score"], T + timedelta(minutes=30000)) == 10.


def test_split_does_not_restate_prior_aligned_samples():
    raw = aligned_strategy()
    raw["signals"]["score"]["args"][0]["value"]["adjustment"] = "split_asof"
    plan = compile_strategy(raw)
    rows = [bar("1d", -1440, 0, 10.), *[bar("15m", i * 15, (i + 1) * 15, 10.) for i in range(5)]]
    action = {"kind": "split", "id": "future-split", "symbol": "AAPL", "ratio": 2., "available_at": T, "effective_at": T + timedelta(minutes=50)}
    data = Snapshot(rows, calendar_version="split-grid", actions=[action])
    evaluator = Evaluator(plan, data)
    assert evaluator.value(plan.named_nodes["score"], T + timedelta(minutes=45)) == 10.
    assert evaluator.value(plan.named_nodes["score"], T + timedelta(minutes=60)) == 7.5
    assert evaluator.value(plan.named_nodes["score"], T + timedelta(minutes=75)) == 5.


def test_nested_alignment_and_mixed_rolling_use_only_eligible_child_prefixes():
    raw = aligned_strategy(mixed=True)
    # The hourly SMA must finish on the hourly decision grid, before the
    # outer alignment and SMA sample it on the 15m decision grid.
    daily = {"kind": "ref", "symbol": "AAPL", "field": "close", "timeframe": "1d"}
    hourly = {"kind": "align", "value": daily, "timeframe": "1h", "policy": "last_completed"}
    smooth = {"kind": "call", "operator": "sma", "args": [hourly], "params": {"period": 2}}
    raw["signals"]["score"]["args"][0]["left"]["value"] = smooth
    plan = compile_strategy(raw)
    rows = [bar("1d", -1440, 0, 10.), bar("1d", 0, 60, 30.), bar("1d", 60, 120, 1000.),
            bar("1h", -60, 0, 1.), bar("1h", 0, 60, 1.), bar("1h", 60, 120, 1.),
            *[bar("15m", i * 15, (i + 1) * 15, 2.) for i in range(9)]]
    data = Snapshot(rows, calendar_version="nested-grid")
    evaluator = Evaluator(plan, data)
    assert evaluator.value(plan.named_nodes["score"], T + timedelta(minutes=60)) is None
    assert evaluator.value(plan.named_nodes["score"], T + timedelta(minutes=75)) == 22.

    # Evaluate a later point first, then query a past clock again.
    assert evaluator.value(plan.named_nodes["score"], T + timedelta(minutes=135)) == 517.
    assert evaluator.value(plan.named_nodes["score"], T + timedelta(minutes=75)) == 22.


def test_unused_future_child_exception_does_not_invalidate_frozen_outer_samples():
    raw = aligned_strategy()
    daily = {"kind": "ref", "symbol": "AAPL", "field": "close", "timeframe": "1d", "adjustment": "split_asof"}
    raw["signals"]["score"]["args"][0]["value"] = {"kind": "align", "value": daily, "timeframe": "1h", "policy": "last_completed"}
    plan = compile_strategy(raw)
    rows = [bar("1d", -60, 0, 10.), bar("1d", 0, 60, 20.), bar("1h", -60, 0, 1.), bar("1h", 0, 60, 1.), bar("15m", 0, 15, 1.), bar("15m", 15, 30, 1.)]
    action = {"kind": "split", "id": "straddle", "symbol": "AAPL", "ratio": 2., "available_at": T, "effective_at": T + timedelta(minutes=45)}
    evaluator = Evaluator(plan, Snapshot(rows, calendar_version="unused-child", actions=[action]))
    assert evaluator.value(plan.named_nodes["score"], T + timedelta(minutes=30)) == 10.
    assert evaluator.value(plan.named_nodes["score"], T + timedelta(minutes=60)) == 10.
