"""Execute the eight versioned strategy benchmarks and emit strict JSON metrics.

Run from any directory with `uv run --project quant python quant/benchmarks/run.py`.
Timing covers production compilation/evaluation only; oracle generation is excluded.
"""
import argparse
import json
from pathlib import Path
import statistics
import sys
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from reliability.benchmarks import catalog
from reliability.market import canonical_hash, payload, snapshot
from stock_quant.compiler import compile_strategy
from stock_quant.runtime import ExecutionConfig, run_backtest


def run(repeats=3):
    if type(repeats) is not int or not 1 <= repeats <= 100:
        raise ValueError("repeats must be 1..100")
    cat, raw = catalog(), payload()
    market = snapshot(raw)
    if canonical_hash(raw) != cat["fixture_hash"] or market.id != cat["snapshot_id"]:
        raise ValueError("benchmark fixture integrity mismatch")
    metrics = []
    for case in cat["cases"]:
        if canonical_hash(case["strategy"]) != case["strategy_hash"]:
            raise ValueError("benchmark strategy integrity mismatch")
        started = perf_counter()
        plan = compile_strategy(case["strategy"], symbol="AAPL")
        compilation = perf_counter() - started
        durations = []
        for _ in range(repeats):
            started = perf_counter()
            result = run_backtest(plan, market, symbol="AAPL", timeframe=plan.decision_timeframe, config=ExecutionConfig(**cat["execution_config"]))
            durations.append(perf_counter() - started)
        expected = case["expected"]
        actual_fills = [{k: f[k] for k in ("time", "side", "quantity", "price", "commission")} for f in result["fills"]]
        drift = abs(result["final_account"]["equity"] - expected["equity"])
        dd_drift = abs(result["summary"]["max_drawdown"] - expected["max_drawdown"])
        if actual_fills != expected["fills"] or drift > 1e-7 or dd_drift > 1e-10:
            raise AssertionError(f"independent benchmark oracle mismatch: {case['id']}")
        decisions = result["diagnostics"]["evaluated_decisions"]
        median = statistics.median(durations)
        metrics.append({"id": case["id"], "status": "PASS", "timeframe": plan.decision_timeframe,
                        "plan_hash": plan.plan_hash, "compile_seconds": compilation, "runs": repeats,
                        "runtime_seconds_p50": median, "runtime_seconds_max": max(durations),
                        "evaluated_decisions": decisions, "decisions_per_second": decisions / median,
                        "fill_count": len(result["fills"]), "order_count": len(result["orders"]),
                        "total_return": result["summary"]["total_return"], "max_drawdown": result["summary"]["max_drawdown"],
                        "commission_paid": result["attribution"]["commission_paid"],
                        "equity_oracle_abs_error": drift, "drawdown_oracle_abs_error": dd_drift})
    return {"schema_version": "phase2-benchmark-metrics-v1", "catalog_version": cat["version"],
            "fixture_hash": cat["fixture_hash"], "snapshot_id": market.id, "bar_count": len(market.bars), "benchmarks": metrics,
            "limitations": ["Artificial data; no vendor-quality claim", "Local timings; no universal latency threshold", "PIT feature path; shared-broker vector parity is tested separately", "No sandbox/provider/production deployment performance claim"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    encoded = json.dumps(run(args.repeats), indent=2, allow_nan=False) + "\n"
    if args.output:
        args.output.write_text(encoded)
    else:
        print(encoded, end="")
