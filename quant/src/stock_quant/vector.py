"""Causal NumPy DAG backend for one monotone publication series.

Only feature computation is vectorized. All order/risk/account behavior still
belongs to the shared event broker. Unsupported temporal models fail explicitly.
"""
from __future__ import annotations

from bisect import bisect_right
from datetime import datetime
from math import isfinite

import numpy as np

from .actions import Split
from .compiler import Plan
from .data import Snapshot
from .numerics import ordered_sum


class VectorEvaluator:
    def __init__(self, plan: Plan, snapshot: Snapshot, *, symbol: str, checkpoint=None):
        self.nodes = {n.id: n for n in plan.nodes}
        self.checkpoint = checkpoint
        if checkpoint:
            checkpoint()
        if any(n.kind == "align" or n.kind == "ref" and (n.payload["symbol"] != symbol or n.timeframe != plan.decision_timeframe) for n in plan.nodes):
            raise ValueError("UNSUPPORTED_EXECUTION_MODEL: vector requires one symbol/timeframe without alignment")
        rows = [b for b in snapshot.bars if b.symbol == symbol and b.timeframe == plan.decision_timeframe]
        if not rows:
            raise ValueError("DATA_UNAVAILABLE: vector execution series is missing")
        if any(a.end_time >= b.end_time for a, b in zip(rows, rows[1:])):
            raise ValueError("UNSUPPORTED_EXECUTION_MODEL: vector requires monotone market/publication order")
        if any(n.kind == "ref" and n.payload.get("adjustment") == "split_asof" for n in plan.nodes):
            if any(b.adjustment != "raw" for b in rows) or any(isinstance(a, Split) and a.symbol == symbol for a in snapshot.actions):
                raise ValueError("UNSUPPORTED_EXECUTION_MODEL: time-varying split adjustment requires the PIT backend")
        size = len(rows)
        if size > 100000 or size * len(plan.nodes) > 2_000_000:
            raise ValueError("RESOURCE_LIMIT_EXCEEDED: vector cell budget exceeded")
        work = sum(size * (n.payload["period"] if n.kind == "call" and n.payload["operator"] == "sma" else 1) for n in plan.nodes)
        if work > 20_000_000:
            raise ValueError("RESOURCE_LIMIT_EXCEEDED: vector operation budget exceeded")
        self.times = tuple(b.available_at for b in rows)
        self.values = {}
        for node in plan.nodes:
            if checkpoint:
                checkpoint()
            with np.errstate(all="ignore"):
                if node.kind == "const":
                    out = np.full(size, node.payload["value"], dtype=np.float64)
                elif node.kind == "ref":
                    out = np.array([getattr(b, node.payload["field"]) for b in rows], dtype=np.float64)
                elif node.kind == "binary":
                    left, right = (self.values[c] for c in node.inputs)
                    op = node.payload["op"]
                    functions = {"add": np.add, "sub": np.subtract, "mul": np.multiply, "div": np.divide,
                                 "gt": np.greater, "gte": np.greater_equal, "lt": np.less, "lte": np.less_equal,
                                 "eq": np.equal, "and": np.logical_and, "or": np.logical_or}
                    out = functions[op](left, right).astype(np.float64)
                    valid = np.isfinite(left) & np.isfinite(right)
                    if op == "div":
                        valid &= right != 0
                    out[~valid] = np.nan
                elif node.kind == "call":
                    out = self._rolling(self.values[node.inputs[0]], node.payload["operator"], node.payload["period"])
                else:
                    raise ValueError("UNSUPPORTED_EXECUTION_MODEL: unknown vector node")
                out[~np.isfinite(out)] = np.nan
                out.flags.writeable = False
                self.values[node.id] = out

    def _rolling(self, source, operator, period):
        out = np.full(len(source), np.nan)
        if operator == "sma":
            count = len(source) - period + 1
            if count > 0:
                # Ordered vector additions match the PIT backend's left-to-right
                # window summation, avoiding cumsum cancellation at thresholds.
                totals = np.zeros(count)
                for offset in range(period):
                    if self.checkpoint and offset % 256 == 0:
                        self.checkpoint()
                    totals += source[offset:offset + count]
                out[period - 1:] = totals / period
            return out
        window, smooth, gain, loss, previous, gains, losses = [], None, None, None, None, [], []
        for i, raw in enumerate(source):
            if self.checkpoint and i % 256 == 0:
                self.checkpoint()
            value = float(raw)
            if not isfinite(value):
                window, smooth, gain, loss, previous, gains, losses = [], None, None, None, None, [], []
                continue
            window.append(value)
            result = None
            if operator == "ema" and len(window) >= period:
                smooth = ordered_sum(window[:period]) / period if smooth is None else smooth + (value - smooth) * 2 / (period + 1)
                result = smooth
            if operator == "rsi" and previous is not None:
                change = value - previous
                gains.append(max(change, 0.))
                losses.append(max(-change, 0.))
                if len(gains) >= period:
                    gain = ordered_sum(gains[:period]) / period if gain is None else (gain * (period - 1) + gains[-1]) / period
                    loss = ordered_sum(losses[:period]) / period if loss is None else (loss * (period - 1) + losses[-1]) / period
                    result = (50. if gain == 0 else 100.) if loss == 0 else 100 - 100 / (1 + gain / loss)
            previous = value
            if result is not None and isfinite(result):
                out[i] = result
        return out

    def value(self, node_id: str, now: datetime):
        if self.checkpoint:
            self.checkpoint()
        if self.nodes[node_id].kind == "const":
            return self.nodes[node_id].payload["value"]
        index = bisect_right(self.times, now) - 1
        if self.nodes[node_id].timeframe is None:
            index = 0
        if index < 0:
            return None
        value = float(self.values[node_id][index])
        if not isfinite(value):
            return None
        return bool(value) if self.nodes[node_id].value_type == "bool" else value
