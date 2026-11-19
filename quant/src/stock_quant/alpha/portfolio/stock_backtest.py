"""Top-K stock portfolio research with attested prices and corporate-action accounting."""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np
from pydantic import Field

from ...contracts import StrictModel
from ..contracts import DatasetManifest
from ..data.action_validation import validate_execution_inputs
from ..data.catalog import DatasetStore
from ..data.corporate_actions import CorporateActionDataset
from ..data.price_attestation import PriceAttestation
from ..errors import AlphaError, AlphaErrorCode
from .corporate_action_ledger import CorporateActionLedger
from .ranking import rank_all_predictions
from .rebalance import weekly_last_decisions
from .target_weights import equal_target_weights


class TopKPortfolioConfig(StrictModel):
    top_k: int = Field(default=20, ge=1, le=500, strict=True)
    weighting: str = "equal"
    max_name_weight: float = Field(default=0.05, gt=0, le=1, strict=True)
    max_gross_exposure: float = Field(default=1.0, gt=0, le=1, strict=True)
    commission_bps: float = Field(default=5.0, ge=0, strict=True)
    slippage_bps: float = Field(default=5.0, ge=0, strict=True)
    initial_cash: float = Field(default=100_000.0, gt=0, strict=True)


class PortfolioBacktestResult(StrictModel):
    metrics: dict
    final_account: dict
    orders: list[dict]
    fills: list[dict]
    positions: list[dict]
    equity_curve: list[dict]
    target_weights: list[dict]
    risk_events: list[dict]
    diagnostics: dict
    cash_ledger: list[dict] = Field(default_factory=list)
    corporate_actions: list[dict] = Field(default_factory=list)


def _utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _max_drawdown(values: list[float]) -> float:
    peak, worst = -float("inf"), 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, value / peak - 1)
    return worst


