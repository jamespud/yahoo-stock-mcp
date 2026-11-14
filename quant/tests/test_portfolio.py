from datetime import timedelta
import pytest
from stock_quant.compiler import compile_strategy
from stock_quant.data import Snapshot
from stock_quant.portfolio import run_portfolio
from stock_quant.runtime import ExecutionConfig
from test_runtime import fixture, plan
from test_data import T


def test_shared_cash_and_deterministic_symbol_order():
    bars = list(fixture().bars)
    bars += [b.model_copy(update={"symbol": "MSFT"}) for b in fixture().bars]
    snapshot = Snapshot(bars, calendar_version="v1")
    result = run_portfolio({"MSFT": plan(), "AAPL": plan()}, snapshot, config=ExecutionConfig(initial_cash=15.))
    assert [(f["symbol"], f["quantity"]) for f in result["fills"]] == [("AAPL", 1.)]
    assert any(o.get("reason") == "INSUFFICIENT_CASH" for o in result["orders"])
    assert result == run_portfolio({"AAPL": plan(), "MSFT": plan()}, snapshot, config=ExecutionConfig(initial_cash=15.))
    for row in result["equity_curve"]:
        assert row["equity"] == pytest.approx(row["cash"] + row["receivables"] + sum(p["quantity"] * p["mark"] for p in row["positions"].values()))


def test_split_preserves_value_and_adjusts_pending_exit_quantity():
    bars = [b.model_copy(update={"open": 100., "high": 100., "low": 100., "close": 100.}) for b in fixture().bars]
    for i in (2, 3):
        bars[i] = bars[i].model_copy(update={"open": 50., "high": 50., "low": 50., "close": 50.})
    action = {"kind": "split", "id": "split1", "symbol": "AAPL", "ratio": 2., "effective_at": bars[2].start_time, "available_at": bars[0].start_time}
    p = compile_strategy({"schema_version": "1.0", "name": "toggle", "decision_timeframe": "1d", "entry": {"kind": "const", "value": True}, "exit": {"kind": "const", "value": True}})
    result = run_portfolio({"AAPL": p}, Snapshot(bars, calendar_version="v1", actions=[action]), config=ExecutionConfig(initial_cash=1000., quantity=2.))
    assert result["fills"][1]["side"] == "sell"
    assert result["fills"][1]["quantity"] == 4
    assert result["fills"][1]["price"] == 50
    assert all(row["equity"] == pytest.approx(1000.) for row in result["equity_curve"])


def test_dividend_entitlement_remains_after_sale_and_paid_once():
    bars = [b.model_copy(update={"open": 100., "high": 100., "low": 100., "close": 100.}) for b in fixture().bars]
    for i in (2, 3):
        bars[i] = bars[i].model_copy(update={"open": 99., "high": 99., "low": 99., "close": 99.})
    action = {"kind": "cash_dividend", "id": "div1", "symbol": "AAPL", "amount": 1., "effective_at": bars[2].start_time, "available_at": bars[0].start_time, "pay_at": bars[3].start_time + timedelta(hours=1)}
    p = compile_strategy({"schema_version": "1.0", "name": "sell", "decision_timeframe": "1d", "entry": {"kind": "const", "value": True}, "exit": {"kind": "const", "value": True}})
    result = run_portfolio({"AAPL": p}, Snapshot(bars, calendar_version="v1", actions=[action]), config=ExecutionConfig(initial_cash=1000., quantity=2.))
    assert [e["kind"] for e in result["cash_ledger"]].count("dividend_payment") == 1
    assert result["final_account"]["receivables"] == 0
    assert result["final_account"]["equity"] == pytest.approx(1000.)


def test_partial_fills_use_previously_completed_volume():
    bars = [b.model_copy(update={"volume": 10.}) for b in fixture().bars]
    result = run_portfolio({"AAPL": plan()}, Snapshot(bars, calendar_version="v1"), config=ExecutionConfig(quantity=7., volume_participation=.2))
    assert [f["quantity"] for f in result["fills"]] == [2., 2., 2.]
    assert result["orders"][0]["status"] == "expired"
    assert result["orders"][0]["remaining"] == 1


