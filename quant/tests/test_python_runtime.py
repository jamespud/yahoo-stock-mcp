"""Python lifecycle -> same broker/risk/account as DSL, in actual containers."""
from datetime import timedelta
import os

import pytest

from stock_quant.data import Snapshot
from stock_quant.runtime import ExecutionConfig
from test_data import T
from test_runtime import fixture


@pytest.fixture
def image():
    value = os.environ.get("QUANT_SANDBOX_IMAGE")
    if not value:
        pytest.skip("actual Python strategy execution requires QUANT_SANDBOX_IMAGE")
    return value


def test_python_fill_lifecycle_uses_shared_broker_costs_and_account(image):
    from stock_quant.strategy_runtime import run_python_backtest
    from stock_quant.sandbox import PythonArtifact
    source = '''
class Strategy:
    def initialize(self, ctx):
        self.fills = 0
    def on_bar(self, ctx, bar):
        history = ctx.history(bar.symbol, "1d", 100)
        assert history[-1].available_at == bar.available_at
        if self.fills == 0 and ctx.position(bar.symbol) == 0:
            return [{"symbol": bar.symbol, "side": "buy", "quantity": 2}]
        if self.fills == 1:
            assert ctx.position(bar.symbol) == 2
            return [{"symbol": bar.symbol, "side": "sell", "quantity": 2}]
        return []
    def on_fill(self, ctx, fill):
        self.fills += 1
'''
    result = run_python_backtest(PythonArtifact(name="fill-state", source=source), fixture(), symbols=["AAPL"], timeframe="1d", image=image, config=ExecutionConfig(initial_cash=1000., commission_bps=10.))
    assert [f["side"] for f in result["fills"]] == ["buy", "sell"]
    assert [f["price"] for f in result["fills"]] == [12., 14.]
    assert result["final_account"]["cash"] == pytest.approx(1003.948)
    assert result["final_account"]["positions"]["AAPL"]["quantity"] == 0
    assert result["final_account"]["positions"]["AAPL"]["realized_pnl"] == pytest.approx(3.948)
    assert result["reproducibility"]["strategy_mode"] == "python"
    assert result["reproducibility"]["sandbox_image"] == image


def test_python_universe_shares_cash_and_cannot_oversell(image):
    from stock_quant.strategy_runtime import run_python_backtest
    from stock_quant.sandbox import PythonArtifact
    data = fixture()
    data = Snapshot([*data.bars, *[b.model_copy(update={"symbol": "MSFT"}) for b in data.bars]], calendar_version="python-portfolio")
    source = '''
class Strategy:
    def on_bar(self, ctx, bar):
        if ctx.position(bar.symbol) == 0:
            return [{"symbol": bar.symbol, "side": "sell", "quantity": 1e-12}, {"symbol": bar.symbol, "side": "sell", "quantity": 100}, {"symbol": bar.symbol, "side": "buy", "quantity": 1}]
        return []
'''
    result = run_python_backtest(PythonArtifact(name="portfolio", source=source), data, symbols=["MSFT", "AAPL"], timeframe="1d", image=image, config=ExecutionConfig(initial_cash=15.))
    assert [(f["symbol"], f["quantity"]) for f in result["fills"]] == [("AAPL", 1.)]
    assert any(o.get("reason") == "INSUFFICIENT_POSITION" for o in result["orders"])
    assert any(o.get("reason") == "INSUFFICIENT_CASH" for o in result["orders"])
    assert result["final_account"]["equity"] == 20.


def test_python_bar_callbacks_continue_while_partial_order_is_pending(image):
    from stock_quant.strategy_runtime import run_python_backtest
    from stock_quant.sandbox import PythonArtifact
    source = '''
class Strategy:
    def initialize(self, ctx): self.bars = 0
    def on_bar(self, ctx, bar):
        self.bars += 1
        if self.bars == 1:
            return [{"symbol": bar.symbol, "side": "buy", "quantity": 7}]
        if self.bars == 4:
            assert ctx.position(bar.symbol) == 6
        return []
'''
    data = Snapshot([b.model_copy(update={"volume": 10.}) for b in fixture().bars], calendar_version="partial")
    result = run_python_backtest(PythonArtifact(name="pending", source=source), data, symbols=["AAPL"], timeframe="1d", image=image, config=ExecutionConfig(volume_participation=.2))
    assert [f["quantity"] for f in result["fills"]] == [2., 2., 2.]
    assert result["orders"][0]["remaining"] == 1.


def test_broker_bounds_total_intents_before_untrusted_strategy_exhausts_host_memory():
    from stock_quant.portfolio import run_portfolio
    from test_runtime import plan
    data = fixture()
    rows = [data.bars[0].model_copy(update={"start_time": T + timedelta(days=i), "end_time": T + timedelta(days=i, hours=6), "available_at": T + timedelta(days=i, hours=6)}) for i in range(205)]
    class Flood:
        def initialize(self, ctx): pass
        def on_fill(self, ctx, fill): pass
        def on_bar(self, ctx, bar):
            return [{"symbol": "AAPL", "side": "sell", "quantity": 1.}] * 50
    with pytest.raises(ValueError, match="RESOURCE_LIMIT_EXCEEDED.*order"):
        run_portfolio({"AAPL": plan()}, Snapshot(rows, calendar_version="intent-flood"), strategy=Flood())


def test_sell_order_notional_is_checked_again_at_the_execution_open(image):
    from stock_quant.strategy_runtime import run_python_backtest
    from stock_quant.sandbox import PythonArtifact
    source = '''
class Strategy:
    def on_bar(self, ctx, bar):
        return [{"symbol": bar.symbol, "side": "sell" if ctx.position(bar.symbol) else "buy", "quantity": 1}]
'''
    rows = list(fixture().bars)
    rows[2] = rows[2].model_copy(update={"open": 100., "high": 100.})
    result = run_python_backtest(PythonArtifact(name="sell-cap", source=source), Snapshot(rows, calendar_version="sell-cap"), symbols=["AAPL"], timeframe="1d", image=image, config=ExecutionConfig(max_order_notional=15.))
    assert [fill["side"] for fill in result["fills"]] == ["buy"]
    assert any(event["code"] == "MAX_ORDER_NOTIONAL" and event["stage"] == "pre_fill" for event in result["risk_events"])
