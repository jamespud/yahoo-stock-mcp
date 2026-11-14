"""Shared event kernel for long-only portfolios, broker risk and action accounting."""
from __future__ import annotations

from datetime import datetime
from itertools import groupby
from math import isfinite
from types import MappingProxyType

from .analytics import analyze
from .actions import CashDelisting, Dividend, Split
from .compiler import Plan, digest
from .data import Snapshot
from .provenance import environment
from .runtime import ExecutionConfig
from .strategy_runtime import DslDriver, StrategyDriver, StrategyView
from .sandbox import INTENTS


def run_portfolio(plans: dict[str, Plan], snapshot: Snapshot, *, config: ExecutionConfig | None = None,
                  start: datetime | None = None, end: datetime | None = None, checkpoint=None, strategy: StrategyDriver | None = None) -> dict:
    config = ExecutionConfig.model_validate((config or ExecutionConfig()).model_dump())
    if not 1 <= len(plans) <= 500:
        raise ValueError("RESOURCE_LIMIT_EXCEEDED: expected 1..500 execution symbols")
    for time in (start, end):
        if time and (time.tzinfo is None or time.utcoffset() is None):
            raise ValueError("Execution bounds must include timezone")
    if start and end and start >= end:
        raise ValueError("start must be before exclusive end")
    if config.fill_policy != "next_bar_open":
        raise ValueError("UNSUPPORTED_EXECUTION_MODEL: next_bar_open is required")
    plans = dict(sorted(plans.items()))
    bars = snapshot.bars
    positions = {symbol: {"quantity": 0., "basis": 0., "mark": 0., "realized_pnl": 0.} for symbol in plans}
    events, drivers = [], {}
    diagnostics = {"undefined_entry_decisions": 0, "undefined_exit_decisions": 0, "evaluated_decisions": 0}
    progress = 0.
    probe = (lambda: checkpoint(progress)) if checkpoint else None
    for symbol, plan in plans.items():
        rows = [b for b in bars if b.symbol == symbol and b.timeframe == plan.decision_timeframe]
        if not rows:
            raise ValueError(f"DATA_UNAVAILABLE: missing execution series {symbol}")
        if any(b.adjustment != "raw" for b in rows):
            raise ValueError("UNSUPPORTED_EXECUTION_MODEL: broker requires raw prices")
        if any(b.currency != config.currency for b in rows):
            raise ValueError("UNSUPPORTED_EXECUTION_MODEL: currency conversion is not supported")
        for dep in plan.requirements:
            if not any(b.symbol == dep["symbol"] and b.timeframe == dep["timeframe"] for b in bars):
                raise ValueError("DATA_UNAVAILABLE: required signal series is missing")
        drivers[symbol] = strategy or DslDriver(plan, snapshot, symbol, config, diagnostics, probe)
        for i, bar in enumerate(rows):
            events.extend([(bar.start_time, 4, symbol, str(i), "open", bar), (bar.available_at, 5, symbol, str(i), "decision", bar)])
    for action in snapshot.actions:
        if action.symbol not in plans:
            continue
        if isinstance(action, (Dividend, CashDelisting)) and action.currency != config.currency:
            raise ValueError("UNSUPPORTED_EXECUTION_MODEL: action currency differs from account")
        phase = 0 if isinstance(action, Split) else 1 if isinstance(action, Dividend) else 2
        events.append((action.effective_at, phase, action.symbol, action.id, "action", action))
        if isinstance(action, Dividend):
            events.append((action.pay_at, 3, action.symbol, action.id, "payment", action))
    events.sort(key=lambda e: e[:4])
    cash = config.initial_cash
    receivables, pending, last_end, last_volume = {}, {}, {}, {}
    orders, fills, curve, cash_ledger, action_ledger, exposures, risk_events = [], [], [], [], [], [], []
    delisted = {a.symbol for a in snapshot.actions if isinstance(a, CashDelisting) and start is not None and a.effective_at < start and a.symbol in plans}
    mark_times, quote_metadata = {}, {}
    processed, last_time = 0, None
    initialized = False
    simulation_start = start

    def equity():
        return cash + sum(receivables.values()) + sum(p["quantity"] * p["mark"] for p in positions.values())

    def view(now):
        return StrategyView(now, snapshot, MappingProxyType({s: p["quantity"] for s, p in positions.items()}), equity(), frozenset(pending))

    def assert_account():
        values = [cash, equity(), *receivables.values()]
        for p in positions.values():
            values.extend(p.values())
            if p["quantity"] < -1e-10 or p["basis"] < -1e-8:
                raise ArithmeticError("Negative long-only position/basis")
        if cash < -1e-8 or any(not isfinite(v) for v in values):
            raise ArithmeticError("Account invariant violated")

    def record(now, reason="market_or_account_event"):
        if (len(curve) + 1) * len(positions) > 50000:
            raise ValueError("RESOURCE_LIMIT_EXCEEDED: position snapshot budget exceeded; use a smaller period or universe")
        assert_account()
        row = {"time": now.isoformat(), "cash": cash, "receivables": sum(receivables.values()),
               "positions": {s: dict(p) for s, p in positions.items()}, "equity": equity(),
               "quotes": {s: dict(q) for s, q in quote_metadata.items()}}
        row["sampling_reason"] = reason
        curve.append(row)
        total = equity()
        exposures.append({"time": now.isoformat(), "gross": sum(p["quantity"] * p["mark"] for p in positions.values()),
                          "weights": {s: (p["quantity"] * p["mark"] / total if total > 0 else 0.) for s, p in positions.items()}})

    def risk(symbol, qty, price, side):
        notional = qty * price
        if config.max_order_notional is not None and notional > config.max_order_notional:
            return "MAX_ORDER_NOTIONAL"
        if side == "sell":
            return "INSUFFICIENT_POSITION" if qty > positions[symbol]["quantity"] else None
        if symbol in delisted:
            return "DELISTED"
        position = positions[symbol]
        fee = notional * config.commission_bps / 10000
        if notional + fee > cash + 1e-10:
            return "INSUFFICIENT_CASH"
        if position["quantity"] == 0 and sum(p["quantity"] > 0 for p in positions.values()) >= config.max_positions:
            return "MAX_POSITIONS"
        total = equity() - qty * (price - position["mark"]) - fee
        projected = (position["quantity"] + qty) * position["mark"]
        gross = sum(p["quantity"] * p["mark"] for p in positions.values()) + qty * position["mark"]
        if total <= 0 or projected > total * config.max_position_weight + 1e-8:
            return "MAX_POSITION_WEIGHT"
        if gross > total * config.max_gross_exposure + 1e-8:
            return "MAX_GROSS_EXPOSURE"
        return None

    if delisted:
        for symbol in sorted(delisted):
            action_ledger.append({"kind": "historical_delisting_state", "symbol": symbol, "time": start.isoformat(), "cash_settled": 0.})
        if len(delisted) == len(plans):
            record(start, "historical_delisting_state")

    for now, batch_iter in groupby(events, key=lambda e: e[0]):
        batch = list(batch_iter)
        progress = processed / len(events)
        processed += len(batch)
        if checkpoint:
            checkpoint(progress)
        if end and now >= end:
            break
        if start and now < start:
            continue
        last_time = now
        if simulation_start is None:
            simulation_start = now
        if not initialized:
            seen = set()
            for driver in drivers.values():
                if id(driver) not in seen:
                    driver.initialize(view(now))
                    seen.add(id(driver))
            initialized = True
        sample = any(event[4] in ("open", "decision") and event[2] not in delisted for event in batch)
        # All same-timestamp opens are visible to the broker before any matching.
        # Corporate actions run first; strategies still see only completed bars.
        opening_marks_applied = False
        closing_marks_applied = False
        for _, phase, symbol, _, kind, item in batch:
            p = positions[symbol]
            if phase >= 4 and not opening_marks_applied:
                for event in batch:
                    if event[4] == "open" and event[2] not in delisted:
                        positions[event[2]]["mark"] = event[5].open
                        mark_times[event[2]] = now
                        quote_metadata[event[2]] = {"kind": "open", "source_time": now.isoformat(), "observed_at": now.isoformat()}
                opening_marks_applied = True
            if phase >= 5 and not closing_marks_applied:
                for event in batch:
                    s, bar = event[2], event[5]
                    if event[4] == "decision" and s not in delisted and (s not in mark_times or bar.end_time > mark_times[s]):
                        positions[s]["mark"] = bar.close
                        mark_times[s] = bar.end_time
                        quote_metadata[s] = {"kind": "completed_bar", "source_time": bar.end_time.isoformat(), "observed_at": now.isoformat()}
                closing_marks_applied = True
            if kind == "action":
                sample = True
                action = item
                if symbol in delisted:
                    raise ValueError("Corporate action after delisting")
                if isinstance(action, Split):
                    p["quantity"] *= action.ratio
                    p["mark"] /= action.ratio
                    mark_times[symbol] = now
                    if symbol in last_volume:
                        last_volume[symbol] *= action.ratio
                    if symbol in pending:
                        pending[symbol]["remaining"] *= action.ratio
                        pending[symbol]["quantity"] *= action.ratio
                        pending[symbol]["filled_quantity"] *= action.ratio
                    action_ledger.append({"id": action.id, "kind": "split", "symbol": symbol, "time": now.isoformat(), "ratio": action.ratio})
                elif isinstance(action, Dividend):
                    amount = p["quantity"] * action.amount
                    receivables[action.id] = amount
                    # Carry a theoretical ex-dividend mark until a new market
                    # observation replaces it, preserving entitlement value.
                    p["mark"] = max(0., p["mark"] - action.amount)
                    mark_times[symbol] = now
                    action_ledger.append({"id": action.id, "kind": "dividend_entitlement", "symbol": symbol, "time": now.isoformat(), "quantity": p["quantity"], "amount": amount})
                else:
                    amount = p["quantity"] * action.amount
                    cash += amount
                    p["realized_pnl"] += amount - p["basis"]
                    p.update(quantity=0., basis=0., mark=action.amount)
                    cash_ledger.append({"kind": "delisting_cash", "symbol": symbol, "time": now.isoformat(), "amount": amount, "cash_after": cash})
                    action_ledger.append({"id": action.id, "kind": "cash_delisting", "symbol": symbol, "time": now.isoformat(), "amount": amount})
                    if symbol in pending:
                        pending.pop(symbol)["status"] = "cancelled"
                    delisted.add(symbol)
                mark_times[symbol] = now
                quote_metadata[symbol] = {"kind": "corporate_action", "source_time": now.isoformat(), "observed_at": now.isoformat()}
            elif kind == "payment":
                if item.id in receivables:
                    amount = receivables.pop(item.id)
                    cash += amount
                    cash_ledger.append({"kind": "dividend_payment", "action_id": item.id, "symbol": symbol, "time": now.isoformat(), "amount": amount, "cash_after": cash})
                    sample = True
            elif kind == "open" and symbol not in delisted:
                order = pending.get(symbol)
                if order is None or order["submitted_at"] >= now:
                    continue
                side = order["side"]
                qty = order["remaining"]
                if config.volume_participation is not None:
                    qty = min(qty, last_volume.get(symbol, 0.) * config.volume_participation)
                if qty <= 0:
                    continue
                price = item.open * (1 + config.slippage_bps / 10000 * (1 if side == "buy" else -1))
                reason = risk(symbol, qty, price, side)
                if reason:
                    order.update(status="rejected", reason=reason)
                    risk_events.append({"time": now.isoformat(), "symbol": symbol, "order_id": order["id"], "code": reason, "stage": "pre_fill"})
                    pending.pop(symbol)
                    continue
                fee = qty * price * config.commission_bps / 10000
                change = -qty * price - fee if side == "buy" else qty * price - fee
                cash += change
                if side == "buy":
                    p["quantity"] += qty
                    p["basis"] += qty * price + fee
                else:
                    basis = p["basis"] * qty / p["quantity"]
                    p["realized_pnl"] += qty * price - fee - basis
                    p["basis"] -= basis
                    p["quantity"] -= qty
                order["remaining"] = max(0., order["remaining"] - qty)
                order["filled_quantity"] += qty
                order["status"] = "filled" if order["remaining"] == 0. else "partially_filled"
                if order["status"] == "filled":
                    pending.pop(symbol)
                fills.append({"order_id": order["id"], "symbol": symbol, "time": now.isoformat(), "side": side, "quantity": qty, "price": price, "commission": fee})
                cash_ledger.append({"kind": "fill", "order_id": order["id"], "symbol": symbol, "time": now.isoformat(), "amount": change, "cash_after": cash})
                assert_account()
                drivers[symbol].on_fill(view(now), dict(fills[-1]))
            elif kind == "decision" and symbol not in delisted:
                if symbol in last_end and item.end_time <= last_end[symbol]:
                    continue
                last_end[symbol] = item.end_time
                volume = item.volume
                for action in snapshot.actions:
                    if isinstance(action, Split) and action.symbol == symbol and item.end_time <= action.effective_at <= now:
                        volume *= action.ratio
                last_volume[symbol] = volume
                if symbol not in mark_times or item.end_time > mark_times[symbol]:
                    p["mark"] = item.close
                    mark_times[symbol] = item.end_time
                    quote_metadata[symbol] = {"kind": "completed_bar", "source_time": item.end_time.isoformat(), "observed_at": now.isoformat()}
                sample = True
                intents = drivers[symbol].on_bar(view(now), item)
                if len(intents) > 50:
                    raise ValueError("RESOURCE_LIMIT_EXCEEDED: at most 50 intents per callback")
                for intent in INTENTS.validate_python(intents):
                    if len(orders) >= 10000:
                        raise ValueError("RESOURCE_LIMIT_EXCEEDED: total order budget exceeded")
                    target, side, qty = intent.symbol, intent.side, intent.quantity
                    if target not in plans:
                        raise ValueError("SANDBOX_POLICY_VIOLATION: order outside execution universe")
                    order = {"id": f"order_{len(orders) + 1}", "symbol": target, "side": side, "submitted_at": now,
                             "quantity": qty, "remaining": qty, "filled_quantity": 0., "status": "accepted"}
                    price = positions[target]["mark"] * (1 + config.slippage_bps / 10000 * (1 if side == "buy" else -1))
                    reason = "PENDING_ORDER" if target in pending else risk(target, qty, price, side)
                    if reason:
                        order.update(status="rejected", reason=reason)
                        risk_events.append({"time": now.isoformat(), "symbol": target, "order_id": order["id"], "code": reason, "stage": "pre_trade"})
                    else:
                        pending[target] = order
                    orders.append(order)
        assert_account()
        total = equity()
        gross = sum(p["quantity"] * p["mark"] for p in positions.values())
        if total > 0 and gross > total * config.max_gross_exposure + 1e-8:
            risk_events.append({"time": now.isoformat(), "code": "MAX_GROSS_EXPOSURE", "stage": "post_trade", "action": "reported_no_forced_liquidation"})
        if total > 0:
            for symbol, position in positions.items():
                if position["quantity"] * position["mark"] > total * config.max_position_weight + 1e-8:
                    risk_events.append({"time": now.isoformat(), "symbol": symbol, "code": "MAX_POSITION_WEIGHT", "stage": "post_trade", "action": "reported_no_forced_liquidation"})
        if sample:
            record(now)
    if not curve:
        raise ValueError("DATA_UNAVAILABLE: no completed bars/actions in execution period")
    if last_time and curve[-1]["time"] != last_time.isoformat():
        record(last_time, "terminal_account")
    for order in pending.values():
        order["status"] = "expired"
    orders = [{**o, "submitted_at": o["submitted_at"].isoformat()} for o in orders]
    analysis = analyze(curve, initial_cash=config.initial_cash, start=simulation_start, fills=fills, actions=action_ledger)
    return {**analysis, "summary": {**analysis["analytics"]["metrics"], "fill_count": len(fills)},
            "orders": orders, "fills": fills, "equity_curve": curve, "final_account": curve[-1], "diagnostics": diagnostics,
            "cash_ledger": cash_ledger, "corporate_actions": action_ledger, "exposures": exposures, "risk_events": risk_events,
            "reproducibility": {"strategy_hashes": {s: p.strategy_hash for s, p in plans.items()}, "plan_hashes": {s: p.plan_hash for s, p in plans.items()},
                                "data_snapshot_id": snapshot.id, "config_hash": digest(config.model_dump()), "currency": config.currency,
                                "engine_version": "0.4.0", "signal_backend": config.signal_backend, "environment": environment(), "drawdown_sampling": "market_opens_completed_publications_actions_and_terminal_account",
                                "valuation_policy": "latest_observed_raw_quote; equal source time open precedes completed close; one snapshot after each ordered batch"}}
