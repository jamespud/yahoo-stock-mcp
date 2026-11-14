"""Point-in-time DSL evaluation and single-asset facade for the shared kernel."""
from __future__ import annotations

from datetime import datetime
from bisect import bisect_right
from functools import cache
from math import isfinite
import operator
from typing import Literal

from pydantic import Field

from .contracts import StrictModel
from .compiler import Plan
from .data import Snapshot
from .actions import Split
from .numerics import RollingState


class ExecutionConfig(StrictModel):
    signal_backend: Literal["pit", "vector"] = "pit"
    initial_cash: float = Field(default=10000., gt=0, strict=True)
    quantity: float = Field(default=1., gt=0, strict=True)
    commission_bps: float = Field(default=0., ge=0, le=10000, strict=True)
    slippage_bps: float = Field(default=0., ge=0, lt=10000, strict=True)
    fill_policy: str = "next_bar_open"
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    max_positions: int = Field(default=500, ge=1, le=500, strict=True)
    max_position_weight: float = Field(default=1., gt=0, le=1, strict=True)
    max_gross_exposure: float = Field(default=1., gt=0, le=1, strict=True)
    max_order_notional: float | None = Field(default=None, gt=0, strict=True)
    volume_participation: float | None = Field(default=None, gt=0, le=1, strict=True)


