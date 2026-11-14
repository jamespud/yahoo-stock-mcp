"""Hand-derived finance cases beyond the limited benchmark account oracle."""
from datetime import datetime, timedelta, timezone

import pytest

from stock_quant.compiler import compile_strategy
from stock_quant.data import MarketBar, Snapshot
from stock_quant.portfolio import run_portfolio
from stock_quant.runtime import ExecutionConfig
from stock_quant.sandbox import INTENTS
from stock_quant.strategy_runtime import DslDriver

T = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)


def bars(prices, symbol="AAPL"):
    return [MarketBar(symbol=symbol, timeframe="1d", exchange="XNAS", start_time=T + timedelta(days=i),
                      end_time=T + timedelta(days=i, hours=6), available_at=T + timedelta(days=i, hours=6),
                      open=float(p), high=float(p), low=float(p), close=float(p), volume=10.,
                      source="hand_oracle", adjustment="raw", dataset_version="phase2-hand-v1") for i, p in enumerate(prices)]


def hold():
    return compile_strategy({"schema_version": "1.0", "name": "hand-hold", "decision_timeframe": "1d", "entry": {"kind": "const", "value": True}, "exit": {"kind": "const", "value": False}})


def test_split_dividend_golden_hand_account_oracle():
    rows = bars([100, 100, 49, 49])
    actions = [{"kind": "split", "id": "split", "symbol": "AAPL", "ratio": 2., "available_at": T, "effective_at": rows[2].start_time},
               {"kind": "cash_dividend", "id": "dividend", "symbol": "AAPL", "amount": 1., "available_at": T, "effective_at": rows[2].start_time, "pay_at": rows[3].start_time}]
    result = run_portfolio({"AAPL": hold()}, Snapshot(rows, calendar_version="hand", actions=actions), config=ExecutionConfig(initial_cash=1000., quantity=2.))
    assert [(f["side"], f["quantity"], f["price"]) for f in result["fills"]] == [("buy", 2., 100.)]
    ex = next(r for r in result["equity_curve"] if r["time"] == rows[2].start_time.isoformat())
    assert ex["cash"] == 800.
    assert ex["receivables"] == 4.  # 2 pre-split shares -> 4 entitled shares.
    assert ex["positions"]["AAPL"] == {"quantity": 4., "basis": 200., "mark": 49., "realized_pnl": 0.}
    assert result["final_account"]["cash"] == 804.
    assert result["final_account"]["receivables"] == 0.
    assert result["final_account"]["equity"] == 1000.  # 804 + 4 * 49.
    assert result["attribution"]["unrealized_pnl"] == -4.
    assert result["attribution"]["dividend_income"] == 4.
    assert result["summary"]["max_drawdown"] == 0.
    assert [e["amount"] for e in result["cash_ledger"] if e["kind"] == "dividend_payment"] == [4.]


def test_partial_fills_shared_cash_and_expiry_hand_oracle():
    data = Snapshot(bars([10] * 4) + bars([20] * 4, "MSFT"), calendar_version="hand")
    result = run_portfolio({"MSFT": hold(), "AAPL": hold()}, data,
                           config=ExecutionConfig(initial_cash=150., quantity=7., volume_participation=.2))
    assert [(f["symbol"], f["quantity"], f["price"]) for f in result["fills"]] == [("AAPL", 2., 10.), ("MSFT", 2., 20.), ("AAPL", 2., 10.), ("MSFT", 2., 20.), ("AAPL", 2., 10.)]
    final = result["final_account"]
    assert final["cash"] == 10.  # 150 - 6*10 - 4*20.
    assert final["equity"] == 150.
    assert final["positions"]["AAPL"]["quantity"] == 6.
    assert final["positions"]["MSFT"]["quantity"] == 4.
    a, m = result["orders"][:2]
    assert (a["status"], a["remaining"], a["filled_quantity"]) == ("expired", 1., 6.)
    assert (m["status"], m["remaining"], m["reason"]) == ("rejected", 3., "INSUFFICIENT_CASH")
    assert [r["cash"] for r in result["equity_curve"] if r["time"] in [(T + timedelta(days=i)).isoformat() for i in (1, 2, 3)]] == [90., 30., 10.]


def test_partial_fill_cancellation_stops_before_second_fill():
    data, plan = Snapshot(bars([10] * 4), calendar_version="hand"), hold()
    config = ExecutionConfig(initial_cash=150., quantity=7., volume_participation=.2)
    fills = []
    class ObservingDriver(DslDriver):
        def on_fill(self, view, fill):
            fills.append(fill)
    driver = ObservingDriver(plan, data, "AAPL", config, {"evaluated_decisions": 0, "undefined_entry_decisions": 0, "undefined_exit_decisions": 0})
    def cancel_after_first(progress):
        if fills:
            raise InterruptedError("cancel after observed partial fill")
    with pytest.raises(InterruptedError, match="cancel after observed"):
        run_portfolio({"AAPL": plan}, data, config=config, strategy=driver, checkpoint=cancel_after_first)
    assert [(f["quantity"], f["price"]) for f in fills] == [(2., 10.)]


@pytest.mark.parametrize("intent", [{"symbol": "AAPL", "side": "short", "quantity": 1.}, {"symbol": "AAPL", "side": "buy", "quantity": -1.}, {"symbol": "AAPL", "side": "buy", "quantity": 1., "order_type": "limit", "limit_price": 9.}])
def test_unsupported_orders_short_and_limit_rejected(intent):
    with pytest.raises(ValueError):
        INTENTS.validate_python([intent])


def test_short_inventory_and_multicurrency_rejection():
    class SellWithoutInventory:
        def initialize(self, view): pass
        def on_fill(self, view, fill): raise AssertionError("naked sale filled")
        def on_bar(self, view, bar): return [{"symbol": "AAPL", "side": "sell", "quantity": 1.}]
    data = Snapshot(bars([10] * 3), calendar_version="hand")
    result = run_portfolio({"AAPL": hold()}, data, strategy=SellWithoutInventory())
    assert result["fills"] == []
    assert all(o["reason"] == "INSUFFICIENT_POSITION" for o in result["orders"])
    assert result["final_account"]["cash"] == 10000.
    assert result["final_account"]["positions"]["AAPL"]["quantity"] == 0.
    with pytest.raises(ValueError, match="currency"):
        run_portfolio({"AAPL": hold()}, data, config=ExecutionConfig(currency="EUR"))