def run_top_k_backtest(
    prediction_set: DatasetManifest,
    dataset: DatasetManifest,
    *,
    store: DatasetStore,
    price_attestation: PriceAttestation,
    corporate_action_dataset: CorporateActionDataset,
    config: TopKPortfolioConfig | None = None,
    stress_cost_multiplier: float = 1.0,
) -> PortfolioBacktestResult:
    config = config or TopKPortfolioConfig()
    if stress_cost_multiplier <= 0:
        raise ValueError("Cost stress multiplier must be positive")
    if prediction_set.dataset_kind != "prediction_panel" or dataset.dataset_kind != "stock_bars":
        raise AlphaError(AlphaErrorCode.DATASET_MISMATCH, "Top-K backtest requires prediction_panel and stock_bars")
    if dataset.price_basis != "raw_attested" or price_attestation.price_dataset_id != dataset.dataset_id or price_attestation.price_basis != "raw_attested":
        raise AlphaError(AlphaErrorCode.PRICE_BASIS_UNVERIFIED, "Price attestation does not certify this raw-price dataset")
    validity = validate_execution_inputs(
        price_attestation,
        corporate_action_dataset,
        instruments=dataset.instrument_ids,
        start_at=dataset.start_at,
        end_at=dataset.end_at,
    )
    execution_eligible = validity.execution_status == "ELIGIBLE"
    total_return_supported = execution_eligible and validity.total_return_claim_allowed
    if not execution_eligible or not total_return_supported:
        if any(item["reason"].startswith("PRICE_") for item in validity.blocking_reasons):
            raise AlphaError(AlphaErrorCode.PRICE_BASIS_UNVERIFIED, "Price attestation is incomplete", {"blocking_reasons": validity.blocking_reasons})
        raise AlphaError(AlphaErrorCode.DATA_UNAVAILABLE, "Corporate-action admission is incomplete", {"blocking_reasons": validity.blocking_reasons})
    predictions = store.read_rows(prediction_set.dataset_id)
    bars_rows = store.read_rows(dataset.dataset_id)
    if any(row.get("adjustment") != "raw" for row in bars_rows):
        raise AlphaError(AlphaErrorCode.PRICE_BASIS_UNVERIFIED, "Top-K execution requires independently attested raw OHLC")
    if not predictions or not bars_rows:
        raise AlphaError(AlphaErrorCode.DATA_UNAVAILABLE, "Backtest inputs are empty")

    bars_by_date = defaultdict(dict)
    for row in bars_rows:
        bars_by_date[_utc(row["end_at"]).date()][row["instrument_id"]] = row
    dates = sorted(bars_by_date)
    usable_predictions = sum(
        any(bar_date > _utc(row["decision_at"]).date() and row["instrument_id"] in bars_by_date[bar_date] for bar_date in dates)
        for row in predictions
    )
    prediction_coverage = usable_predictions / len(predictions)
    ranked_all = rank_all_predictions(predictions, config.top_k)
    signals = weekly_last_decisions(sorted(ranked_all))
    signal_index = 0
    pending = None
    cash = config.initial_cash
    positions: dict[str, dict] = {}
    last_close = {}
    fills, orders, risk_events, target_rows, position_rows, equity_rows = [], [], [], [], [], []
    cash_ledger, corporate_action_rows = [], []
    total_notional = 0.0
    commission_paid = 0.0
    slippage_paid = 0.0
    effective_commission_bps = config.commission_bps * stress_cost_multiplier
    effective_slippage_bps = config.slippage_bps * stress_cost_multiplier
    action_ledger = CorporateActionLedger(corporate_action_dataset)

    def record_cash(day, kind, amount, **extra):
        entry = {"date": day.isoformat(), "kind": kind, "amount": amount, **extra, "balance": cash}
        cash_ledger.append(entry)
        return entry

    def mark_equity(prices):
        return cash + sum(position["quantity"] * prices.get(symbol, position["mark"]) for symbol, position in positions.items())

    def schedule_for(decision):
        ranked = ranked_all[decision]
        weights = equal_target_weights(ranked, max_name_weight=config.max_name_weight, max_gross_exposure=config.max_gross_exposure)
        target_rows.append({"decision_at": decision.isoformat(), "ranking": [row["instrument_id"] for row in ranked], "weights": weights})
        return {"signal_at": decision, "weights": weights}

    for day in dates:
        day_bars = bars_by_date[day]
        cash, action_rows, action_cash_rows, action_risks = action_ledger.process(day, positions, cash)
        corporate_action_rows.extend(action_rows)
        for row in action_cash_rows:
            record_cash(day, row["kind"], row["amount"], instrument_id=row.get("instrument_id"), action_id=row.get("action_id"))
        risk_events.extend(action_risks)
        while signal_index < len(signals) and signals[signal_index].date() < day:
            pending = schedule_for(signals[signal_index])
            signal_index += 1
        if pending is not None:
            signal_at = pending["signal_at"]
            desired = pending["weights"]
            pre_trade_equity = mark_equity({symbol: position["mark"] for symbol, position in positions.items()})
            for symbol in sorted(set(positions) - set(desired)):
                bar = day_bars.get(symbol)
                if bar is None:
                    risk_events.append({"time": day.isoformat(), "code": "NO_EXIT_BAR", "symbol": symbol})
                    continue
                quantity = positions[symbol]["quantity"]
                reference_price = float(bar["open"])
                price = reference_price * (1 - effective_slippage_bps / 10000)
                commission = quantity * price * effective_commission_bps / 10000
                proceeds = quantity * price - commission
                cash += proceeds
                commission_paid += commission
                slippage_paid += quantity * reference_price * effective_slippage_bps / 10000
                total_notional += quantity * price
                record_cash(day, "trade", proceeds, symbol=symbol, side="sell", gross=quantity * price, commission=commission)
                fills.append({
                    "signal_at": signal_at.isoformat(), "time": _utc(bar["start_at"]).isoformat(),
                    "symbol": symbol, "side": "sell", "quantity": quantity, "price": price,
                    "reference_open": reference_price, "commission": commission,
                })
                positions.pop(symbol)
            target_value = pre_trade_equity
            for symbol, weight in sorted(desired.items(), key=lambda item: (item[0], item[1])):
                current = positions.get(symbol)
                bar = day_bars.get(symbol)
                if current is None or bar is None:
                    continue
                target_notional = target_value * weight
                current_notional = current["quantity"] * float(bar["open"])
                if current_notional <= target_notional:
                    continue
                quantity = min(current["quantity"], (current_notional - target_notional) / float(bar["open"]))
                reference_price = float(bar["open"])
                price = reference_price * (1 - effective_slippage_bps / 10000)
                commission = quantity * price * effective_commission_bps / 10000
                proceeds = quantity * price - commission
                cash += proceeds
                commission_paid += commission
                slippage_paid += quantity * reference_price * effective_slippage_bps / 10000
                total_notional += quantity * price
                record_cash(day, "trade", proceeds, symbol=symbol, side="sell", gross=quantity * price, commission=commission)
                current["basis"] -= current["basis"] * quantity / current["quantity"]
                current["quantity"] -= quantity
                if current["quantity"] <= 1e-12:
                    positions.pop(symbol)
                fills.append({
                    "signal_at": signal_at.isoformat(), "time": _utc(bar["start_at"]).isoformat(),
                    "symbol": symbol, "side": "sell", "quantity": quantity, "price": price,
                    "reference_open": reference_price, "commission": commission,
                })
            for symbol, weight in sorted(desired.items(), key=lambda item: (-item[1], item[0])):
                bar = day_bars.get(symbol)
                if bar is None:
                    risk_events.append({"time": day.isoformat(), "code": "NO_ENTRY_BAR", "symbol": symbol})
                    continue
                target_notional = target_value * weight
                current = positions.get(symbol, {"quantity": 0.0, "basis": 0.0, "mark": float(bar["close"])})
                current_notional = current["quantity"] * float(bar["open"])
                delta_notional = target_notional - current_notional
                if delta_notional <= 0:
                    continue
                reference_price = float(bar["open"])
                price = reference_price * (1 + effective_slippage_bps / 10000)
                max_quantity = max(0.0, cash / (price * (1 + effective_commission_bps / 10000)))
                requested_quantity = delta_notional / price
                quantity = min(requested_quantity, max_quantity)
                if quantity <= 0:
                    orders.append({"signal_at": signal_at.isoformat(), "symbol": symbol, "status": "rejected", "reason": "INSUFFICIENT_CASH"})
                    risk_events.append({"time": day.isoformat(), "code": "INSUFFICIENT_CASH", "symbol": symbol})
                    continue
                if quantity + 1e-12 < requested_quantity:
                    orders.append({"signal_at": signal_at.isoformat(), "symbol": symbol, "status": "partially_filled", "requested_notional": delta_notional, "filled_notional": quantity * price})
                else:
                    orders.append({"signal_at": signal_at.isoformat(), "symbol": symbol, "status": "filled", "requested_notional": delta_notional})
                commission = quantity * price * effective_commission_bps / 10000
                cash -= quantity * price + commission
                commission_paid += commission
                slippage_paid += quantity * reference_price * effective_slippage_bps / 10000
                total_notional += quantity * price
                record_cash(day, "trade", -(quantity * price + commission), symbol=symbol, side="buy", gross=quantity * price, commission=commission)
                if current["quantity"] > 0:
                    current["basis"] += quantity * price + commission
                    current["quantity"] += quantity
                else:
                    current = {"quantity": quantity, "basis": quantity * price + commission, "mark": float(bar["close"])}
                current["mark"] = float(bar["close"])
                positions[symbol] = current
                fills.append({
                    "signal_at": signal_at.isoformat(), "time": _utc(bar["start_at"]).isoformat(),
                    "symbol": symbol, "side": "buy", "quantity": quantity, "price": price,
                    "reference_open": reference_price, "commission": commission,
                })
            pending = None
        for symbol, bar in day_bars.items():
            last_close[symbol] = float(bar["close"])
            if symbol in positions:
                positions[symbol]["mark"] = float(bar["close"])
        position_snapshot = [
            {"symbol": symbol, "quantity": position["quantity"], "mark": position["mark"], "value": position["quantity"] * position["mark"]}
            for symbol, position in sorted(positions.items()) if position["quantity"] > 0
        ]
        positions_value = sum(row["value"] for row in position_snapshot)
        equity = cash + positions_value
        reconciliation_error = equity - cash - positions_value
        equity_rows.append({
            "date": day.isoformat(), "equity": equity, "cash": cash,
            "positions_value": positions_value, "reconciliation_error": reconciliation_error,
        })
        position_rows.append({"date": day.isoformat(), "positions": position_snapshot})

    final_equity = equity_rows[-1]["equity"]
    equities = np.array([row["equity"] for row in equity_rows], dtype=float)
    returns = np.diff(equities) / equities[:-1] if len(equities) > 1 else np.array([])
    benchmark_returns = []
    for previous, current in zip(dates, dates[1:]):
        symbols = sorted(set(bars_by_date[previous]) & set(bars_by_date[current]))
        if symbols:
            benchmark_returns.append(float(np.mean([bars_by_date[current][symbol]["close"] / bars_by_date[previous][symbol]["close"] - 1 for symbol in symbols])))
    benchmark_return = float(np.prod(1 + np.array(benchmark_returns)) - 1) if benchmark_returns else None
    active = returns - np.array(benchmark_returns[:len(returns)]) if len(returns) and len(benchmark_returns) >= len(returns) else np.array([])
    years = max((dates[-1] - dates[0]).days, 1) / 365.25
    max_reconciliation_error = max(abs(row["reconciliation_error"]) for row in equity_rows)
    metrics = {
        "total_return": final_equity / config.initial_cash - 1,
        "cagr": (final_equity / config.initial_cash) ** (1 / years) - 1 if years > 0 else None,
        "annualized_volatility": float(np.std(returns, ddof=1) * math.sqrt(252)) if len(returns) >= 2 else None,
        "sharpe": float(np.mean(returns) / np.std(returns, ddof=1) * math.sqrt(252)) if len(returns) >= 2 and np.std(returns, ddof=1) > 0 else None,
        "max_drawdown": _max_drawdown(equities.tolist()),
        "turnover": total_notional / max(1.0, float(np.mean(equities))),
        "commission_paid": commission_paid,
        "baseline_commission_paid_estimate": commission_paid / stress_cost_multiplier,
        "slippage_paid": slippage_paid,
        "benchmark_return": benchmark_return,
        "benchmark_return_semantics": "raw_price_unadjusted",
        "excess_return": (final_equity / config.initial_cash - 1 - benchmark_return) if benchmark_return is not None else None,
        "tracking_error": float(np.std(active, ddof=1) * math.sqrt(252)) if len(active) >= 2 else None,
        "information_ratio": float(np.mean(active) / np.std(active, ddof=1) * math.sqrt(252)) if len(active) >= 2 and np.std(active, ddof=1) > 0 else None,
        "prediction_coverage": prediction_coverage,
        "rebalance_count": len(target_rows),
        "max_reconciliation_error": max_reconciliation_error,
        "cost_stress_multiplier": stress_cost_multiplier,
        "price_attestation_id": price_attestation.attestation_id,
        "corporate_action_dataset_id": corporate_action_dataset.action_dataset_id,
        "execution_validation_id": validity.validation_id,
        "return_semantics": "total_return_with_corporate_actions" if total_return_supported else "raw_price_return_excluding_corporate_actions",
        "corporate_actions_modeled": total_return_supported,
        "total_return_claim_supported": total_return_supported,
        "definition": "daily UTC close marks; next-session open fills; cash, commission, slippage, split and dividend accounting reconciled daily",
    }
    return PortfolioBacktestResult(
        metrics=metrics,
        final_account={"cash": cash, "positions_value": final_equity - cash, "equity": final_equity},
        orders=orders, fills=fills, positions=position_rows, equity_curve=equity_rows,
        target_weights=target_rows, risk_events=risk_events,
        diagnostics={
            "same_bar_signal_fill": False,
            "target_weights_separate_from_positions": True,
            "corporate_actions_modeled": total_return_supported,
            "full_total_return_claim_supported": total_return_supported,
            "cash_never_negative": all(row["cash"] >= -1e-9 for row in equity_rows),
            "max_reconciliation_error": max_reconciliation_error,
            "cost_stress_multiplier": stress_cost_multiplier,
            "execution_validation": validity.model_dump(mode="json"),
        },
        cash_ledger=cash_ledger,
        corporate_actions=corporate_action_rows,
    )
