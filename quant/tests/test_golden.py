"""One versioned artificial market shared by the feature engines and broker."""
import json
from pathlib import Path
from datetime import datetime, timezone

import pytest

from stock_quant.compiler import compile_strategy
from stock_quant.data import Snapshot, MarketBar
from stock_quant.portfolio import run_portfolio
from stock_quant.runtime import ExecutionConfig
from stock_quant.quality import data_quality


def dataset():
    raw = json.loads(Path(__file__).with_name('fixtures').joinpath('golden_market_v1.json').read_text())
    return Snapshot([MarketBar.model_validate(b) for b in raw["bars"]], calendar_version=raw["calendar_version"],
                    calendars=raw["calendars"], actions=raw["actions"]), raw["expected"]


def test_golden_accounting_partial_fills_actions_and_feature_backend_parity():
    snapshot, expected = dataset()
    plan = compile_strategy({"schema_version": "1.0", "name": "golden-hold", "decision_timeframe": "1d",
                             "entry": {"kind": "const", "value": True}, "exit": {"kind": "const", "value": False}})
    result = run_portfolio({"AAPL": plan, "BETF": plan}, snapshot, config=ExecutionConfig(initial_cash=1000., quantity=10., volume_participation=.5))
    vector = run_portfolio({"AAPL": plan, "BETF": plan}, snapshot, config=ExecutionConfig(initial_cash=1000., quantity=10., volume_participation=.5, signal_backend="vector"))
    for section in ("summary", "orders", "fills", "equity_curve", "final_account", "cash_ledger", "corporate_actions", "attribution", "daily_returns"):
        assert vector[section] == result[section]
    assert len(result["fills"]) == expected["fill_count"]
    assert result["final_account"]["equity"] == pytest.approx(expected["equity"])
    assert result["final_account"]["cash"] == pytest.approx(expected["cash"])
    assert result["final_account"]["positions"]["AAPL"]["quantity"] == expected["aapl_quantity"]
    assert result["final_account"]["positions"]["BETF"]["quantity"] == 0
    assert result["attribution"]["accounting_residual"] == pytest.approx(0., abs=1e-10)
    assert result["attribution"]["dividend_income"] == expected["dividend_income"]
    for row in result["equity_curve"]:
        assert row["equity"] == pytest.approx(row["cash"] + row["receivables"] + sum(p["quantity"] * p["mark"] for p in row["positions"].values()))


def test_golden_observed_gaps_are_reported_and_unfinished_daily_hidden():
    from stock_quant.runtime import Evaluator
    snapshot, _ = dataset()
    q = data_quality(snapshot, symbol="AAPL", timeframe="15m", as_of=datetime(2026, 1, 9, 21, tzinfo=timezone.utc))
    assert q["unavailable_completed_count"] > 0
    assert q["out_of_schedule_count"] == 0
    plan = compile_strategy({"schema_version": "1.0", "name": "daily-confirmation", "decision_timeframe": "15m", "entry": {
        "kind": "binary", "op": "gt", "left": {"kind": "align", "value": {"kind": "ref", "symbol": "AAPL", "field": "close", "timeframe": "1d"}, "timeframe": "15m", "policy": "last_completed"}, "right": {"kind": "const", "value": 0}}, "exit": {"kind": "const", "value": False}})
    evaluator = Evaluator(plan, snapshot, symbol="AAPL")
    assert evaluator.value(plan.entry.id, datetime(2026, 1, 2, 15, tzinfo=timezone.utc)) is None
    assert evaluator.value(plan.entry.id, datetime(2026, 1, 5, 15, tzinfo=timezone.utc)) is True
