from datetime import timedelta
import copy

import pytest

from stock_quant.data import Snapshot
from stock_quant.research import OptimizationRequest, ScreenRequest, optimize, screen
from test_parameters import template
from test_runtime import fixture
from test_data import T


def research_snapshot():
    base = fixture().bars[0]
    prices = [10., 12., 9., 11., 13., 10., 12., 14., 8., 10., 15., 12.]
    rows = [base.model_copy(update={"start_time": T + timedelta(days=i), "end_time": T + timedelta(days=i, hours=6), "available_at": T + timedelta(days=i, hours=6), "open": p, "high": p, "low": p, "close": p}) for i, p in enumerate(prices)]
    return Snapshot(rows, calendar_version="golden")


def windows():
    def window(train_end, val_end, test_end):
        return {"train_start": T, "train_end": T + timedelta(days=train_end), "validation_start": T + timedelta(days=train_end), "validation_end": T + timedelta(days=val_end), "test_start": T + timedelta(days=val_end), "test_end": T + timedelta(days=test_end)}
    return [window(3, 6, 9), window(6, 9, 12)]


def request(**kwargs):
    return OptimizationRequest.model_validate({"strategy_id": "s", "data_snapshot_id": "d", "symbol": "AAPL", "search_space": {"period": [1, 2], "threshold": [9, 12]}, "windows": windows(), "train_top_k": 2, **kwargs})


def test_optimization_records_all_trials_folds_costs_and_reproducibility():
    r = optimize(request(), template(), research_snapshot())
    assert r["summary"]["candidate_count"] == 4
    assert r["summary"]["evaluations"] == 18
    assert len(r["trials"]) == 8
    assert sum(t["validation"] is not None for t in r["trials"]) == 4
    assert len(r["walk_forward"]) == 2
    assert all(len(w["cost_sensitivity"]) == 2 for w in r["walk_forward"])
    assert r == optimize(request(), template(), research_snapshot())


def test_research_metrics_preserve_finite_safe_attribution_and_diagnostics():
    import json
    from stock_quant.analytics import analyze
    from stock_quant.research import metrics
    fills = [{"quantity": 1., "price": 1e308, "commission": 0.} for _ in range(2)]
    result = analyze([{"time": (T + timedelta(days=1)).isoformat(), "equity": 1., "positions": {}}],
                     initial_cash=1., start=T, fills=fills, actions=[])
    result.update(summary={"total_return": 0., "max_drawdown": 0.}, fills=fills, diagnostics={})
    measure = metrics(result)
    json.dumps(measure, allow_nan=False)
    assert measure["turnover"] is None
    assert measure["commission_paid"] == 0.
    assert any("GROSS_TRADED_NOTIONAL_UNDEFINED" in warning for warning in measure["metric_warnings"])


def test_changing_independent_test_prices_cannot_select_parameters():
    snapshot = research_snapshot()
    rows = [b if b.start_time < T + timedelta(days=9) else b.model_copy(update={"open": b.open * 7, "high": b.high * 7, "low": b.low * 7, "close": b.close * 7}) for b in snapshot.bars]
    a = optimize(request(), template(), snapshot)
    b = optimize(request(), template(), Snapshot(rows, calendar_version="golden"))
    assert a["trials"] == b["trials"]
    assert [w["selected_parameters"] for w in a["walk_forward"]] == [w["selected_parameters"] for w in b["walk_forward"]]
    assert a["walk_forward"][-1]["test"] != b["walk_forward"][-1]["test"]


@pytest.mark.parametrize("change", [{"max_trials": 3}, {"max_evaluations": 5}, {"search_space": {"period": [True]}}, {"cost_multipliers": [-1]}, {"search_space": {"period": []}}])
def test_research_budgets_and_types_fail_before_execution(change):
    with pytest.raises(ValueError):
        request(**change)


def test_split_overlap_rejected():
    intervals = windows()
    intervals[0]["validation_start"] = intervals[0]["train_start"]
    with pytest.raises(ValueError):
        request(windows=intervals)
    with pytest.raises(ValueError):
        request(windows=[windows()[0], windows()[0]])


