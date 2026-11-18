from datetime import date, datetime, timedelta, timezone

import pytest

from stock_quant.alpha.contracts import OptionDatasetRequest
from stock_quant.alpha.data.catalog import DatasetStore
from stock_quant.alpha.data.option_dataset import OptionSnapshotBatch, prepare_option_dataset
from stock_quant.alpha.options.contracts import OptionContract, option_contract_id
from stock_quant.alpha.options.quotes import OptionQuote
from stock_quant.alpha.portfolio.option_backtest import OptionExecutionConfig, run_option_backtest


def build_option_contract(option_type, strike):
    fields = dict(underlying_id="AAPL", option_type=option_type, strike=strike, expiration=date(2027, 1, 15),
                  exercise_style="american", settlement_type="physical", multiplier=100,
                  currency="USD", deliverable="100 shares AAPL", adjustment_version="standard")
    return OptionContract(contract_id=option_contract_id(**fields), **fields)


def build_option_quote(contract, at, bid, ask):
    return OptionQuote(contract=contract, observed_at=at, quote_at=at, available_at=at + timedelta(seconds=1),
                       bid=bid, ask=ask, bid_size=10, ask_size=10, last_price=(bid + ask) / 2,
                       last_trade_at=None, volume=10, open_interest=100, implied_volatility=0.3,
                       source="thetadata", source_version="v3", quality_flags=[])


def _fixture(tmp_path, *, missing_exit=False):
    store = DatasetStore(tmp_path / "alpha")
    decision = datetime(2026, 1, 2, 21, tzinfo=timezone.utc)
    call = build_option_contract("CALL", 200.0)
    put = build_option_contract("PUT", 200.0)
    quotes = []
    for contract, prefix in ((call, "call"), (put, "put")):
        quotes.append(build_option_quote(contract, decision + timedelta(days=1), 5.0, 5.2))
        if not missing_exit:
            quotes.append(build_option_quote(contract, decision + timedelta(days=6), 5.8, 6.0))
    request = OptionDatasetRequest(source="thetadata", underlyings=["AAPL"], start=datetime(2026, 1, 1, tzinfo=timezone.utc),
                                   end=datetime(2026, 1, 10, tzinfo=timezone.utc), quote_policy="historical_bid_ask")
    class Provider:
        def read(self, request):
            return OptionSnapshotBatch(rows=quotes, source_version="fixture", quality={})
    option_dataset = prepare_option_dataset(request, store=store, provider=Provider())
    predictions = []
    for index, contract in enumerate((call, put)):
        for day in range(5):
            predictions.append({"decision_at": decision + timedelta(days=day), "instrument_id": contract.contract_id,
                                "prediction_value": float(10 - index), "model_id": "sha256:" + "a" * 64,
                                "model_set_id": "sha256:" + "b" * 64, "fold": "test"})
    parent = store.publish_stock(
        __import__("stock_quant.alpha.contracts", fromlist=["StockDatasetRequest"]).StockDatasetRequest(
            source="fixture", symbols=["AAPL"], start=datetime(2026, 1, 1, tzinfo=timezone.utc), end=datetime(2026, 1, 10, tzinfo=timezone.utc)
        ),
        [{"instrument_id": "AAPL", "start_at": datetime(2026, 1, 1, 14, 30, tzinfo=timezone.utc),
          "end_at": datetime(2026, 1, 1, 21, tzinfo=timezone.utc), "available_at": datetime(2026, 1, 1, 21, 1, tzinfo=timezone.utc),
          "open": 200, "high": 201, "low": 199, "close": 200, "volume": 1, "adjustment": "raw",
          "source": "fixture", "source_version": "v1", "currency": "USD"}],
        calendar_version="fixture", source_version="v1", data_quality={},
    )
    prediction_set = store.publish_prediction_panel(
        predictions, parent_feature_set_id=parent.dataset_id, model_id="sha256:" + "a" * 64,
        model_set_id="sha256:" + "b" * 64, start_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end_at=datetime(2026, 1, 10, tzinfo=timezone.utc), price_basis="price_basis_unverified",
    )
    return store, prediction_set, option_dataset


def test_option_backtest_uses_ask_entry_bid_exit_and_multiplier(tmp_path):
    store, predictions, options = _fixture(tmp_path)
    result = run_option_backtest(predictions, options, store=store, config=OptionExecutionConfig(top_k=2, horizon_days=5, commission_per_contract=0.65))
    assert len(result.trades) == 2
    assert len(result.fills) == 4
    buys = [fill for fill in result.fills if fill["side"] == "buy"]
    sells = [fill for fill in result.fills if fill["side"] == "sell"]
    assert {fill["price"] for fill in buys} == {5.2}
    assert {fill["price"] for fill in sells} == {5.8}
    assert all(fill["multiplier"] == 100 for fill in result.fills)
    assert result.metrics["commission_paid"] == pytest.approx(2 * 2 * 0.65)
    assert all(trade["unresolved"] is False for trade in result.trades)
    assert result.metrics["initial_cash"] + sum(row["amount"] for row in result.cash_ledger) == pytest.approx(result.metrics["final_cash"])
    assert result.diagnostics["full_broker_portfolio"] is False
    assert result.diagnostics["corporate_actions_modeled"] is False


def test_missing_exit_quote_leaves_position_unresolved(tmp_path):
    store, predictions, options = _fixture(tmp_path, missing_exit=True)
    result = run_option_backtest(predictions, options, store=store, config=OptionExecutionConfig(top_k=2, horizon_days=5))
    assert result.metrics["unresolved_positions"] == 2
    assert all(trade["unresolved"] is True for trade in result.trades)
    assert result.risk_events


def test_option_fill_waits_for_quote_availability_and_requires_depth():
    from stock_quant.alpha.options.execution import next_ask

    contract = build_option_contract("CALL", 200.0)
    quote = build_option_quote(contract, datetime(2026, 1, 2, 15, tzinfo=timezone.utc), 5.0, 5.2)
    delayed = quote.model_copy(update={"available_at": quote.available_at + timedelta(minutes=2)})
    no_depth = quote.model_copy(update={"ask_size": 0})
    assert next_ask([delayed], datetime(2026, 1, 2, 15, tzinfo=timezone.utc)) == delayed
    assert next_ask([no_depth], datetime(2026, 1, 2, 15, tzinfo=timezone.utc)) is None
