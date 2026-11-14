"""Typed, content-addressed DAG compilation with explicit temporal alignment."""
from __future__ import annotations

from dataclasses import dataclass, asdict
import hashlib
import json
from math import isfinite
from typing import Any

from pydantic import ValidationError

from .contracts import Align, Binary, Call, Const, Expr, Period, Ref, Signal, Strategy

from .provenance import source_identity

COMPILER_VERSION = "1.0+" + source_identity("compiler", "contracts").removeprefix("sha256:")
OPERATORS = {"sma": "2.0", "ema": "2.0", "rsi": "2.0"}
MINUTES = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "1d": 1440, "1w": 10080}


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class CompileError(ValueError):
    def __init__(self, code: str, path: str, message: str):
        self.diagnostic = {"code": code, "path": path, "message": message}
        super().__init__(message)


@dataclass(frozen=True)
class Node:
    id: str
    kind: str
    value_type: str
    shape: str
    timeframe: str | None
    inputs: tuple[str, ...]
    payload: dict
    lookback: int
    availability: str = "available_at"
    stateful: bool = False
    deterministic: bool = True


@dataclass(frozen=True)
class Plan:
    plan_hash: str
    strategy_hash: str
    compiler_version: str
    decision_timeframe: str
    nodes: tuple[Node, ...]
    entry: Node
    exit: Node
    requirements: list[dict]
    operator_versions: dict[str, str]
    parameters: dict[str, int | float]
    symbol_binding: str | None
    named_nodes: dict[str, str]
    grid_requirements: list[dict]

    def to_dict(self) -> dict:
        return asdict(self)


