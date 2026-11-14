"""Nine required benchmark categories bound to production API acceptance IDs."""
from stock_quant.compiler import compile_strategy, digest
from .benchmarks import catalog
from .market import SEED, SYMBOLS, canonical_hash, daily_payload, payload, snapshot


def manifest():
    from test_phase2_research import research_request, template
    cat = catalog()
    mixed, daily = payload(), daily_payload()
    daily_snapshot = snapshot(daily)
    tol = {"equity_abs": 1e-7, "indicator_abs": 1e-9, "drawdown_abs": 1e-10, "order_time": "exact", "rank": "exact", "parameter_selection": "exact"}
    cases = []
    def add(category, raw, plans, config, nodes, definition, **extra):
        cases.append({"id": category + "-v1", "category": category, "version": 1,
                      "benchmark_id": category + "-v1", "strategy_version": 1, "baseline_version": 1,
                      "calendar_version": raw["version"], "expected_invariants": ["identity_matches", "PIT_only", "accounting_conservation"],
                      "fixture_version": raw["version"], "fixture_payload_hash": canonical_hash(raw), "snapshot_hash": snapshot(raw).id,
                      "strategy_definition_hash": canonical_hash(definition), "plan_hashes": plans,
                      "random_seed": SEED, "execution_config": config, "execution_config_hash": digest(config),
                      "tolerance": dict(tol), "pytest_node_ids": nodes, **extra})
    for category, index in (("BuyAndHold", 0), ("EMATrend", 2), ("TrendFollowing", 1), ("MeanReversion", 3), ("MultiTimeframe", 5), ("CrossAssetSignal", 7)):
        case = cat["cases"][index]
        add(category, mixed, [compile_strategy(case["strategy"], symbol="AAPL").plan_hash], cat["execution_config"],
            [f"quant/tests/test_phase2_reliability.py::test_phase2_strategy_golden_oracle[golden_oracle_{case['id']}]"], case["strategy"],
            oracle_catalog="quant/benchmarks/strategies_v1.json", oracle_case=case["id"], expected_metrics=case["expected"])
    definition = template()
    params = {"period": 30, "threshold": 115.}
    add("Parameterized", daily, [compile_strategy(definition, params, "AAPL").plan_hash], cat["execution_config"],
        ["quant/tests/test_phase2_benchmarks.py::test_required_parameterized_benchmark_oracle", "quant/tests/test_phase2_research.py::test_parameter_research_binding_isolation"], definition, parameters=params)
    clock = max(b.available_at for b in daily_snapshot.bars).isoformat()
    add("Screening", daily, [compile_strategy(definition, params, s).plan_hash for s in SYMBOLS],
        {"mode": "screen_only", "as_of": clock, "symbols": list(SYMBOLS), "score_signal": "score", "parameters": params},
        ["quant/tests/test_phase2_benchmarks.py::test_required_screening_benchmark_oracle"], definition)
    request = research_request(daily_snapshot)
    plans = [compile_strategy(definition, {"period": p, "threshold": t}, "AAPL").plan_hash for p in (10, 30) for t in (105., 115.)]
    add("WalkForward", daily, plans, request.config.model_dump(mode="json"),
        [f"quant/tests/test_phase2_research.py::test_walk_forward_optimization_fold_future_mutation[walk_forward_optimization_fold_{i}_test_isolation]" for i in range(4)], definition,
        optimization_request=request.model_dump(mode="json"), optimization_request_hash=digest(request.model_dump(mode="json")),
        parent_report="reports/quant-research.json", parent_report_producer="scripts/quant-research-regression.py",
        parent_report_note="Separate parent EMA fixture; its own hashes and plan identities apply")
    hold = cat["cases"][0]["strategy"]
    add("MultiAssetPortfolio", daily, [compile_strategy(hold, symbol=s).plan_hash for s in SYMBOLS], cat["execution_config"],
        ["quant/tests/test_phase2_benchmarks.py::test_required_multi_asset_portfolio_benchmark_oracle", "quant/tests/test_phase2_accounting.py::test_partial_fills_shared_cash_and_expiry_hand_oracle"], hold,
        execution_symbols=list(SYMBOLS))
    return {"schema_version": "phase2-required-benchmarks-v1", "benchmarks": cases,
            "strategy_oracle_catalog": "quant/benchmarks/strategies_v1.json", "metrics_command": "uv run --project quant python quant/benchmarks/run.py",
            "required_categories_command": "uv run --project quant python quant/benchmarks/run_required.py"}
