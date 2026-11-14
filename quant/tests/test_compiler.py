import copy

import pytest

from stock_quant.compiler import compile_strategy, validate_strategy


def ref(timeframe="15m"):
    return {"kind": "ref", "symbol": "AAPL", "field": "close", "timeframe": timeframe}


def strategy():
    return {
        "schema_version": "1.0", "name": "trend", "decision_timeframe": "15m",
        "signals": {"trend": {"kind": "call", "operator": "ema", "args": [ref("1d")], "params": {"period": 20}}},
        "entry": {"kind": "binary", "op": "gt", "left": ref(), "right": {"kind": "align", "value": {"kind": "signal", "name": "trend"}, "timeframe": "15m", "policy": "last_completed"}},
        "exit": {"kind": "const", "value": False},
    }


def codes(value):
    return {e["code"] for e in validate_strategy(value)["errors"]}


def test_compiles_explicit_alignment_and_warmup():
    plan = compile_strategy(strategy())
    assert plan.entry.value_type == "bool"
    assert plan.requirements == [{"symbol": "AAPL", "field": "close", "timeframe": "15m", "warmup_bars": 1}, {"symbol": "AAPL", "field": "close", "timeframe": "1d", "warmup_bars": 20}]
    assert len({n.id for n in plan.nodes}) == len(plan.nodes)
    assert all(n.availability == "available_at" for n in plan.nodes)


def test_canonical_hash_and_operator_version():
    value = strategy()
    other = dict(reversed(list(value.items())))
    assert compile_strategy(value).plan_hash == compile_strategy(other).plan_hash
    assert compile_strategy(value).operator_versions == {"ema": "2.0"}
    other = copy.deepcopy(value)
    other["signals"]["trend"]["params"]["period"] = 21
    assert compile_strategy(value).plan_hash != compile_strategy(other).plan_hash


@pytest.mark.parametrize("value", [1, 0, 1.0])
def test_numeric_entry_is_not_bool(value):
    s = strategy()
    s["entry"] = {"kind": "const", "value": value}
    assert "TYPE_MISMATCH" in codes(s)


def test_implicit_mixed_timeframes_fail():
    s = strategy()
    s["entry"]["right"] = {"kind": "signal", "name": "trend"}
    assert "TIMEFRAME_MISMATCH" in codes(s)


def test_cycle_and_unknown_reference_are_structured():
    s = strategy()
    s["signals"]["trend"] = {"kind": "signal", "name": "trend"}
    assert "CYCLIC_DEPENDENCY" in codes(s)
    s["signals"]["trend"]["name"] = "missing"
    assert "UNKNOWN_REFERENCE" in codes(s)


@pytest.mark.parametrize("params", [{"period": True}, {"period": 0}, {"period": 2.5}, {"period": 20, "typo": 1}])
def test_operator_parameters_strict(params):
    s = strategy()
    s["signals"]["trend"]["params"] = params
    assert "SCHEMA_INVALID" in codes(s)


def test_unknown_operator_and_extra_keys_fail():
    s = strategy()
    s["signals"]["trend"]["operator"] = "execute_python"
    assert "UNKNOWN_OPERATOR" in codes(s)
    s = strategy()
    s["typo"] = 1
    assert "SCHEMA_INVALID" in codes(s)


def test_shared_expression_is_one_dag_node():
    s = strategy()
    s["entry"] = {"kind": "binary", "op": "gt", "left": ref(), "right": ref()}
    plan = compile_strategy(s)
    assert plan.entry.inputs[0] == plan.entry.inputs[1]


def test_nested_warmup_accumulates():
    s = strategy()
    s["signals"]["trend"]["args"] = [{"kind": "call", "operator": "sma", "args": [ref("1d")], "params": {"period": 10}}]
    assert compile_strategy(s).requirements[-1]["warmup_bars"] == 29


def test_rejects_bool_arithmetic_and_future_alignment():
    s = strategy()
    s["entry"] = {"kind": "binary", "op": "add", "left": {"kind": "const", "value": True}, "right": ref()}
    assert "TYPE_MISMATCH" in codes(s)
    s = strategy()
    s["entry"]["right"]["policy"] = "nearest"
    assert "SCHEMA_INVALID" in codes(s)
