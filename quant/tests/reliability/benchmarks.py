"""Versioned benchmark strategies; public input is the checked-in JSON catalog."""
import json
from pathlib import Path

CATALOG = Path(__file__).parents[2] / "benchmarks" / "strategies_v1.json"


def catalog():
    return json.loads(CATALOG.read_text())


def definitions():
    def ref(tf, symbol="$symbol"):
        return {"kind": "ref", "symbol": symbol, "timeframe": tf, "field": "close"}
    def const(x): return {"kind": "const", "value": x}
    def binary(op, a, b): return {"kind": "binary", "op": op, "left": a, "right": b}
    def call(op, tf, p): return {"kind": "call", "operator": op, "args": [ref(tf)], "params": {"period": p}}
    def aligned(tf, target): return {"kind": "align", "value": ref(tf), "timeframe": target, "policy": "last_completed"}
    cases = []
    def add(name, tf, a, b, *, entry="gt", exit="lt"):
        cases.append({"id": name, "strategy": {"schema_version": "1.0", "name": name, "decision_timeframe": tf,
                      "entry": binary(entry, a, b), "exit": binary(exit, a, b)}})
    cases.append({"id": "buy_hold_daily", "strategy": {"schema_version": "1.0", "name": "buy_hold_daily", "decision_timeframe": "1d", "entry": const(True), "exit": const(False)}})
    add("sma_trend_daily", "1d", ref("1d"), call("sma", "1d", 3))
    add("ema_trend_hourly", "1h", ref("1h"), call("ema", "1h", 4))
    add("rsi_reversion_15m", "15m", call("rsi", "15m", 5), const(50), entry="lt", exit="gt")
    add("dual_sma_hourly", "1h", call("sma", "1h", 2), call("sma", "1h", 7))
    add("daily_confirmation_hourly", "1h", ref("1h"), aligned("1d", "1h"))
    add("hourly_confirmation_15m", "15m", ref("15m"), aligned("1h", "15m"))
    # Three actual data dependencies; offsets normalize the artificial price levels.
    relative = binary("sub", binary("sub", ref("1d", "MSFT"), const(23)), binary("sub", ref("1d", "NVDA"), const(46)))
    add("three_asset_relative_daily", "1d", binary("sub", ref("1d"), const(90)), relative)
    return cases
