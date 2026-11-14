"""Required benchmark categories execute production portfolio/research APIs."""
import json
from pathlib import Path
from statistics import mean, pstdev

import pytest

from stock_quant.compiler import compile_strategy, digest
from stock_quant.portfolio import run_portfolio
from stock_quant.research import ScreenRequest, screen
from stock_quant.runtime import ExecutionConfig, run_backtest
from reliability.benchmarks import catalog
from reliability.market import SYMBOLS, canonical_hash, daily_payload, payload, snapshot
from reliability.oracles import account, indicator
from test_phase2_research import template
from test_phase2_reliability import assert_account

MANIFEST = Path(__file__).parents[1] / "benchmarks" / "manifest_v1.json"


def parameterized_inputs():
    definition = template()
    parameters = {"period": 30, "threshold": 115.}
    bound = json.loads(json.dumps(definition))
    bound.pop("parameters")
    bound["signals"]["score"]["params"]["period"] = parameters["period"]
    bound["signals"]["score"]["args"][0]["symbol"] = "AAPL"
    for key in ("entry", "exit"):
        bound[key]["right"] = {"kind": "const", "value": parameters["threshold"]}
    return definition, parameters, bound


def test_required_parameterized_benchmark_oracle():
    data = snapshot(daily_payload())
    definition, parameters, bound = parameterized_inputs()
    config = catalog()["execution_config"]
    result = run_backtest(compile_strategy(definition, parameters, "AAPL"), data, symbol="AAPL", timeframe="1d", config=ExecutionConfig(**config))
    expected = account(bound, data.bars, **config)
    assert len(expected["fills"]) > 0
    assert_account(result, expected)


def test_required_screening_benchmark_oracle():
    data = snapshot(daily_payload())
    as_of = max(b.available_at for b in data.bars)
    request = ScreenRequest(strategy_id=digest(template()), data_snapshot_id=data.id, symbols=list(SYMBOLS), as_of=as_of, parameters={"period": 30, "threshold": 115.}, score_signal="score", random_seed=80421)
    result = screen(request, template(), data)
    scores = {}
    for symbol in SYMBOLS:
        history = sorted((b for b in data.bars if b.symbol == symbol and b.available_at <= as_of), key=lambda b: b.end_time)
        scores[symbol] = indicator([b.close for b in history], "ema", 30)
    average, deviation = mean(scores.values()), pstdev(scores.values())
    assert result["summary"]["universe_size"] == result["summary"]["valid_count"] == 3
    for row in result["screen"]:
        expected = scores[row["symbol"]]
        assert row["score"] == pytest.approx(expected, abs=1e-9)
        assert row["rank"] == 1 + sum(s > expected for s in scores.values())
        assert row["zscore"] == pytest.approx((expected - average) / deviation)
        assert row["entry"] is (expected > 115.)


def test_required_multi_asset_portfolio_benchmark_oracle():
    data = snapshot(daily_payload())
    spec = catalog()["cases"][0]["strategy"]
    plans = {s: compile_strategy(spec, symbol=s) for s in SYMBOLS}
    cfg = catalog()["execution_config"]
    result = run_portfolio(plans, data, config=ExecutionConfig(**cfg))
    cash, equity, fees = cfg["initial_cash"], cfg["initial_cash"], 0.
    assert len(result["fills"]) == 3
    for symbol in SYMBOLS:
        history = sorted((b for b in data.bars if b.symbol == symbol), key=lambda b: b.start_time)
        price = history[1].open * (1 + cfg["slippage_bps"] / 10000)
        fee = cfg["quantity"] * price * cfg["commission_bps"] / 10000
        debit = cfg["quantity"] * price + fee
        cash -= debit
        equity += cfg["quantity"] * history[-1].close - debit
        fees += fee
        fill = next(f for f in result["fills"] if f["symbol"] == symbol)
        assert fill["time"] == history[1].start_time.isoformat()
        assert fill["price"] == pytest.approx(price)
        assert fill["commission"] == pytest.approx(fee)
        pos = result["final_account"]["positions"][symbol]
        assert pos["quantity"] == cfg["quantity"]
        assert pos["basis"] == pytest.approx(debit)
        assert pos["mark"] == history[-1].close
    assert result["final_account"]["cash"] == pytest.approx(cash)
    assert result["final_account"]["equity"] == pytest.approx(equity)
    assert result["attribution"]["commission_paid"] == pytest.approx(fees)


def test_required_benchmark_manifest_hashes_plans_seeds_config():
    from reliability.required import manifest
    checked = json.loads(MANIFEST.read_text())
    assert checked == manifest()
    assert {c["category"] for c in checked["benchmarks"]} == {"BuyAndHold", "EMATrend", "TrendFollowing", "MeanReversion", "MultiTimeframe", "CrossAssetSignal", "Parameterized", "Screening", "WalkForward", "MultiAssetPortfolio"}
    assert len(checked["benchmarks"]) == 10
    for case in checked["benchmarks"]:
        assert case["random_seed"] == 80421
        assert case["plan_hashes"]
        assert case["execution_config_hash"] == digest(case["execution_config"])
        assert case["tolerance"]["equity_abs"] == 1e-7
        assert case["pytest_node_ids"]
