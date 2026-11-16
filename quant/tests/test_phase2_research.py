"""Daily EMA30 warmup, parameter isolation and four-fold OOS mutation gates."""
import copy
from dataclasses import asdict
import json
from datetime import timedelta

import pytest
from hypothesis import given, settings, strategies as st

from stock_quant.calendar import generate_calendar
from stock_quant.compiler import compile_strategy
from stock_quant.data import Snapshot
from stock_quant.research import OptimizationRequest, optimize
from stock_quant.runtime import Evaluator
from reliability.market import MANIFEST, canonical_hash, daily_payload, payload, snapshot
from reliability.oracles import indicator


def template():
    return {"schema_version": "1.0", "name": "phase2-ema30-research", "decision_timeframe": "1d",
            "parameters": {"period": {"value_type": "integer", "minimum": 1, "maximum": 40, "default": 30}, "threshold": {"value_type": "number", "minimum": 50, "maximum": 160, "default": 115}},
            "signals": {"score": {"kind": "call", "operator": "ema", "args": [{"kind": "ref", "symbol": "$symbol", "field": "close", "timeframe": "1d"}], "params": {"period": {"kind": "param", "name": "period"}}}},
            "entry": {"kind": "binary", "op": "gt", "left": {"kind": "signal", "name": "score"}, "right": {"kind": "param", "name": "threshold"}},
            "exit": {"kind": "binary", "op": "lt", "left": {"kind": "signal", "name": "score"}, "right": {"kind": "param", "name": "threshold"}}}


@pytest.fixture(scope="module")
def daily():
    return snapshot(daily_payload())


def test_calendar_fixture_matches_production_xnys_xnas_and_daily_regimes(daily):
    raw = payload()
    for exchange in ("XNYS", "XNAS"):
        actual = generate_calendar(calendar_name="XNYS", exchange=exchange, start_date="2026-03-04", end_date="2026-11-27")
        by_day = {s.date.isoformat(): s for s in actual.sessions}
        for session in raw["calendars"][0]["sessions"]:
            assert by_day[session["date"]].intervals[0].start.isoformat() == session["intervals"][0]["start"]
            assert by_day[session["date"]].intervals[-1].end.isoformat() == session["intervals"][0]["end"]
    generated = generate_calendar(calendar_name="XNYS", exchange="XNAS", start_date="2026-01-02", end_date="2026-10-01")
    assert [s.date for s in generated.sessions] == [s.date for s in daily.calendars[0].sessions]
    rows = sorted((b for b in daily.bars if b.symbol == "AAPL"), key=lambda b: b.end_time)
    assert len(rows) >= 180
    for a, b in zip(rows[:44], rows[1:45]): assert b.close > a.close
    for a, b in zip(rows[45:89], rows[46:90]): assert b.close < a.close
    prices = [b.close for b in rows[90:135]]
    assert max(prices) - min(prices) < 4
    prices = [b.close for b in rows[135:180]]
    assert max(prices) - min(prices) > 40
    assert min(prices[:22]) < prices[0] and max(prices[22:]) > prices[22]
    manifest = json.loads(MANIFEST.with_name("phase2_daily_v1.json").read_text())
    assert canonical_hash(daily_payload()) == manifest["payload_hash"]
    assert daily.id == manifest["snapshot_id"]


def test_ema30_warmup_independent_oracle(daily):
    plan = compile_strategy(template(), symbol="AAPL")
    ev = Evaluator(plan, daily)
    rows = sorted((b for b in daily.bars if b.symbol == "AAPL"), key=lambda b: b.end_time)
    assert ev.value(plan.named_nodes["score"], rows[28].available_at) is None
    for index in (29, 30, 70, 100, 150):
        assert ev.value(plan.named_nodes["score"], rows[index].available_at) == pytest.approx(indicator([b.close for b in rows[:index + 1]], "ema", 30), abs=1e-9)


@settings(max_examples=12, deadline=None, derandomize=True)
@given(period=st.integers(1, 40), threshold=st.integers(70, 140))
def test_parameter_research_binding_isolation(daily, period, threshold):
    raw = template()
    pristine = copy.deepcopy(raw)
    first = compile_strategy(raw, {"period": period, "threshold": threshold}, "AAPL")
    frozen = asdict(first)
    other = compile_strategy(raw, {"period": 41 - period, "threshold": 210 - threshold}, "MSFT")
    assert asdict(first) == frozen
    assert raw == pristine
    assert first.plan_hash != other.plan_hash
    assert first.requirements[0]["symbol"] == "AAPL"
    assert other.requirements[0]["symbol"] == "MSFT"
    # Running the second binding cannot contaminate an existing evaluator.
    now = max(b.available_at for b in daily.bars)
    before = Evaluator(first, daily).value(first.named_nodes["score"], now)
    Evaluator(other, daily).value(other.named_nodes["score"], now)
    assert Evaluator(first, daily).value(first.named_nodes["score"], now) == before


def research_request(daily):
    rows = sorted((b for b in daily.bars if b.symbol == "AAPL"), key=lambda b: b.start_time)
    windows = []
    for fold in range(4):
        t = 60 + fold * 20
        windows.append(dict(train_start=rows[0].start_time, train_end=rows[t].start_time,
                            validation_start=rows[t].start_time, validation_end=rows[t + 10].start_time,
                            test_start=rows[t + 10].start_time, test_end=rows[t + 20].start_time))
    return OptimizationRequest(strategy_id="phase2-ema30", data_snapshot_id=daily.id, symbol="AAPL",
                               search_space={"period": [10, 30], "threshold": [105., 115.]}, windows=windows,
                               train_top_k=2, cost_multipliers=[1, 2], random_seed=80421)


@pytest.mark.parametrize("fold", range(4), ids=lambda n: f"walk_forward_optimization_fold_{n}_test_isolation")
def test_walk_forward_optimization_fold_future_mutation(daily, fold):
    request = research_request(daily)
    baseline = optimize(request, template(), daily)
    boundary = request.windows[fold].test_start
    rows = [b.model_copy(update={k: getattr(b, k) * 1.7 for k in ("open", "high", "low", "close")}) if b.start_time >= boundary else b for b in daily.bars]
    mutated = optimize(request, template(), Snapshot(rows, calendar_version=daily.calendar_version, calendars=daily.calendars))
    assert baseline["summary"]["windows"] == 4
    assert baseline["summary"]["candidate_count"] == 4
    # Earlier folds and this fold's selection are isolated; later folds may
    # intentionally train on what is now historical OOS data.
    for index in range(fold + 1):
        a, b = baseline["walk_forward"][index], mutated["walk_forward"][index]
        for key in ("selected_parameters", "selected_plan_id", "train", "validation"):
            assert a[key] == b[key]
        assert [t for t in baseline["trials"] if t["window"] == index] == [t for t in mutated["trials"] if t["window"] == index]
    a, b = baseline["walk_forward"][fold], mutated["walk_forward"][fold]
    assert a["test"] != b["test"], "OOS perturbation must reach the executed test result"
    assert len(a["cost_sensitivity"]) == 2
