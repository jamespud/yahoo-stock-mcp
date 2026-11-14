"""Bounded research computation; no autonomous agent decisions."""
from __future__ import annotations

from itertools import product
from math import prod, isfinite
import statistics
from typing import Literal

from pydantic import AwareDatetime, Field, StrictFloat, StrictInt, model_validator

from .actions import CashDelisting
from .compiler import compile_strategy, digest
from .contracts import StrictModel, Strategy
from .data import Snapshot
from .runtime import ExecutionConfig, Evaluator, run_backtest


class Window(StrictModel):
    train_start: AwareDatetime
    train_end: AwareDatetime
    validation_start: AwareDatetime
    validation_end: AwareDatetime
    test_start: AwareDatetime
    test_end: AwareDatetime

    @model_validator(mode="after")
    def boundaries(self):
        if not self.train_start < self.train_end <= self.validation_start < self.validation_end <= self.test_start < self.test_end:
            raise ValueError("Expected disjoint train < validation < test intervals (exclusive ends)")
        return self


class OptimizationRequest(StrictModel):
    kind: Literal["optimize"] = "optimize"
    strategy_id: str
    data_snapshot_id: str
    symbol: str = Field(min_length=1, max_length=64)
    search_space: dict[str, list[StrictInt | StrictFloat]] = Field(min_length=1, max_length=16)
    windows: list[Window] = Field(min_length=1, max_length=10)
    objective: Literal["total_return", "max_drawdown"] = "total_return"
    train_top_k: int = Field(default=3, ge=1, le=10, strict=True)
    max_trials: int = Field(default=100, ge=1, le=100, strict=True)
    max_evaluations: int = Field(default=1000, ge=1, le=1000, strict=True)
    cost_multipliers: list[StrictInt | StrictFloat] = Field(default_factory=lambda: [1, 2], min_length=1, max_length=5)
    config: ExecutionConfig = Field(default_factory=ExecutionConfig)
    random_seed: int = Field(default=0, strict=True)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def budget(self):
        if any(not values or len(values) > 100 or len(set(values)) != len(values) for values in self.search_space.values()):
            raise ValueError("Search dimensions must contain 1..100 unique values")
        count = prod(len(v) for v in self.search_space.values())
        if count > self.max_trials:
            raise ValueError("RESOURCE_LIMIT_EXCEEDED: Cartesian search exceeds max_trials")
        if (count + min(count, self.train_top_k) + 1 + len(self.cost_multipliers)) * len(self.windows) > self.max_evaluations:
            raise ValueError("RESOURCE_LIMIT_EXCEEDED: research exceeds max_evaluations")
        if any(v < 0 or not isfinite(v) for v in self.cost_multipliers):
            raise ValueError("Cost multipliers must be finite/nonnegative")
        for a, b in zip(self.windows, self.windows[1:]):
            if a.test_end > b.test_start or a.train_end >= b.train_end:
                raise ValueError("Walk-forward tests must not overlap and training endpoints must advance")
        for scale in self.cost_multipliers:
            ExecutionConfig.model_validate({**self.config.model_dump(), "commission_bps": self.config.commission_bps * scale, "slippage_bps": self.config.slippage_bps * scale})
        return self


class ScreenRequest(StrictModel):
    kind: Literal["screen"] = "screen"
    strategy_id: str
    data_snapshot_id: str
    symbols: list[str] = Field(min_length=1, max_length=500)
    as_of: AwareDatetime
    parameters: dict[str, StrictInt | StrictFloat] = Field(default_factory=dict, max_length=16)
    score_signal: str | None = None
    random_seed: int = Field(default=0, strict=True)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def universe(self):
        if len(set(self.symbols)) != len(self.symbols) or any(not 1 <= len(s) <= 64 or s == "$symbol" for s in self.symbols):
            raise ValueError("Expected unique explicit universe symbols")
        return self


def candidates(request: OptimizationRequest):
    names = sorted(request.search_space)
    for values in product(*(request.search_space[name] for name in names)):
        yield dict(zip(names, values))


