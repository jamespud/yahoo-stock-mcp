"""Independent vector/PIT signal goldens and shared-broker consistency."""
from datetime import timedelta

from hypothesis import given, settings, strategies as st
import pytest

from stock_quant.compiler import compile_strategy
from stock_quant.data import Snapshot
from stock_quant.runtime import Evaluator, ExecutionConfig, run_backtest
from test_alignment import bar
from test_data import T


def strategy(op="sma", period=2):
    return {"schema_version": "1.0", "name": "vector", "decision_timeframe": "15m",
            "signals": {"score": {"kind": "call", "operator": op,
                                  "args": [{"kind": "ref", "symbol": "AAPL", "field": "close", "timeframe": "15m"}], "params": {"period": period}}},
            "entry": {"kind": "binary", "op": "gt", "left": {"kind": "signal", "name": "score"}, "right": {"kind": "const", "value": 12}},
            "exit": {"kind": "binary", "op": "lt", "left": {"kind": "signal", "name": "score"}, "right": {"kind": "const", "value": 12}}}


def snapshot(prices):
    return Snapshot([bar("15m", i * 15, (i + 1) * 15, float(p)) for i, p in enumerate(prices)], calendar_version="vector-golden")


@pytest.mark.parametrize("op,want", [("sma", [None, 11., 13., 12.]), ("ema", [None, 11., 13., 11.]), ("rsi", [None, None, 100., 100 / 3])])
def test_vector_indicator_golden_and_no_scalar_fallback(op, want, monkeypatch):
    from stock_quant.vector import VectorEvaluator
    plan = compile_strategy(strategy(op))
    def forbidden(*args):
        raise AssertionError("vector backend delegated to scalar evaluator")
    monkeypatch.setattr(Evaluator, "value", forbidden)
    evaluator = VectorEvaluator(plan, snapshot([10, 12, 14, 10]), symbol="AAPL")
    got = [evaluator.value(plan.named_nodes["score"], T + timedelta(minutes=m)) for m in (15, 30, 45, 60)]
    for actual, expected in zip(got, want):
        assert actual == pytest.approx(expected) if expected is not None else actual is None
    assert evaluator.value(plan.named_nodes["score"], T) is None


@settings(max_examples=30, deadline=None)
@given(st.lists(st.floats(min_value=.00001, max_value=1000, allow_nan=False, allow_infinity=False), min_size=5, max_size=20), st.sampled_from(["sma", "ema", "rsi"]), st.integers(1, 4))
def test_vector_and_pit_features_and_backtests_agree(prices, op, period):
    from stock_quant.vector import VectorEvaluator
    plan, data = compile_strategy(strategy(op, period)), snapshot(prices)
    event = Evaluator(plan, data)
    vector = VectorEvaluator(plan, data, symbol="AAPL")
    for b in data.bars:
        for node in (plan.named_nodes["score"], plan.entry.id, plan.exit.id):
            expected = event.value(node, b.available_at)
            actual = vector.value(node, b.available_at)
            assert actual is None if expected is None else actual == pytest.approx(expected)
    a = run_backtest(plan, data, symbol="AAPL", timeframe="15m", config=ExecutionConfig(quantity=2., commission_bps=5., slippage_bps=10.))
    b = run_backtest(plan, data, symbol="AAPL", timeframe="15m", config=ExecutionConfig(quantity=2., commission_bps=5., slippage_bps=10., signal_backend="vector"))
    for section in ("summary", "orders", "fills", "equity_curve", "cash_ledger", "final_account"):
        assert a[section] == b[section]


def test_vector_rejects_reordered_publication_and_alignment_explicitly():
    from stock_quant.vector import VectorEvaluator
    from test_alignment import aligned_strategy, fixture
    rows = list(snapshot([10, 12, 14]).bars)
    rows[0] = rows[0].model_copy(update={"available_at": T + timedelta(minutes=50)})
    with pytest.raises(ValueError, match="UNSUPPORTED_EXECUTION_MODEL"):
        VectorEvaluator(compile_strategy(strategy()), Snapshot(rows, calendar_version="late"), symbol="AAPL")
    with pytest.raises(ValueError, match="UNSUPPORTED_EXECUTION_MODEL"):
        VectorEvaluator(compile_strategy(aligned_strategy()), fixture(), symbol="AAPL")


