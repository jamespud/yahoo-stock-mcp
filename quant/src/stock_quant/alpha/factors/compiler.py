"""Deterministic FactorSpec validation and topological compilation."""
from __future__ import annotations

from dataclasses import dataclass

from ...compiler import digest
from .ast import FactorSpec
from .operators import OPERATORS, operator_version

BASE_FIELDS = {"open", "high", "low", "close", "volume"}
COMPILER_VERSION = "factor-compiler-v1"


class FactorCompileError(ValueError):
    def __init__(self, code: str, path: str, message: str):
        self.diagnostic = {"code": code, "path": path, "message": message}
        super().__init__(message)


@dataclass(frozen=True)
class CompiledFactorSpec:
    spec: FactorSpec
    sorted_nodes: tuple[str, ...]
    node_map: dict
    lookback: dict[str, int]
    spec_hash: str


def _validate_params(node):
    definition = OPERATORS.get(node.operator)
    if definition is None:
        raise FactorCompileError("FACTOR_OPERATOR_UNKNOWN", f"nodes.{node.id}.operator", "Unknown operator")
    if len(node.inputs) != definition.arity:
        raise FactorCompileError("FACTOR_ARITY_INVALID", f"nodes.{node.id}.inputs", "Input count does not match operator")
    keys = set(node.params)
    if not set(definition.required).issubset(keys) or not keys.issubset(set(definition.allowed)):
        raise FactorCompileError("FACTOR_PARAMETER_INVALID", f"nodes.{node.id}.params", "Missing or unknown parameters")
    if "period" in keys and (type(node.params["period"]) is not int or node.params["period"] < 1 or node.params["period"] > 10000):
        raise FactorCompileError("FACTOR_PARAMETER_INVALID", f"nodes.{node.id}.params.period", "period must be an integer in 1..10000")
    if node.operator == "cs_winsorize":
        lower, upper = node.params.get("lower"), node.params.get("upper")
        if type(lower) not in (int, float) or type(upper) not in (int, float) or not 0 <= lower < upper <= 1:
            raise FactorCompileError("FACTOR_PARAMETER_INVALID", f"nodes.{node.id}.params", "Expected 0 <= lower < upper <= 1")


def compile_factor_spec(spec: FactorSpec) -> CompiledFactorSpec:
    ids = [node.id for node in spec.nodes]
    if len(ids) != len(set(ids)):
        raise FactorCompileError("FACTOR_ID_DUPLICATE", "nodes", "Node IDs must be unique")
    node_map = {node.id: node for node in spec.nodes}
    dependencies = {}
    for node in spec.nodes:
        _validate_params(node)
        deps = set()
        for index, reference in enumerate(node.inputs):
            if reference in BASE_FIELDS:
                continue
            if reference not in node_map:
                raise FactorCompileError("FACTOR_REFERENCE_MISSING", f"nodes.{node.id}.inputs.{index}", "Referenced node does not exist")
            deps.add(reference)
        dependencies[node.id] = deps
    for output in spec.outputs:
        if output not in node_map:
            raise FactorCompileError("FACTOR_OUTPUT_MISSING", "outputs", "Output is not a node")
    remaining = set(ids)
    ready = sorted(node_id for node_id in remaining if not dependencies[node_id])
    ordered = []
    while ready:
        node_id = ready.pop(0)
        ordered.append(node_id)
        remaining.remove(node_id)
        for candidate in sorted(remaining):
            dependencies[candidate].discard(node_id)
            if not dependencies[candidate] and candidate not in ready:
                ready.append(candidate)
        ready.sort()
    if remaining:
        raise FactorCompileError("FACTOR_DEPENDENCY_CYCLE", "nodes", "Factor graph contains a cycle")
    lookback = {}
    for node_id in ordered:
        node = node_map[node_id]
        own = int(node.params.get("period", 0)) if node.operator in OPERATORS else 0
        deps = [lookback[ref] for ref in node.inputs if ref in lookback]
        lookback[node_id] = own + (max(deps) if deps else 0)
    canonical = spec.model_dump(mode="json")
    spec_hash = digest({"compiler": COMPILER_VERSION, "spec": canonical, "operators": {name: operator_version(name) for name in OPERATORS}})
    return CompiledFactorSpec(spec=spec, sorted_nodes=tuple(ordered), node_map=node_map, lookback=lookback, spec_hash=spec_hash)