def validate_research(request, definition, checkpoint=None):
    Strategy.model_validate(definition)
    if isinstance(request, OptimizationRequest):
        plans = []
        for parameters in candidates(request):
            if checkpoint:
                checkpoint(0.)
            plans.append((parameters, compile_strategy(definition, parameters, request.symbol)))
        return plans
    else:
        plan = compile_strategy(definition, request.parameters, request.symbols[0])
        if request.score_signal is not None:
            if request.score_signal not in plan.named_nodes:
                raise ValueError("UNKNOWN_REFERENCE: score_signal must name a signal")
            node = next(n for n in plan.nodes if n.id == plan.named_nodes[request.score_signal])
            if node.value_type != "float":
                raise ValueError("TYPE_MISMATCH: score_signal must be numeric")


def metrics(result):
    return {**result["summary"], "turnover": result["attribution"]["turnover_to_initial_equity"],
            "commission_paid": result["attribution"]["commission_paid"],
            "metric_warnings": result["analytics"]["warnings"], "signal_diagnostics": result["diagnostics"]}


def optimize(request: OptimizationRequest, definition: dict, snapshot: Snapshot, checkpoint=None):
    plans = validate_research(request, definition, checkpoint)
    grid = [parameters for parameters, _ in plans]
    windows, trials = [], []
    evaluations = 0
    bound = (len(grid) + min(len(grid), request.train_top_k) + 1 + len(request.cost_multipliers)) * len(request.windows)
    def run(plan, start, end, config):
        nonlocal evaluations
        if evaluations >= request.max_evaluations:
            raise ValueError("RESOURCE_LIMIT_EXCEEDED: evaluation budget exhausted")
        base = evaluations
        result = run_backtest(plan, snapshot, symbol=request.symbol, timeframe=plan.decision_timeframe, config=config, start=start, end=end,
                              checkpoint=(lambda p: checkpoint((base + p) / bound)) if checkpoint else None)
        if result["diagnostics"]["evaluated_decisions"] == 0 or any(result["diagnostics"][name] == result["diagnostics"]["evaluated_decisions"] for name in ("undefined_entry_decisions", "undefined_exit_decisions")):
            raise ValueError("INSUFFICIENT_HISTORY: trial has no defined entry decisions")
        evaluations += 1
        return metrics(result)
    for wi, window in enumerate(request.windows):
        train = []
        for parameters, plan in plans:
            measure = run(plan, window.train_start, window.train_end, request.config)
            trial = {"window": wi, "parameters": parameters, "plan_id": plan.plan_hash, "train": measure, "validation": None}
            trials.append(trial)
            train.append((trial, plan))
        train.sort(key=lambda item: (-item[0]["train"][request.objective], digest(item[0]["parameters"])))
        finalists = train[:request.train_top_k]
        for trial, plan in finalists:
            trial["validation"] = run(plan, window.validation_start, window.validation_end, request.config)
        winner, selected = min(finalists, key=lambda item: (-item[0]["validation"][request.objective], digest(item[0]["parameters"])))
        # No test observation is read to choose the winner. Only the frozen
        # winner is evaluated on this fold's independent test interval.
        test = run(selected, window.test_start, window.test_end, request.config)
        costs = []
        for scale in request.cost_multipliers:
            config = ExecutionConfig.model_validate({**request.config.model_dump(), "commission_bps": request.config.commission_bps * scale, "slippage_bps": request.config.slippage_bps * scale})
            costs.append({"multiplier": scale, "metrics": run(selected, window.test_start, window.test_end, config)})
        windows.append({"window": wi, "boundaries": window.model_dump(mode="json"), "selected_parameters": winner["parameters"], "selected_plan_id": selected.plan_hash,
                        "train": winner["train"], "validation": winner["validation"], "test": test, "cost_sensitivity": costs})
    sensitivity = []
    for name in sorted(request.search_space):
        for value in request.search_space[name]:
            scores = [t["train"][request.objective] for t in trials if t["parameters"][name] == value]
            sensitivity.append({"parameter": name, "value": value, "mean_train_score": statistics.mean(scores), "observations": len(scores)})
    returns = [w["test"]["total_return"] for w in windows]
    return {"summary": {"windows": len(windows), "candidate_count": len(grid), "evaluations": evaluations,
                        "mean_test_return": statistics.mean(returns), "test_return_std": statistics.pstdev(returns),
                        "return_definition": "independent fresh-capital folds; mean is not a compounded portfolio return"},
            "trials": trials, "walk_forward": windows, "parameter_sensitivity": sensitivity,
            "diagnostics": {"selection": "train_top_k_then_validation", "out_of_sample_used_for_selection": False, "universe": "caller_selected_symbol"},
            "reproducibility": {"strategy_hash": plans[0][1].strategy_hash, "data_snapshot_id": snapshot.id, "request_hash": digest(request.model_dump(mode="json"))}}