def compile_strategy(raw: dict, parameters: dict | None = None, symbol: str | None = None) -> Plan:
    original = Strategy.model_validate(raw)
    parameters = {} if parameters is None else parameters
    if not isinstance(parameters, dict) or set(parameters) - set(original.parameters):
        raise CompileError("UNKNOWN_PARAMETER", "$.parameters", "Unknown parameter override")
    bound = {}
    for name, definition in original.parameters.items():
        value = parameters.get(name, definition.default)
        if type(value) not in (int, float) or (definition.value_type == "integer" and type(value) is not int) or not definition.minimum <= value <= definition.maximum:
            raise CompileError("PARAMETER_OUT_OF_RANGE", f"$.parameters.{name}", "Parameter type/range mismatch")
        bound[name] = value
    if symbol is not None and (not isinstance(symbol, str) or not 1 <= len(symbol) <= 64 or symbol == "$symbol"):
        raise CompileError("UNKNOWN_REFERENCE", "$.symbol", "Invalid symbol binding")
    def bind(value, path="$", in_operator_params=False):
        if isinstance(value, list):
            return [bind(v, f"{path}[{i}]") for i, v in enumerate(value)]
        if not isinstance(value, dict):
            return value
        if value.get("kind") == "param":
            name = value["name"]
            if name not in bound:
                raise CompileError("UNKNOWN_PARAMETER", path, f"Unknown parameter {name}")
            return bound[name] if in_operator_params else {"kind": "const", "value": bound[name]}
        out = {}
        for key, v in value.items():
            if key == "params" and value.get("kind") == "call":
                out[key] = {p: bind(x, f"{path}.params.{p}", True) for p, x in v.items()}
            elif key == "symbol" and value.get("kind") == "ref" and v == "$symbol" and symbol is not None:
                out[key] = symbol
            else:
                out[key] = bind(v, f"{path}.{key}")
        return out
    strategy = Strategy.model_validate(bind(original.model_dump()))
    nodes: dict[str, Node] = {}
    signals: dict[str, Node] = {}
    visiting: set[str] = set()
    versions: dict[str, str] = {}
    # The last component preserves alignment boundaries even when source and
    # target timeframe labels coincide (possibly on different symbols).
    dependencies: dict[str, dict[tuple[str, str, str, bool], int]] = {}
    grids: dict[str, dict[str, int]] = {}
    budget = 0

    def fail(code, path, message):
        raise CompileError(code, path, message)

    def emit(kind, value_type, timeframe, inputs=(), payload=None, extra=0):
        payload = payload or {}
        key = digest([kind, value_type, timeframe, [n.id for n in inputs], payload])
        if key not in nodes:
            deps = {}
            grid = {}
            for n in inputs:
                for dep, count in dependencies[n.id].items():
                    dest = (*dep[:3], True) if kind == "align" else dep
                    deps[dest] = max(deps.get(dest, 0), count + (extra if not dep[3] else 0))
                for period, count in grids[n.id].items():
                    grid[period] = max(grid.get(period, 0), count + (extra if period == timeframe else 0))
            if kind == "ref":
                deps[(payload["symbol"], payload["field"], timeframe, False)] = 1
            if kind == "align":
                grid[timeframe] = max(grid.get(timeframe, 0), 1)
            grids[key] = grid
            dependencies[key] = deps
            nodes[key] = Node(key, kind, value_type, "series" if timeframe else "scalar", timeframe,
                              tuple(n.id for n in inputs), payload, max([*deps.values(), *grid.values()], default=0))
        return nodes[key]

    def visit(expr: Expr, path: str, depth=0) -> Node:
        nonlocal budget
        budget += 1
        if depth > 64 or budget > 10000:
            fail("RESOURCE_LIMIT_EXCEEDED", path, "Expression budget exceeded")
        if isinstance(expr, Const):
            if type(expr.value) is bool:
                return emit("const", "bool", None, payload={"value": expr.value})
            try:
                value = float(expr.value)
            except OverflowError:
                fail("SCHEMA_INVALID", path, "Numeric literals must fit finite binary64")
            if not isfinite(value):
                fail("SCHEMA_INVALID", path, "Numeric literals must fit finite binary64")
            return emit("const", "float", None, payload={"value": value})
        if isinstance(expr, Ref):
            return emit("ref", "float", expr.timeframe, payload={"symbol": expr.symbol, "field": expr.field, "adjustment": expr.adjustment})
        if isinstance(expr, Signal):
            if expr.name not in strategy.signals:
                fail("UNKNOWN_REFERENCE", path, f"Unknown signal {expr.name}")
            if expr.name in visiting:
                fail("CYCLIC_DEPENDENCY", path, f"Cycle at {expr.name}")
            if expr.name not in signals:
                visiting.add(expr.name)
                signals[expr.name] = visit(strategy.signals[expr.name], f"$.signals.{expr.name}", depth + 1)
                visiting.remove(expr.name)
            return signals[expr.name]
        if isinstance(expr, Align):
            n = visit(expr.value, path + ".value", depth + 1)
            if n.timeframe is None or MINUTES[n.timeframe] < MINUTES[expr.timeframe]:
                fail("TIMEFRAME_MISMATCH", path, "Alignment requires a series at the same or coarser timeframe; use resampling for aggregation")
            return emit("align", n.value_type, expr.timeframe, (n,), {"policy": expr.policy})
        if isinstance(expr, Call):
            if expr.operator not in OPERATORS:
                fail("UNKNOWN_OPERATOR", path, f"Unknown operator {expr.operator}")
            try:
                period = Period.model_validate(expr.params).period
            except ValidationError as error:
                fail("SCHEMA_INVALID", path + ".params", str(error))
            if len(expr.args) != 1:
                fail("TYPE_MISMATCH", path, "Expected one series argument")
            n = visit(expr.args[0], path + ".args[0]", depth + 1)
            if n.value_type != "float" or n.timeframe is None:
                fail("TYPE_MISMATCH", path, "Operator requires a numeric series")
            versions[expr.operator] = OPERATORS[expr.operator]
            # RSI needs period changes, hence period + 1 prices.
            extra = period if expr.operator == "rsi" else period - 1
            return emit("call", "float", n.timeframe, (n,), {"operator": expr.operator, "period": period, "version": OPERATORS[expr.operator]}, extra)
        if isinstance(expr, Binary):
            left, right = visit(expr.left, path + ".left", depth + 1), visit(expr.right, path + ".right", depth + 1)
            if left.timeframe and right.timeframe and left.timeframe != right.timeframe:
                fail("TIMEFRAME_MISMATCH", path, "Mixed periods require explicit last_completed alignment")
            required = "bool" if expr.op in ("and", "or") else "float"
            if expr.op == "eq":
                if left.value_type != right.value_type:
                    fail("TYPE_MISMATCH", path, "Equality operands must have the same type")
            elif left.value_type != required or right.value_type != required:
                fail("TYPE_MISMATCH", path, f"{expr.op} requires {required} operands")
            result_type = "float" if expr.op in ("add", "sub", "mul", "div") else "bool"
            return emit("binary", result_type, left.timeframe or right.timeframe, (left, right), {"op": expr.op})
        fail("SCHEMA_INVALID", path, "Unsupported expression")

    for name in strategy.signals:
        visit(Signal(kind="signal", name=name), f"$.signals.{name}")
    entry, exit_ = visit(strategy.entry, "$.entry"), visit(strategy.exit, "$.exit")
    for name, node in (("entry", entry), ("exit", exit_)):
        if node.value_type != "bool":
            fail("TYPE_MISMATCH", f"$.{name}", "Expected Bool, got Float")
        if node.timeframe and node.timeframe != strategy.decision_timeframe:
            fail("TIMEFRAME_MISMATCH", f"$.{name}", "Condition must match decision timeframe")
    merged = {}
    for deps in dependencies.values():
        for dep, count in deps.items():
            merged[dep[:3]] = max(merged.get(dep[:3], 0), count)
    requirements = [{"symbol": s, "field": f, "timeframe": t, "warmup_bars": merged[(s, f, t)]} for s, f, t in sorted(merged)]
    grid_requirements = [{"timeframe": t, "warmup_bars": max(g.get(t, 0) for g in grids.values())}
                         for t in sorted({t for g in grids.values() for t in g})]
    strategy_hash = digest(original.model_dump())
    plan_hash = digest({"strategy": strategy_hash, "compiler": COMPILER_VERSION, "operators": versions, "parameters": bound, "symbol_binding": symbol})
    return Plan(plan_hash, strategy_hash, COMPILER_VERSION, strategy.decision_timeframe, tuple(nodes.values()), entry, exit_, requirements, versions, bound, symbol, {name: node.id for name, node in signals.items()}, grid_requirements)


def validate_strategy(raw: dict, parameters: dict | None = None, symbol: str | None = None) -> dict:
    try:
        plan = compile_strategy(raw, parameters, symbol)
        return {"valid": True, "errors": [], "warnings": [], "requirements": plan.requirements, "grid_requirements": plan.grid_requirements, "plan_hash": plan.plan_hash}
    except CompileError as error:
        errors = [error.diagnostic]
    except (ValidationError, RecursionError) as error:
        errors = [{"code": "SCHEMA_INVALID", "path": "$", "message": str(error)}]
    return {"valid": False, "errors": errors, "warnings": [], "requirements": []}
