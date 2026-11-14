import copy
import pytest
from stock_quant.compiler import compile_strategy, CompileError
from stock_quant.registry import Registry
from stock_quant.jobs import JobStore


def template():
    return {"schema_version": "1.0", "name": "parametric", "decision_timeframe": "1d",
            "parameters": {"threshold": {"value_type": "number", "minimum": 0, "maximum": 1000, "default": 10}, "period": {"value_type": "integer", "minimum": 1, "maximum": 20, "default": 2}},
            "entry": {"kind": "binary", "op": "gt", "left": {"kind": "call", "operator": "sma", "args": [{"kind": "ref", "symbol": "$symbol", "field": "close", "timeframe": "1d"}], "params": {"period": {"kind": "param", "name": "period"}}}, "right": {"kind": "param", "name": "threshold"}}, "exit": {"kind": "const", "value": False}}


def test_bindings_change_plan_not_strategy_and_are_immutable(tmp_path):
    raw = template()
    original = copy.deepcopy(raw)
    a, b = compile_strategy(raw, {"period": 1}, "AAPL"), compile_strategy(raw, {"period": 3}, "MSFT")
    assert a.strategy_hash == b.strategy_hash
    assert a.plan_hash != b.plan_hash
    assert a.requirements[0]["symbol"] == "AAPL"
    assert b.requirements[0]["warmup_bars"] == 3
    assert raw == original
    with JobStore(tmp_path / "db") as store:
        registered = store.register(raw)
        persisted = store.compile(registered["strategy_id"], {"period": 1}, "AAPL")
        assert store.load_plan(persisted["plan_id"]).plan_hash == a.plan_hash


@pytest.mark.parametrize("parameters", [{"period": True}, {"period": 2.5}, {"period": 0}, {"threshold": float("nan")}, {"unknown": 1}])
def test_invalid_parameter_values_rejected(parameters):
    with pytest.raises(CompileError):
        compile_strategy(template(), parameters, "AAPL")