def test_small_fractional_partial_order_keeps_its_unfilled_remainder():
    bars = [b.model_copy(update={"volume": 1e-12}) for b in fixture().bars]
    result = run_portfolio({"AAPL": plan()}, Snapshot(bars, calendar_version="small-partial"), config=ExecutionConfig(quantity=1e-12, volume_participation=.5))
    assert [f["quantity"] for f in result["fills"]] == [5e-13, 5e-13]
    assert result["orders"][0]["remaining"] == 0.


def test_risk_limits_and_currency_rejection():
    result = run_portfolio({"AAPL": plan()}, fixture(), config=ExecutionConfig(max_order_notional=5.))
    assert result["fills"] == []
    assert result["orders"][0]["reason"] == "MAX_ORDER_NOTIONAL"
    with pytest.raises(ValueError, match="currency"):
        run_portfolio({"AAPL": plan()}, fixture(), config=ExecutionConfig(currency="EUR"))


def test_action_changes_snapshot_identity_and_duplicate_id_rejected():
    s = fixture()
    action = {"kind": "split", "id": "s1", "symbol": "AAPL", "ratio": 2., "effective_at": T + timedelta(days=2), "available_at": T}
    assert s.id != Snapshot(list(s.bars), calendar_version=s.calendar_version, actions=[action]).id
    with pytest.raises(ValueError, match="Duplicate"):
        Snapshot(list(s.bars), calendar_version="v1", actions=[action, action])


def test_split_adjusted_signal_is_pit_and_separate_from_broker_prices():
    from stock_quant.runtime import Evaluator
    bars = [b.model_copy(update={"open": 100., "high": 100., "low": 100., "close": 100.}) for b in fixture().bars]
    for i in (2, 3):
        bars[i] = bars[i].model_copy(update={"open": 50., "high": 50., "low": 50., "close": 50.})
    action = {"kind": "split", "id": "s", "symbol": "AAPL", "ratio": 2., "effective_at": bars[2].start_time, "available_at": bars[0].start_time}
    ref = {"kind": "ref", "symbol": "AAPL", "field": "close", "timeframe": "1d", "adjustment": "split_asof"}
    raw = {"schema_version": "1.0", "name": "adjusted", "decision_timeframe": "1d", "entry": {"kind": "binary", "op": "gte", "left": ref, "right": {"kind": "call", "operator": "sma", "args": [ref], "params": {"period": 2}}}, "exit": {"kind": "const", "value": False}}
    p = compile_strategy(raw)
    s = Snapshot(bars, calendar_version="v1", actions=[action])
    evaluator = Evaluator(p, s)
    sma = next(n for n in p.nodes if n.kind == "call")
    assert evaluator.value(sma.id, bars[1].available_at) == 100  # Future announced split not applied yet.
    assert evaluator.value(sma.id, bars[2].available_at) == 50
    result = run_portfolio({"AAPL": p}, s, config=ExecutionConfig(quantity=2.))
    assert result["fills"][0]["price"] == 50  # Broker always consumes actual raw open.


def test_cash_delisting_settles_and_prevents_reentry():
    s = fixture()
    action = {"kind": "cash_delisting", "id": "d", "symbol": "AAPL", "amount": 15., "effective_at": s.bars[2].start_time, "available_at": s.bars[0].start_time}
    result = run_portfolio({"AAPL": plan()}, Snapshot(list(s.bars), calendar_version="v1", actions=[action]))
    assert len(result["fills"]) == 1
    assert result["final_account"]["positions"]["AAPL"]["quantity"] == 0
    assert result["final_account"]["equity"] == pytest.approx(10003.)


def test_dividend_before_open_preserves_equity_until_price_observation():
    s = fixture()
    bars = [b.model_copy(update={"open": 100., "high": 100., "low": 100., "close": 100.}) for b in s.bars]
    for i in (2, 3):
        bars[i] = bars[i].model_copy(update={"open": 99., "high": 99., "low": 99., "close": 99.})
    action = {"kind": "cash_dividend", "id": "div", "symbol": "AAPL", "amount": 1., "effective_at": bars[2].start_time - timedelta(hours=1), "available_at": bars[0].start_time, "pay_at": bars[3].end_time}
    result = run_portfolio({"AAPL": plan()}, Snapshot(bars, calendar_version="v1", actions=[action]))
    assert all(row["equity"] == pytest.approx(10000.) for row in result["equity_curve"])
    assert result["summary"]["max_drawdown"] == 0


