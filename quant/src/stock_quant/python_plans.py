"""Immutable stateful Python plans pinned to operator-approved isolation policy."""
from dataclasses import asdict, dataclass
import os
import re

from .compiler import digest
from .provenance import source_identity
from .sandbox import PythonArtifact


@dataclass(frozen=True)
class PythonPlan:
    plan_hash: str
    strategy_hash: str
    decision_timeframe: str
    sandbox_image: str
    sandbox_runtime: str
    protocol_version: str
    symbol_binding: str | None
    mode: str = "python"

    @property
    def requirements(self):
        return []

    def to_dict(self):
        return asdict(self)

    @property
    def execution_identity(self):
        return (self.strategy_hash, self.sandbox_image, self.sandbox_runtime, self.protocol_version, self.decision_timeframe)


def compile_python(definition: dict, parameters: dict | None = None, symbol: str | None = None) -> PythonPlan:
    artifact = PythonArtifact.model_validate(definition)
    if parameters is not None and parameters != {}:
        raise ValueError("UNSUPPORTED_EXECUTION_MODEL: Python artifacts do not accept DSL parameter bindings")
    if symbol is not None and (not isinstance(symbol, str) or not 1 <= len(symbol) <= 64 or symbol == "$symbol"):
        raise ValueError("UNKNOWN_REFERENCE: invalid symbol binding")
    image = os.environ.get("QUANT_SANDBOX_IMAGE", "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise ValueError("SANDBOX_UNAVAILABLE: operator must approve an immutable sandbox image")
    runtime = os.environ.get("QUANT_SANDBOX_RUNTIME", "runc")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", runtime):
        raise ValueError("SANDBOX_UNAVAILABLE: invalid operator runtime")
    protocol = source_identity("python_plans", "sandbox", "sandbox_guest", "sandbox_watchdog", "strategy_runtime")
    strategy_hash = digest(artifact.model_dump())
    plan_hash = digest({"artifact": strategy_hash, "image": image, "runtime": runtime, "protocol": protocol, "symbol_binding": symbol})
    return PythonPlan(plan_hash, strategy_hash, artifact.decision_timeframe, image, runtime, protocol, symbol)