def screen(request: ScreenRequest, definition: dict, snapshot: Snapshot, checkpoint=None):
    validate_research(request, definition)
    rows = []
    for i, symbol in enumerate(sorted(request.symbols)):
        if checkpoint:
            checkpoint(i / len(request.symbols))
        plan = compile_strategy(definition, request.parameters, symbol)
        errors = []
        if not snapshot.history(symbol, plan.decision_timeframe, request.as_of, 1):
            errors.append({"code": "DATA_UNAVAILABLE", "symbol": symbol})
        if any(isinstance(a, CashDelisting) and a.symbol == symbol and a.effective_at <= request.as_of for a in snapshot.actions):
            errors.append({"code": "DELISTED"})
        for dep in plan.requirements:
            if len(snapshot.history(dep["symbol"], dep["timeframe"], request.as_of, dep["warmup_bars"])) < dep["warmup_bars"]:
                errors.append({"code": "INSUFFICIENT_HISTORY", "dependency": dep})
        try:
            evaluator = Evaluator(plan, snapshot, checkpoint=(lambda: checkpoint(i / len(request.symbols))) if checkpoint else None, symbol=symbol)
            for dependency in evaluator.grid_dependencies(request.as_of):
                if dependency["available_bars"] < dependency["warmup_bars"]:
                    errors.append({"code": "INSUFFICIENT_HISTORY", "grid_dependency": dependency})
            entry = evaluator.value(plan.entry.id, request.as_of) if not errors else None
            exit_ = evaluator.value(plan.exit.id, request.as_of) if not errors else None
            score = evaluator.value(plan.named_nodes[request.score_signal], request.as_of) if request.score_signal and not errors else float(entry is True)
        except ValueError as error:
            code = str(error).partition(":")[0]
            if code not in ("DATA_UNAVAILABLE", "INSUFFICIENT_HISTORY"):
                raise
            errors.append({"code": code, "message": str(error)})
            entry, exit_, score = None, None, None
        if not errors and (entry is None or exit_ is None or score is None):
            errors.append({"code": "SIGNAL_UNDEFINED"})
        rows.append({"symbol": symbol, "entry": entry, "exit": exit_, "selected": not errors and entry is True and exit_ is False, "score": score if not errors else None,
                     "errors": errors, "plan_id": plan.plan_hash})
    valid = sorted((r for r in rows if r["score"] is not None), key=lambda r: (-r["score"], r["symbol"]))
    scores = [r["score"] for r in valid]
    scale = max((abs(value) for value in scores), default=0.) or 1.
    normalized = [value / scale for value in scores]
    mean, std = (statistics.mean(normalized), statistics.pstdev(normalized)) if normalized else (0., 0.)
    # Equal factors share a competition rank and population z-score.
    for row in valid:
        row["rank"] = 1 + sum(score > row["score"] for score in scores)
        row["percentile"] = sum(score <= row["score"] for score in scores) / len(scores)
        row["zscore"] = (row["score"] / scale - mean) / std if std else 0.
    return {"summary": {"universe_size": len(rows), "valid_count": len(valid), "selected_count": sum(r["selected"] for r in rows)},
            "screen": sorted(rows, key=lambda r: (r["score"] is None, -(r["score"] or 0), r["symbol"])),
            "diagnostics": {"universe": "caller_supplied_as_of_membership; not vendor historical constituents", "ranking_population": "all valid symbols including unselected"},
            "reproducibility": {"strategy_hash": compile_strategy(definition).strategy_hash, "data_snapshot_id": snapshot.id, "as_of": request.as_of.isoformat(), "request_hash": digest(request.model_dump(mode="json"))}}
