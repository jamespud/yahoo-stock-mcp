"""Shared strategy lifecycle adapters; broker/account state stays in the host."""
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Protocol

from .compiler import Plan, compile_strategy, digest
from .data import MarketBar, Snapshot
from .runtime import Evaluator, ExecutionConfig
from .sandbox import PythonArtifact, SandboxSession
from .vector import VectorEvaluator


@dataclass(frozen=True)
class StrategyView:
    now: datetime
    snapshot: Snapshot
    positions: Mapping[str, float]
    equity: float
    pending: frozenset[str]


class StrategyDriver(Protocol):
    def initialize(self, view: StrategyView) -> None: ...
    def on_bar(self, view: StrategyView, bar: MarketBar) -> list[dict]: ...
    def on_fill(self, view: StrategyView, fill: dict) -> None: ...


class DslDriver:
    def __init__(self, plan: Plan, snapshot: Snapshot, symbol: str, config: ExecutionConfig, diagnostics: dict, checkpoint=None):
        self.plan, self.config, self.diagnostics = plan, config, diagnostics
        self.evaluator = (VectorEvaluator(plan, snapshot, symbol=symbol, checkpoint=checkpoint) if config.signal_backend == "vector"
                          else Evaluator(plan, snapshot, checkpoint=checkpoint, symbol=symbol))

    def initialize(self, view):
        pass

    def on_fill(self, view, fill):
        pass

    def on_bar(self, view, bar):
        if bar.symbol in view.pending:
            return []
        self.diagnostics["evaluated_decisions"] += 1
        entry = self.evaluator.value(self.plan.entry.id, view.now)
        exit_ = self.evaluator.value(self.plan.exit.id, view.now)
        self.diagnostics["undefined_entry_decisions"] += int(entry is None)
        self.diagnostics["undefined_exit_decisions"] += int(exit_ is None)
        quantity = view.positions[bar.symbol]
        side = "sell" if quantity > 0 and exit_ is True else "buy" if quantity == 0 and entry is True else None
        return [{"symbol": bar.symbol, "side": side, "quantity": quantity if side == "sell" else self.config.quantity}] if side else []


class PythonDriver:
    def __init__(self, session: SandboxSession):
        self.session = session

    def invoke(self, event, view, **kwargs):
        return self.session.callback(event, now=view.now, snapshot=view.snapshot, positions=dict(view.positions), equity=view.equity, **kwargs)

    def initialize(self, view):
        self.invoke("initialize", view)

    def on_bar(self, view, bar):
        return self.invoke("on_bar", view, bar=bar)

    def on_fill(self, view, fill):
        self.invoke("on_fill", view, fill=fill)


def run_python_backtest(artifact: PythonArtifact, snapshot: Snapshot, *, symbols: list[str], timeframe: str,
                        image: str | None = None, config: ExecutionConfig | None = None, runtime: str | None = None,
                        start=None, end=None, checkpoint=None, random_seed: int = 0) -> dict:
    from .portfolio import run_portfolio
    config = config or ExecutionConfig()
    if config.signal_backend != "pit":
        raise ValueError("UNSUPPORTED_EXECUTION_MODEL: Python lifecycle requires the event/PIT path")
    if timeframe != artifact.decision_timeframe:
        raise ValueError("TIMEFRAME_MISMATCH: decision timeframe must match the artifact")
    # This plan supplies only the common event schedule; it is never evaluated
    # as the Python strategy. The lifecycle driver generates all order intents.
    clock = compile_strategy({"schema_version": "1.0", "name": "python-event-clock", "decision_timeframe": timeframe,
                              "entry": {"kind": "const", "value": False}, "exit": {"kind": "const", "value": False}})
    probe = (lambda: checkpoint(0.)) if checkpoint else None
    with SandboxSession(artifact, symbols=symbols, image=image, checkpoint=probe, runtime=runtime, random_seed=random_seed) as session:
        result = run_portfolio({s: clock for s in symbols}, snapshot, config=config, start=start, end=end, checkpoint=checkpoint, strategy=PythonDriver(session))
        result["reproducibility"].update(strategy_mode="python", artifact_hash=digest(artifact.model_dump()), sandbox_image=session.image,
                                            sandbox_policy="docker-v1", sandbox_runtime=session.runtime, strategy_hashes={s: digest(artifact.model_dump()) for s in symbols},
                                            random_seed=random_seed, randomness_policy="stdlib_random_seed_v1; explicit ctx.random_seed for custom RNGs")
        result["reproducibility"].pop("plan_hashes")
        return result
