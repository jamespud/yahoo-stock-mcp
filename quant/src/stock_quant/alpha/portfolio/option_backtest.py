"""Long-only option research with next executable ask and bid."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone

from pydantic import Field

from ...contracts import StrictModel
from ..contracts import DatasetManifest
from ..data.catalog import DatasetStore
from ..data.option_dataset import validate_historical_quotes
from ..errors import AlphaError, AlphaErrorCode
from ..options.contracts import OptionContract, option_contract_id
from ..options.execution import next_ask, next_exit_bid
from ..options.lifecycle import require_close_before_expiry
from ..options.quotes import OptionQuote
from .ranking import rank_all_predictions


class OptionExecutionConfig(StrictModel):
    top_k: int = Field(default=5, ge=1, le=100, strict=True)
    horizon_days: int = Field(default=5, ge=1, le=252, strict=True)
    commission_per_contract: float = Field(default=0.65, ge=0, strict=True)
    close_before_expiry_days: int = Field(default=1, ge=0, le=30, strict=True)
    contracts_per_trade: int = Field(default=1, ge=1, le=1000, strict=True)


class OptionBacktestResult(StrictModel):
    metrics: dict
    trades: list[dict]
    fills: list[dict]
    positions: list[dict]
    risk_events: list[dict]
    diagnostics: dict


def _utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _contract(row) -> OptionContract:
    fields = {"underlying_id": row["underlying_id"], "option_type": row["option_type"], "strike": row["strike"],
              "expiration": row["expiration"], "exercise_style": row["exercise_style"],
              "settlement_type": row["settlement_type"], "multiplier": row["multiplier"],
              "currency": row["currency"], "deliverable": row["deliverable"],
              "adjustment_version": row["adjustment_version"]}
    return OptionContract(contract_id=option_contract_id(**fields), **fields)


def _quote(row) -> OptionQuote:
    return OptionQuote(
        contract=_contract(row), observed_at=_utc(row["observed_at"]),
        quote_at=_utc(row["quote_at"]) if row["quote_at"] else None, available_at=_utc(row["available_at"]),
        bid=row["bid"], ask=row["ask"], bid_size=row["bid_size"], ask_size=row["ask_size"],
        last_price=row["last_price"], last_trade_at=_utc(row["last_trade_at"]) if row["last_trade_at"] else None,
        volume=row["volume"], open_interest=row["open_interest"], implied_volatility=row["implied_volatility"],
        source=row["source"], source_version=row["source_version"], quality_flags=row["quality_flags"],
    )


def run_option_backtest(
    prediction_set: DatasetManifest,
    option_dataset: DatasetManifest,
    *,
    store: DatasetStore,
    config: OptionExecutionConfig | None = None,
) -> OptionBacktestResult:
    config = config or OptionExecutionConfig()
    validate_historical_quotes(option_dataset)
    predictions = store.read_rows(prediction_set.dataset_id)
    quote_rows = store.read_rows(option_dataset.dataset_id)
    quotes_by_contract = defaultdict(list)
    for row in quote_rows:
        quote = _quote(row)
        quotes_by_contract[quote.contract.contract_id].append(quote)
    ranked_all = rank_all_predictions(predictions, config.top_k)
    trades, fills, risk_events, positions = [], [], [], []
    traded = set()
    commission_paid = 0.0
    for decision, ranked in sorted(ranked_all.items()):
        for prediction in ranked:
            contract_id = prediction["instrument_id"]
            if contract_id in traded:
                continue
            quotes = quotes_by_contract.get(contract_id, [])
            if not quotes:
                risk_events.append({"decision_at": decision.isoformat(), "code": "OPTION_HISTORY_UNAVAILABLE", "contract_id": contract_id})
                continue
            entry = next_ask(quotes, decision)
            if entry is None:
                risk_events.append({"decision_at": decision.isoformat(), "code": "INVALID_OPTION_QUOTE", "contract_id": contract_id})
                continue
            entry_at = max(entry.quote_at or entry.observed_at, entry.available_at)
            expiration_at = datetime.combine(entry.contract.expiration, time(21), tzinfo=timezone.utc)
            exit_due = entry_at + timedelta(days=config.horizon_days)
            try:
                require_close_before_expiry(exit_due, expiration_at, close_before_expiry=True)
            except AlphaError:
                trades.append({"contract_id": contract_id, "decision_at": decision.isoformat(), "entry_at": entry_at.isoformat(), "unresolved": True, "reason": "EXPIRY_UNRESOLVED"})
                risk_events.append({"decision_at": decision.isoformat(), "code": "EXPIRY_UNRESOLVED", "contract_id": contract_id})
                traded.add(contract_id)
                continue
            exit_quote = next_exit_bid(quotes, after=exit_due, expiration_at=expiration_at, close_before_expiry_days=config.close_before_expiry_days)
            multiplier = entry.contract.multiplier
            quantity = config.contracts_per_trade
            entry_commission = quantity * config.commission_per_contract
            commission_paid += entry_commission
            fills.append({"contract_id": contract_id, "decision_at": decision.isoformat(), "time": entry_at.isoformat(),
                          "side": "buy", "price": entry.ask, "bid": entry.bid, "ask": entry.ask,
                          "quantity": quantity, "multiplier": multiplier, "commission": entry_commission})
            positions.append({"contract_id": contract_id, "state": "open", "entry_at": entry_at.isoformat(), "quantity": quantity})
            traded.add(contract_id)
            if exit_quote is None:
                trades.append({"contract_id": contract_id, "decision_at": decision.isoformat(), "entry_at": entry_at.isoformat(), "unresolved": True, "reason": "NO_EXIT_QUOTE"})
                risk_events.append({"decision_at": decision.isoformat(), "code": "NO_EXIT_QUOTE", "contract_id": contract_id, "unresolved": True})
                continue
            exit_at = max(exit_quote.quote_at or exit_quote.observed_at, exit_quote.available_at)
            exit_commission = quantity * config.commission_per_contract
            commission_paid += exit_commission
            fills.append({"contract_id": contract_id, "decision_at": decision.isoformat(), "time": exit_at.isoformat(),
                          "side": "sell", "price": exit_quote.bid, "bid": exit_quote.bid, "ask": exit_quote.ask,
                          "quantity": quantity, "multiplier": multiplier, "commission": exit_commission})
            gross = (exit_quote.bid - entry.ask) * quantity * multiplier
            pnl = gross - entry_commission - exit_commission
            trades.append({"contract_id": contract_id, "decision_at": decision.isoformat(), "entry_at": entry_at.isoformat(),
                           "exit_at": exit_at.isoformat(), "entry_ask": entry.ask, "exit_bid": exit_quote.bid,
                           "quantity": quantity, "multiplier": multiplier, "pnl": pnl, "unresolved": False})
            positions[-1]["state"] = "closed"
    unresolved = sum(trade["unresolved"] for trade in trades)
    return OptionBacktestResult(
        metrics={"trade_count": len(trades), "fill_count": len(fills), "commission_paid": commission_paid,
                 "unresolved_positions": unresolved, "pnl": sum(trade.get("pnl", 0) for trade in trades),
                 "execution_rule": "BUY next valid Ask; SELL next valid Bid"},
        trades=trades, fills=fills, positions=positions, risk_events=risk_events,
        diagnostics={"mid_price_fills": False, "intrinsic_liquidation": False, "current_yahoo_snapshot_admitted": False},
    )