def test_vector_does_not_expose_future_rows_and_propagates_cancellation():
    from stock_quant.vector import VectorEvaluator
    plan = compile_strategy(strategy())
    early = VectorEvaluator(plan, snapshot([10, 12, 14]), symbol="AAPL")
    changed = VectorEvaluator(plan, snapshot([10, 12, 999999]), symbol="AAPL")
    assert early.value(plan.named_nodes["score"], T + timedelta(minutes=30)) == changed.value(plan.named_nodes["score"], T + timedelta(minutes=30)) == 11.
    def cancelled():
        raise InterruptedError("cancelled")
    with pytest.raises(InterruptedError):
        VectorEvaluator(plan, snapshot([10, 12, 14]), symbol="AAPL", checkpoint=cancelled)


def test_vector_selection_reaches_shared_broker():
    plan = compile_strategy(strategy())
    result = run_backtest(plan, snapshot([10, 12, 14, 10, 16]), symbol="AAPL", timeframe="15m", config=ExecutionConfig(signal_backend="vector"))
    assert result["fills"][0]["time"] == (T + timedelta(minutes=60)).isoformat()
    assert result["reproducibility"]["signal_backend"] == "vector"


def test_scalar_subgraphs_are_available_before_first_bar():
    from stock_quant.vector import VectorEvaluator
    raw = strategy()
    raw["entry"] = {"kind": "binary", "op": "gt", "left": {"kind": "const", "value": 2}, "right": {"kind": "const", "value": 1}}
    plan = compile_strategy(raw)
    assert VectorEvaluator(plan, snapshot([10, 12]), symbol="AAPL").value(plan.entry.id, T) is True


def test_undefined_rows_reset_rolling_without_fabricating_boolean_signals():
    from stock_quant.vector import VectorEvaluator
    for op in ("sma", "ema", "rsi"):
        raw = strategy(op)
        ref = {"kind": "ref", "symbol": "AAPL", "field": "close", "timeframe": "15m"}
        raw["signals"]["score"]["args"] = [{"kind": "binary", "op": "div", "left": ref,
                                          "right": {"kind": "binary", "op": "sub", "left": ref, "right": {"kind": "const", "value": 10}}}]
        plan, data = compile_strategy(raw), snapshot([12, 13, 10, 14, 15, 16, 17])
        pit, vector = Evaluator(plan, data), VectorEvaluator(plan, data, symbol="AAPL")
        for b in data.bars:
            for node in (plan.named_nodes["score"], plan.entry.id, plan.exit.id):
                expected = pit.value(node, b.available_at)
                actual = vector.value(node, b.available_at)
                assert actual is None if expected is None else actual == pytest.approx(expected)
        assert vector.value(plan.entry.id, T + timedelta(minutes=45)) is None


def test_decimal_thresholds_use_the_same_declared_summation_order():
    from stock_quant.vector import VectorEvaluator
    raw = strategy("sma", 3)
    raw["entry"]["right"]["value"] = .2
    plan, data = compile_strategy(raw), snapshot([.1, .2, .3, .4, .5])
    for evaluator in (Evaluator(plan, data), VectorEvaluator(plan, data, symbol="AAPL")):
        assert evaluator.value(plan.named_nodes["score"], T + timedelta(minutes=45)) == .20000000000000004
        assert evaluator.value(plan.entry.id, T + timedelta(minutes=45)) is True
    pit = run_backtest(plan, data, symbol="AAPL", timeframe="15m")
    vector = run_backtest(plan, data, symbol="AAPL", timeframe="15m", config=ExecutionConfig(signal_backend="vector"))
    assert pit["orders"] == vector["orders"]
    assert pit["fills"] == vector["fills"]


def test_float_typed_integer_literals_have_binary64_semantics():
    from stock_quant.vector import VectorEvaluator
    raw = strategy()
    raw["entry"] = {"kind": "binary", "op": "eq", "left": {"kind": "const", "value": 2**53 + 1}, "right": {"kind": "const", "value": 2**53}}
    plan, data = compile_strategy(raw), snapshot([10, 12])
    assert Evaluator(plan, data).value(plan.entry.id, T) is True
    assert VectorEvaluator(plan, data, symbol="AAPL").value(plan.entry.id, T) is True


def test_vector_probe_cancels_inside_rolling_and_budget_fails_before_allocating_windows():
    from stock_quant.vector import VectorEvaluator
    checks = 0
    def stop_in_rolling():
        nonlocal checks
        checks += 1
        if checks == 4:
            raise InterruptedError("during rolling")
    with pytest.raises(InterruptedError, match="during rolling"):
        VectorEvaluator(compile_strategy(strategy()), snapshot([10, 11, 12]), symbol="AAPL", checkpoint=stop_in_rolling)
    assert checks == 4
    with pytest.raises(ValueError, match="RESOURCE_LIMIT_EXCEEDED.*operation"):
        VectorEvaluator(compile_strategy(strategy(period=3000)), snapshot([10] * 7000), symbol="AAPL")
