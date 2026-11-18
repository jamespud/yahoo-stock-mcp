"""Long-only option research with time-ordered cash, entry and exit events."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
import heapq

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
    initial_cash: float = Field(default=100_000.0, gt=0, strict=True)
    max_premium_fraction: float = Field(default=1.0, gt=0, le=1, strict=True)


class OptionBacktestResult(StrictModel):
    metrics: dict
    trades: list[dict]
    fills: list[dict]
    positions: list[dict]
    cash_ledger: list[dict]
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


def _execution_time(quote: OptionQuote) -> datetime:
    return max(quote.quote_at or quote.observed_at, quote.available_at)


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
    for quotes in quotes_by_contract.values():
        quotes.sort(key=_execution_time)
    ranked_all = rank_all_predictions(predictions, config.top_k)

    trades, fills, risk_events, positions, cash_ledger = [], [], [], [], []
    attempted: set[str] = set()
    commission_paid = 0.0
    cash = config.initial_cash
    sequence = 0
    queue: list[tuple[datetime, int, int, str, dict]] = []

    def push(at: datetime, phase: int, kind: str, payload: dict) -> None:
        nonlocal sequence
        heapq.heappush(queue, (at, phase, sequence, kind, payload))
        sequence += 1

    # phase: exit=0, decision=1, entry=2; timestamps include quote availability.
    for decision, ranked in sorted(ranked_all.items()):
        push(decision, 1, "decision", {"decision": decision, "ranked": ranked})

    position_by_contract: dict[str, dict] = {}
    trade_by_contract: dict[str, dict] = {}

    while queue:
        at, _, _, kind, payload = heapq.heappop(queue)
        if kind == "decision":
            decision = payload["decision"]
            for prediction in payload["ranked"]:
                contract_id = prediction["instrument_id"]
                if contract_id in attempted:
                    continue
                attempted.add(contract_id)
                quotes = quotes_by_contract.get(contract_id, [])
                if not quotes:
                    risk_events.append({"decision_at": decision.isoformat(), "code": "OPTION_HISTORY_UNAVAILABLE", "contract_id": contract_id})
                    continue
                entry = next_ask(quotes, decision)
                if entry is None:
                    risk_events.append({"decision_at": decision.isoformat(), "code": "INVALID_OPTION_QUOTE", "contract_id": contract_id})
                    continue
                push(_execution_time(entry), 2, "entry", {"decision": decision, "quote": entry, "contract_id": contract_id})
            continue

        if kind == "entry":
            decision = payload["decision"]
            entry = payload["quote"]
            contract_id = payload["contract_id"]
            execution_at = _execution_time(entry)
            multiplier = entry.contract.multiplier
            per_contract = entry.ask * multiplier + config.commission_per_contract
            budget = min(cash, config.initial_cash * config.max_premium_fraction)
            affordable = int(max(0.0, budget) // per_contract) if per_contract > 0 else 0
            quantity = min(config.contracts_per_trade, entry.ask_size or 0, affordable)
            if quantity <= 0:
                risk_events.append({"decision_at": decision.isoformat(), "time": execution_at.isoformat(),
                                    "code": "INSUFFICIENT_CASH_OR_DEPTH", "contract_id": contract_id,
                                    "required_per_contract": per_contract, "cash": cash, "ask_size": entry.ask_size})
                continue
            requested = config.contracts_per_trade
            capital = entry.ask * multiplier * quantity + quantity * config.commission_per_contract
            cash -= capital
            commission_paid += quantity * config.commission_per_contract
            cash_ledger.append({"time": execution_at.isoformat(), "contract_id": contract_id,
                                "kind": "premium_and_commission", "amount": -capital, "cash_after": cash})
            fills.append({"contract_id": contract_id, "decision_at": decision.isoformat(), "time": execution_at.isoformat(),
                          "side": "buy", "price": entry.ask, "bid": entry.bid, "ask": entry.ask,
                          "quantity": quantity, "multiplier": multiplier,
                          "commission": quantity * config.commission_per_contract, "cash_after": cash})
            position = {"contract_id": contract_id, "state": "open", "entry_at": execution_at.isoformat(),
                        "quantity": quantity, "remaining": quantity, "cost_basis": capital,
                        "entry_ask": entry.ask, "multiplier": multiplier}
            positions.append(position)
            position_by_contract[contract_id] = position
            trade = {"contract_id": contract_id, "decision_at": decision.isoformat(), "entry_at": execution_at.isoformat(),
                     "entry_ask": entry.ask, "entry_quantity": quantity, "remaining_quantity": quantity,
                     "exit_quantity": 0, "exit_bid": None, "exit_at": None, "multiplier": multiplier,
                     "entry_commission": quantity * config.commission_per_contract,
                     "exit_commission": 0.0, "pnl": -quantity * config.commission_per_contract,
                     "unresolved": True, "reason": "NO_EXIT_QUOTE"}
            trades.append(trade)
            trade_by_contract[contract_id] = trade
            if quantity < requested:
                risk_events.append({"decision_at": decision.isoformat(), "time": execution_at.isoformat(),
                                    "code": "PARTIAL_FILL", "contract_id": contract_id,
                                    "requested_quantity": requested, "filled_quantity": quantity})
            expiration_at = datetime.combine(entry.contract.expiration, time(21), tzinfo=timezone.utc)
            exit_due = execution_at + timedelta(days=config.horizon_days)
            try:
                require_close_before_expiry(exit_due, expiration_at, close_before_expiry=True)
            except AlphaError:
                risk_events.append({"time": execution_at.isoformat(), "code": "EXPIRY_UNRESOLVED", "contract_id": contract_id})
                continue
            exit_quote = next_exit_bid(quotes_by_contract[contract_id], after=exit_due, expiration_at=expiration_at,
                                      close_before_expiry_days=config.close_before_expiry_days)
            if exit_quote is None:
                risk_events.append({"time": execution_at.isoformat(), "code": "NO_EXIT_QUOTE", "contract_id": contract_id, "unresolved": True})
                continue
            push(_execution_time(exit_quote), 0, "exit", {"quote": exit_quote, "contract_id": contract_id,
                                                         "expiration_at": expiration_at})
            continue

        if kind == "exit":
            contract_id = payload["contract_id"]
            exit_quote: OptionQuote = payload["quote"]
            execution_at = _execution_time(exit_quote)
            position = position_by_contract.get(contract_id)
            trade = trade_by_contract.get(contract_id)
            if position is None or trade is None or position["remaining"] <= 0:
                continue
            close_quantity = min(position["remaining"], exit_quote.bid_size or 0)
            if close_quantity <= 0:
                risk_events.append({"time": execution_at.isoformat(), "code": "INSUFFICIENT_BID_DEPTH", "contract_id": contract_id})
                continue
            remaining_before = position["remaining"]
            basis_unit = position["cost_basis"] / remaining_before
            basis_released = basis_unit * close_quantity
            exit_commission = close_quantity * config.commission_per_contract
            proceeds = exit_quote.bid * position["multiplier"] * close_quantity - exit_commission
            cash += proceeds
            commission_paid += exit_commission
            cash_ledger.append({"time": execution_at.isoformat(), "contract_id": contract_id,
                                "kind": "sale_net_of_commission", "amount": proceeds, "cash_after": cash})
            fills.append({"contract_id": contract_id, "decision_at": trade["decision_at"], "time": execution_at.isoformat(),
                          "side": "sell", "price": exit_quote.bid, "bid": exit_quote.bid, "ask": exit_quote.ask,
                          "quantity": close_quantity, "multiplier": position["multiplier"],
                          "commission": exit_commission, "cash_after": cash})
            position["remaining"] -= close_quantity
            position["cost_basis"] -= basis_released
            trade["remaining_quantity"] -= close_quantity
            trade["exit_quantity"] += close_quantity
            trade["exit_bid"] = exit_quote.bid
            trade["exit_at"] = execution_at.isoformat()
            trade["exit_commission"] += exit_commission
            trade["pnl"] = ((exit_quote.bid - trade["entry_ask"]) * position["multiplier"] * trade["exit_quantity"]
                            - trade["entry_commission"] - trade["exit_commission"])
            if position["remaining"] == 0:
                position["state"] = "closed"
                trade["unresolved"] = False
                trade["reason"] = None
            else:
                later = next_exit_bid(quotes_by_contract[contract_id], after=execution_at + timedelta(microseconds=1),
                                      expiration_at=payload["expiration_at"],
                                      close_before_expiry_days=config.close_before_expiry_days)
                if later is None:
                    trade["reason"] = "PARTIAL_EXIT_NO_LATER_QUOTE"
                    risk_events.append({"time": execution_at.isoformat(), "code": "PARTIAL_EXIT_NO_LATER_QUOTE", "contract_id": contract_id})
                else:
                    push(_execution_time(later), 0, "exit", {"quote": later, "contract_id": contract_id,
                                                             "expiration_at": payload["expiration_at"]})
            continue

        raise AssertionError(f"Unknown option event {kind}")

    unresolved_cost = sum(position["cost_basis"] for position in positions if position["remaining"] > 0)
    unresolved = sum(1 for trade in trades if trade["unresolved"])
    return OptionBacktestResult(
        metrics={"trade_count": len(trades), "fill_count": len(fills), "commission_paid": commission_paid,
                 "unresolved_positions": unresolved, "pnl": sum(trade.get("pnl", 0) for trade in trades),
                 "initial_cash": config.initial_cash, "final_cash": cash,
                 "equity": cash + unresolved_cost, "unresolved_cost_basis": unresolved_cost,
                 "execution_rule": "BUY next valid Ask; SELL next valid Bid; cash updates at actual fill time"},
        trades=trades, fills=fills, positions=positions, cash_ledger=cash_ledger, risk_events=risk_events,
        diagnostics={"mid_price_fills": False, "intrinsic_liquidation": False, "current_yahoo_snapshot_admitted": False,
                     "execution_model": "long_option_event_simulator", "full_broker_portfolio": False,
                     "corporate_actions_modeled": False, "event_order": "exit < decision < entry"},
    )