class Evaluator:
    def __init__(self, plan: Plan, snapshot: Snapshot, checkpoint=None, *, symbol: str | None = None):
        self.nodes = {n.id: n for n in plan.nodes}
        if checkpoint:
            checkpoint()
        @cache
        def aligned(key):
            if checkpoint:
                checkpoint()
            return self.nodes[key].kind == "align" or any(aligned(c) for c in self.nodes[key].inputs)
        self.aligned = {n.id: aligned(n.id) for n in plan.nodes}
        symbols = {n.payload["symbol"] for n in plan.nodes if n.kind == "ref"}
        self.symbol = symbol or plan.symbol_binding or (next(iter(symbols)) if len(symbols) == 1 else None)
        self.grids = {}
        self.grid_requirements = plan.grid_requirements
        for requirement in plan.grid_requirements:
            if self.symbol is None:
                raise ValueError("DATA_UNAVAILABLE: alignment grid requires an explicit execution symbol")
            rows = [b for b in snapshot.bars if b.symbol == self.symbol and b.timeframe == requirement["timeframe"]]
            if not rows:
                raise ValueError("DATA_UNAVAILABLE: alignment grid series is missing")
            # Match the event kernel: an old bar arriving late cannot cause a
            # second decision on a market interval already passed. One sample
            # per publication instant, using the latest completed interval.
            grid, last_end = {}, None
            for bar in rows:
                if checkpoint:
                    checkpoint()
                if last_end is None or bar.end_time > last_end:
                    grid[bar.available_at] = bar.end_time
                    last_end = bar.end_time
            self.grids[requirement["timeframe"]] = tuple((t, end) for t, end in grid.items())
        self.grid_times = {tf: tuple(t for t, _ in grid) for tf, grid in self.grids.items()}
        self.snapshot = snapshot
        self.checkpoint = checkpoint

    def grid_dependencies(self, now: datetime) -> list[dict]:
        return [{"symbol": self.symbol, **requirement,
                 "available_bars": sum(instant <= now for instant, _ in self.grids[requirement["timeframe"]])}
                for requirement in self.grid_requirements]

    def value(self, node_id: str, now: datetime):
        """Evaluate against a fresh PIT view; no cache survives the decision clock."""
        memo = {}
        aligned_cache, rolling_states = {}, {}
        work = 0

        def probe():
            nonlocal work
            work += 1
            if work > 2_000_000:
                raise ValueError("RESOURCE_LIMIT_EXCEEDED: decision-grid evaluation budget exceeded")
            if self.checkpoint and work % 256 == 0:
                self.checkpoint()

        def reference(n, at, count=100000):
            out = []
            for b in self.snapshot.history(n.payload["symbol"], n.timeframe, at, count):
                probe()
                value = getattr(b, n.payload["field"])
                if n.payload.get("adjustment", "raw") == "split_asof":
                    if b.adjustment != "raw":
                        raise ValueError("DATA_UNAVAILABLE: split_asof requires raw input prices")
                    for action in self.snapshot.actions:
                        if isinstance(action, Split) and action.symbol == b.symbol and action.effective_at <= at:
                            if b.start_time < action.effective_at < b.end_time:
                                raise ValueError("DATA_UNAVAILABLE: a bar straddles a split; finer input is required")
                            if b.end_time <= action.effective_at:
                                value = value * action.ratio if n.payload["field"] == "volume" else value / action.ratio
                out.append((b.end_time, value))
            return out

        def point(key, at):
            """Read a single causal sample without materializing grid prefixes."""
            probe()
            n = self.nodes[key]
            if self.aligned[key]:
                # Append only the child prefix actually demanded by this parent.
                # Evaluating unused later samples could leak their exceptions.
                rows = aligned_series(key, at)
                index = bisect_right(self.grid_times[n.timeframe], at) - 1
                return rows[index] if index >= 0 else (None, None)
            rows = reference(n, at, 1) if n.kind == "ref" else series(key, at)
            return rows[-1] if rows else (None, None)

        functions = {"add": operator.add, "sub": operator.sub, "mul": operator.mul, "div": operator.truediv,
                     "gt": operator.gt, "gte": operator.ge, "lt": operator.lt, "lte": operator.le,
                     "eq": operator.eq, "and": operator.and_, "or": operator.or_}

        def binary(op, left, right):
            if left is None or right is None or (op == "div" and right == 0):
                return None
            value = functions[op](left, right)
            return value if isinstance(value, bool) or isfinite(value) else None

        def aligned_series(key, at):
            n = self.nodes[key]
            out = aligned_cache.setdefault(key, [])
            stop = bisect_right(self.grid_times[n.timeframe], at)
            for index in range(len(out), stop):
                probe()
                instant, end = self.grids[n.timeframe][index]
                if n.kind == "align":
                    value = point(n.inputs[0], instant)[1]
                elif n.kind == "binary":
                    t1, left = point(n.inputs[0], instant)
                    t2, right = point(n.inputs[1], instant)
                    value = binary(n.payload["op"], left, right) if t1 is None or t2 is None or t1 == t2 else None
                elif n.kind == "call":
                    state = rolling_states.setdefault(key, RollingState(n.payload["operator"], n.payload["period"]))
                    value = state.push(point(n.inputs[0], instant)[1])
                else:
                    raise ValueError("Unsupported aligned node")
                out.append((end, value))
            return out

        def series(key, at):
            if self.aligned[key]:
                return aligned_series(key, at)
            identity = (key, at)
            if identity in memo:
                return memo[identity]
            probe()
            if self.checkpoint:
                self.checkpoint()
            n = self.nodes[key]
            if n.kind == "const":
                out = [(None, n.payload["value"])]
            elif n.kind == "ref":
                out = reference(n, at)
            elif n.kind == "binary":
                a, b = series(n.inputs[0], at), series(n.inputs[1], at)
                if not a or not b:
                    out = []
                else:
                    op = n.payload["op"]
                    if a[0][0] is None:
                        pairs = [(t, a[0][1], v) for t, v in b]
                    elif b[0][0] is None:
                        pairs = [(t, v, b[0][1]) for t, v in a]
                    else:
                        right = dict(b)
                        pairs = [(t, v, right[t]) for t, v in a if t in right]
                    out = []
                    for t, x, y in pairs:
                        probe()
                        out.append((t, binary(op, x, y)))
            elif n.kind == "call":
                rows = series(n.inputs[0], at)
                state = RollingState(n.payload["operator"], n.payload["period"])
                out = []
                for t, value in rows:
                    probe()
                    out.append((t, state.push(value)))
            else:
                raise ValueError(f"Unsupported node {n.kind}")
            memo[identity] = out
            return out

        if self.aligned[node_id]:
            return point(node_id, now)[1]
        rows = series(node_id, now)
        return rows[-1][1] if rows else None


def run_backtest(plan: Plan, snapshot: Snapshot, *, symbol: str, timeframe: str, config: ExecutionConfig | None = None, start: datetime | None = None, end: datetime | None = None, checkpoint=None) -> dict:
    """Single-asset compatibility surface on the shared portfolio event kernel."""
    from .portfolio import run_portfolio
    if timeframe != plan.decision_timeframe:
        raise ValueError("TIMEFRAME_MISMATCH: execution must match plan decision timeframe")
    result = run_portfolio({symbol: plan}, snapshot, config=config, start=start, end=end, checkpoint=checkpoint)
    # Keep the original single-asset account fields alongside the shared ledger.
    for row in result["equity_curve"]:
        position = row["positions"][symbol]
        row.update(quantity=position["quantity"], mark=position["mark"])
    result["final_account"] = result["equity_curve"][-1]
    result["reproducibility"].update(strategy_hash=plan.strategy_hash, plan_hash=plan.plan_hash)
    return result