def test_delayed_pre_split_close_cannot_rewind_current_broker_mark():
    bars = [b.model_copy(update={"open": 100., "high": 100., "low": 100., "close": 100.}) for b in fixture().bars]
    bars[1] = bars[1].model_copy(update={"available_at": bars[2].start_time + timedelta(hours=1)})
    for i in (2, 3):
        bars[i] = bars[i].model_copy(update={"open": 50., "high": 50., "low": 50., "close": 50.})
    action = {"kind": "split", "id": "s", "symbol": "AAPL", "ratio": 2., "effective_at": bars[2].start_time, "available_at": bars[0].start_time}
    result = run_portfolio({"AAPL": plan()}, Snapshot(bars, calendar_version="v1", actions=[action]))
    assert all(row["equity"] == pytest.approx(10000.) for row in result["equity_curve"])


def test_position_weight_includes_slippage_and_fees_before_acceptance():
    s = fixture()
    rows = [b.model_copy(update={"open": 100., "high": 100., "low": 100., "close": 100.}) for b in s.bars]
    result = run_portfolio({"AAPL": plan()}, Snapshot(rows, calendar_version="v1"), config=ExecutionConfig(initial_cash=1000., quantity=5., slippage_bps=1000., max_position_weight=.5))
    assert not result["fills"]
    assert all(o["reason"] == "MAX_POSITION_WEIGHT" for o in result["orders"])


def test_cash_and_realized_basis_ledgers_reconcile_after_round_trips():
    p = compile_strategy({"schema_version": "1.0", "name": "roundtrip", "decision_timeframe": "1d", "entry": {"kind": "const", "value": True}, "exit": {"kind": "const", "value": True}})
    result = run_portfolio({"AAPL": p}, fixture(), config=ExecutionConfig(initial_cash=1000., quantity=2., commission_bps=25., slippage_bps=10.))
    final = result["final_account"]
    assert final["cash"] == pytest.approx(1000 + sum(e["amount"] for e in result["cash_ledger"]))
    position = final["positions"]["AAPL"]
    unrealized = position["quantity"] * position["mark"] - position["basis"]
    assert final["equity"] - 1000 == pytest.approx(position["realized_pnl"] + unrealized)


def test_all_simultaneous_close_marks_precede_any_order_risk_check():
    rows = []
    base = fixture().bars
    for symbol, prices in (("AAA", [(100., 100.), (100., 130.), (130., 130.)]), ("ZZZ", [(100., 100.), (100., 200.), (200., 200.)])):
        for bar, (open_, close) in zip(base, prices):
            rows.append(bar.model_copy(update={"symbol": symbol, "open": open_, "high": max(open_, close), "low": min(open_, close), "close": close}))
    raw = {"schema_version": "1.0", "name": "later_entry", "decision_timeframe": "1d", "entry": {"kind": "binary", "op": "gt", "left": {"kind": "ref", "symbol": "AAA", "field": "close", "timeframe": "1d"}, "right": {"kind": "const", "value": 110}}, "exit": {"kind": "const", "value": False}}
    result = run_portfolio({"AAA": compile_strategy(raw), "ZZZ": plan()}, Snapshot(rows, calendar_version="v1"), config=ExecutionConfig(initial_cash=250., max_position_weight=.5))
    aaa = [o for o in result["orders"] if o["symbol"] == "AAA"]
    assert aaa[0]["status"] == "filled"
    assert aaa[0]["submitted_at"] == base[1].available_at.isoformat()
    assert [f["symbol"] for f in result["fills"]] == ["ZZZ", "AAA"]


def test_preperiod_delisting_disables_trading_without_inventing_cash_settlement():
    s = fixture()
    action = {"kind": "cash_delisting", "id": "d", "symbol": "AAPL", "amount": 15., "effective_at": s.bars[1].start_time, "available_at": s.bars[0].start_time}
    result = run_portfolio({"AAPL": plan()}, Snapshot(list(s.bars), calendar_version="v1", actions=[action]), start=s.bars[2].start_time)
    assert result["orders"] == []
    assert result["fills"] == []
    assert result["cash_ledger"] == []
    assert result["final_account"]["equity"] == 10000
    assert result["corporate_actions"][0]["kind"] == "historical_delisting_state"