def test_screen_cross_section_scores_and_missing_symbol_diagnostics():
    raw = template()
    raw["signals"] = {"score": {"kind": "ref", "symbol": "$symbol", "field": "close", "timeframe": "1d"}}
    a = fixture().bars[0]
    b = a.model_copy(update={"symbol": "MSFT", "open": 20., "high": 20., "low": 20., "close": 20.})
    s = Snapshot([a, b], calendar_version="test")
    req = ScreenRequest(strategy_id="s", data_snapshot_id=s.id, symbols=["MSFT", "AAPL", "MISSING"], as_of=a.available_at, parameters={"period": 1, "threshold": 0}, score_signal="score")
    result = screen(req, raw, s)
    assert result["summary"] == {"universe_size": 3, "valid_count": 2, "selected_count": 2}
    assert [r["symbol"] for r in result["screen"]] == ["MSFT", "AAPL", "MISSING"]
    assert [r.get("rank") for r in result["screen"]] == [1, 2, None]
    assert result["screen"][0]["zscore"] == 1
    assert {e["code"] for e in result["screen"][-1]["errors"]} == {"DATA_UNAVAILABLE", "INSUFFICIENT_HISTORY"}


def test_constant_strategy_does_not_select_nonexistent_symbol():
    from test_runtime import plan
    raw = {"schema_version": "1.0", "name": "all", "decision_timeframe": "1d", "entry": {"kind": "const", "value": True}, "exit": {"kind": "const", "value": False}}
    req = ScreenRequest(strategy_id="s", data_snapshot_id="d", symbols=["MISSING"], as_of=T)
    result = screen(req, raw, research_snapshot())
    assert result["summary"]["valid_count"] == 0
    assert result["screen"][0]["selected"] is False


def test_all_undefined_trial_is_reported_not_optimized_as_flat_return():
    raw = template()
    raw["parameters"]["period"]["maximum"] = 100
    raw["parameters"]["period"]["default"] = 100
    req = request(search_space={"period": [100]})
    with pytest.raises(ValueError, match="INSUFFICIENT_HISTORY"):
        optimize(req, raw, research_snapshot())


def test_cancel_probe_runs_during_candidate_compilation():
    calls = 0
    def stop(progress):
        nonlocal calls
        calls += 1
        raise InterruptedError("cancel")
    with pytest.raises(InterruptedError):
        optimize(request(), template(), research_snapshot(), stop)
    assert calls == 1


def test_one_symbol_bad_adjustment_does_not_discard_valid_screen_rows():
    raw = template()
    raw["entry"]["left"]["args"][0]["adjustment"] = "split_asof"
    a = fixture().bars[0]
    b = a.model_copy(update={"symbol": "MSFT", "adjustment": "split"})
    snapshot = Snapshot([a, b], calendar_version="test")
    req = ScreenRequest(strategy_id="s", data_snapshot_id=snapshot.id, symbols=["AAPL", "MSFT"], as_of=a.available_at, parameters={"period": 1, "threshold": 0})
    result = screen(req, raw, snapshot)
    assert result["summary"] == {"universe_size": 2, "valid_count": 1, "selected_count": 1}
    rows = {row["symbol"]: row for row in result["screen"]}
    assert rows["AAPL"]["selected"] is True
    assert rows["MSFT"]["errors"][0]["code"] == "DATA_UNAVAILABLE"
    assert rows["MSFT"]["score"] is None


def test_task_level_ownership_loss_during_symbol_evaluation_is_not_a_row_error(monkeypatch):
    from stock_quant.jobs import OwnershipLost
    raw = template()
    snapshot = research_snapshot()
    req = ScreenRequest(strategy_id="s", data_snapshot_id=snapshot.id, symbols=["AAPL"], as_of=snapshot.bars[-1].available_at)
    from stock_quant.runtime import Evaluator
    reached_value = False
    original = Evaluator.value
    def evaluate_then_lose(self, node, now):
        nonlocal reached_value
        reached_value = True
        original(self, node, now)
        raise OwnershipLost("cancel during value evaluation")
    monkeypatch.setattr(Evaluator, "value", evaluate_then_lose)

    with pytest.raises(OwnershipLost):
        screen(req, raw, snapshot)
    assert reached_value
