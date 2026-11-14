"""Slow independent references; never import production indicators or broker.

The account oracle intentionally supports only long-only market orders, fixed
quantity, ample cash, no corporate actions and no volume/risk limits. Those are
the versioned benchmark assumptions, not a second general-purpose engine.
"""
from decimal import Decimal, localcontext


def indicator(values, operator, period):
    """Decimal textbook seeds and recurrences, rounded only at the interface."""
    if len(values) < period + (operator == "rsi"):
        return None
    with localcontext() as ctx:
        ctx.prec = 40
        xs = [Decimal(str(x)) for x in values]
        n = Decimal(period)
        if operator == "sma":
            value = sum(xs[-period:]) / n
        elif operator == "ema":
            value = sum(xs[:period]) / n
            for x in xs[period:]:
                value = (2 * x + (n - 1) * value) / (n + 1)
        elif operator == "rsi":
            changes = [b - a for a, b in zip(xs, xs[1:])]
            up = [max(x, Decimal(0)) for x in changes]
            down = [max(-x, Decimal(0)) for x in changes]
            gain, loss = sum(up[:period]) / n, sum(down[:period]) / n
            for g, l in zip(up[period:], down[period:]):
                gain, loss = ((n - 1) * gain + g) / n, ((n - 1) * loss + l) / n
            value = (Decimal(50) if gain == 0 else Decimal(100)) if loss == 0 else 100 * gain / (gain + loss)
        else:
            raise ValueError(operator)
        return float(value)


def visible(rows, symbol, tf, now):
    return sorted((b for b in rows if b.symbol == symbol and b.timeframe == tf and b.available_at <= now), key=lambda b: b.end_time)


def value(expr, rows, now, symbol, signals):
    def series(e):
        kind = e["kind"]
        if kind == "const": return [(None, e["value"])]
        if kind == "signal": return series(signals[e["name"]])
        if kind == "ref":
            history = visible(rows, symbol if e["symbol"] == "$symbol" else e["symbol"], e["timeframe"], now)
            return [(b.end_time, getattr(b, e["field"])) for b in history]
        if kind == "align":
            grid = visible(rows, symbol, e["timeframe"], now)
            if not grid: return []
            at = max(b.available_at for b in grid)
            return [(grid[-1].end_time, value(e["value"], rows, at, symbol, signals))]
        if kind == "call":
            xs = series(e["args"][0])
            return [(xs[-1][0], indicator([v for _, v in xs], e["operator"], e["params"]["period"]))] if xs else []
        a, b = series(e["left"]), series(e["right"])
        if not a or not b: return []
        if a[0][0] is None: pairs = [(t, a[0][1], v) for t, v in b]
        elif b[0][0] is None: pairs = [(t, v, b[0][1]) for t, v in a]
        else:
            other = dict(b)
            pairs = [(t, v, other[t]) for t, v in a if t in other]
        out = []
        for t, left, right in pairs:
            if left is None or right is None: answer = None
            elif e["op"] == "gt": answer = left > right
            elif e["op"] == "lt": answer = left < right
            elif e["op"] == "and": answer = left and right
            elif e["op"] == "sub": answer = left - right
            else: raise ValueError(e["op"])
            out.append((t, answer))
        return out
    result = series(expr)
    return result[-1][1] if result else None


def account(spec, rows, *, symbol="AAPL", initial_cash=100000., quantity=3., commission_bps=7., slippage_bps=11.):
    events = {}
    for b in rows:
        if b.symbol == symbol and b.timeframe == spec["decision_timeframe"]:
            events.setdefault(b.start_time, {"open": [], "close": []})["open"].append(b)
            events.setdefault(b.available_at, {"open": [], "close": []})["close"].append(b)
    cash, held, mark, basis, realized = initial_cash, 0., 0., 0., 0.
    pending, last_end, mark_time = None, None, None
    fills, orders, curve = [], [], []
    peak, drawdown = initial_cash, 0.
    for now, event in sorted(events.items()):
        for b in event["open"]:
            mark, mark_time = b.open, now
            if pending and pending[1] < now:
                side = pending[0]
                qty = quantity if side == "buy" else held
                price = b.open * (1 + slippage_bps / 10000 * (1 if side == "buy" else -1))
                fee = qty * price * commission_bps / 10000
                cash += (-qty * price if side == "buy" else qty * price) - fee
                if side == "buy":
                    held, basis = qty, qty * price + fee
                else:
                    realized += qty * price - fee - basis
                    held, basis = 0., 0.
                fills.append({"time": now.isoformat(), "side": side, "quantity": qty, "price": price, "commission": fee})
                orders[pending[2]].update(status="filled", remaining=0., filled_quantity=qty)
                pending = None
        for b in event["close"]:
            if last_end is not None and b.end_time <= last_end:
                continue
            last_end = b.end_time
            if mark_time is None or b.end_time > mark_time:
                mark, mark_time = b.close, b.end_time
            if pending:
                continue
            signals = spec.get("signals", {})
            entry = value(spec["entry"], rows, now, symbol, signals)
            exit_ = value(spec["exit"], rows, now, symbol, signals)
            if held and exit_ is True:
                pending = ("sell", now, len(orders))
                orders.append({"side": "sell", "submitted_at": now.isoformat(), "quantity": held, "remaining": held, "filled_quantity": 0., "status": "accepted"})
            elif not held and entry is True:
                pending = ("buy", now, len(orders))
                orders.append({"side": "buy", "submitted_at": now.isoformat(), "quantity": quantity, "remaining": quantity, "filled_quantity": 0., "status": "accepted"})
        equity = cash + held * mark
        peak = max(peak, equity)
        drawdown = min(drawdown, equity / peak - 1)
        curve.append({"time": now.isoformat(), "cash": cash, "quantity": held, "mark": mark,
                      "basis": basis, "realized_pnl": realized, "unrealized_pnl": held * mark - basis, "equity": equity})
    if pending:
        orders[pending[2]]["status"] = "expired"
    return {"orders": orders, "equity_curve": curve, "max_drawdown": drawdown, "unrealized_pnl": held * mark - basis,
            "fills": fills, "cash": cash, "quantity": held, "mark": mark,
            "basis": basis, "realized_pnl": realized, "equity": cash + held * mark}
