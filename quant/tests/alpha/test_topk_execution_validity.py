from datetime import datetime, timedelta, timezone

import pytest

from stock_quant.alpha.contracts import StockDatasetRequest
from stock_quant.alpha.data.catalog import DatasetStore
from stock_quant.alpha.data.corporate_actions import CorporateAction, CorporateActionCoverage, build_corporate_action_dataset
from stock_quant.alpha.data.price_attestation import PriceReference, attest_price_dataset
from stock_quant.alpha.errors import AlphaError, AlphaErrorCode
from stock_quant.alpha.portfolio.stock_backtest import TopKPortfolioConfig, run_top_k_backtest

SYMBOLS = [f"S{index:02d}" for index in range(21)]
DATES = [datetime(2024, 1, day, 14, 30, tzinfo=timezone.utc) for day in (2, 3, 4, 5, 8, 9)]
SIGNAL = datetime(2023, 12, 29, 21, 5, tzinfo=timezone.utc)


def _price(day, symbol, *, split=False):
    at = datetime(2024, 1, day, 14, 30, tzinfo=timezone.utc)
    open_price = 50.0 if split and symbol == "S00" else 100.0
    return {
        "instrument_id": symbol,
        "start_at": at,
        "end_at": at.replace(hour=21, minute=0),
        "available_at": at.replace(hour=21, minute=5),
        "open": open_price,
        "high": open_price + 1,
        "low": open_price - 1,
        "close": open_price,
        "volume": 100000,
        "adjustment": "raw",
        "source": "fixture",
        "source_version": "v1",
        "currency": "USD",
    }


def _fixture(tmp_path, *, actions=(), split_prices=False):
    store = DatasetStore(tmp_path / "alpha")
    rows = []
    for symbol in SYMBOLS:
        for day in (2, 3, 4, 5, 8, 9):
            rows.append(_price(day, symbol, split=split_prices and day >= 8))
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 10, tzinfo=timezone.utc)
    dataset = store.publish_stock(
        StockDatasetRequest(source="fixture", symbols=SYMBOLS, start=start, end=end, price_basis="raw_attested"),
        rows, calendar_version="fixture", source_version="v1", data_quality={"coverage": 1.0},
    )
    predictions = [
        {
            "decision_at": SIGNAL,
            "instrument_id": symbol,
            "prediction_value": float(len(SYMBOLS) - index),
            "model_id": "sha256:" + "a" * 64,
            "model_set_id": "sha256:" + "b" * 64,
            "fold": "fixture",
        }
        for index, symbol in enumerate(SYMBOLS)
    ]
    prediction_set = store.publish_prediction_panel(
        predictions, parent_feature_set_id=dataset.dataset_id,
        model_id="sha256:" + "a" * 64, model_set_id="sha256:" + "b" * 64,
        start_at=start, end_at=end, price_basis="raw_attested",
    )
    references = [
        PriceReference(
            instrument_id=row["instrument_id"], start_at=row["start_at"], declared_basis="raw",
            reference_open=row["open"], reference_high=row["high"], reference_low=row["low"],
            reference_close=row["close"], source="oracle", source_version="v1",
            evidence_id=f"{row['instrument_id']}:{row['start_at'].isoformat()}",
        )
        for row in rows
    ]
    attestation = attest_price_dataset(
        dataset.dataset_id, rows, references, start_at=start, end_at=end,
        source="fixture", source_version="v1",
        calendar_identity={"calendar_name": "XNYS", "calendar_version": "fixture-v1"},
    )
    coverage = [
        CorporateActionCoverage(
            instrument_id=symbol, start_at=start, end_at=end,
            source="fixture-actions", source_version="v1", status="VERIFIED",
        )
        for symbol in SYMBOLS
    ]
    action_dataset = build_corporate_action_dataset(
        list(actions), expected_instruments=SYMBOLS, start_at=start, end_at=end, coverage=coverage,
    )
    return store, dataset, prediction_set, attestation, action_dataset


def _action(action_type, terms, *, action_id, ex_date, payment_date=None):
    ex = datetime.fromisoformat(ex_date).replace(tzinfo=timezone.utc)
    return CorporateAction(
        action_id=action_id, instrument_id="S00", action_type=action_type,
        ex_date=ex, payment_date=datetime.fromisoformat(payment_date).replace(tzinfo=timezone.utc) if payment_date else None,
        effective_at=ex, source="fixture-actions", source_version="v1",
        quality_status="VERIFIED", terms=terms,
    )


def _run(tmp_path, *, actions=(), split_prices=False, stress=1.0, attestation_override=None):
    store, dataset, predictions, attestation, action_dataset = _fixture(
        tmp_path, actions=actions, split_prices=split_prices
    )
    return run_top_k_backtest(
        predictions, dataset, store=store,
        price_attestation=attestation_override or attestation,
        corporate_action_dataset=action_dataset,
        config=TopKPortfolioConfig(
            top_k=20, initial_cash=100000, commission_bps=5, slippage_bps=5,
            max_name_weight=0.05, max_gross_exposure=1.0,
        ),
        stress_cost_multiplier=stress,
    )


def test_next_session_open_has_no_lookahead(tmp_path):
    result = _run(tmp_path)
    assert result.fills
    assert all(datetime.fromisoformat(fill["signal_at"]) == SIGNAL for fill in result.fills)
    assert all(datetime.fromisoformat(fill["time"]).date() == datetime(2024, 1, 2).date() for fill in result.fills)
    assert result.diagnostics["same_bar_signal_fill"] is False


def test_top20_respects_cash_and_costs(tmp_path):
    result = _run(tmp_path)
    assert len(result.target_weights[0]["weights"]) == 20
    assert all(value <= 0.05 + 1e-12 for value in result.target_weights[0]["weights"].values())
    assert all(value >= -1e-9 for value in (row["cash"] for row in result.equity_curve))
    assert result.metrics["commission_paid"] > 0
    assert result.metrics["slippage_paid"] > 0


def test_top20_respects_weight_limits(tmp_path):
    result = _run(tmp_path)
    for row in result.positions:
        total = sum(position["value"] for position in row["positions"])
        if total > 0:
            assert max(position["value"] / total for position in row["positions"]) <= 0.051


def test_split_does_not_create_false_loss(tmp_path):
    split = _action("split", {"ratio": 2.0}, action_id="split-s00", ex_date="2024-01-08")
    result = _run(tmp_path, actions=[split], split_prices=True)
    before = next(row for row in result.equity_curve if row["date"] == "2024-01-05")
    after = next(row for row in result.equity_curve if row["date"] == "2024-01-08")
    assert after["equity"] >= before["equity"] * 0.99
    assert result.corporate_actions[0]["kind"] == "split"
    s00 = next(row for row in result.positions if row["date"] == "2024-01-08")
    position = next(item for item in s00["positions"] if item["symbol"] == "S00")
    assert position["quantity"] > 0


def test_dividend_entitlement_and_payment(tmp_path):
    dividend = _action(
        "cash_dividend", {"cash_per_share": 1.0}, action_id="div-s00",
        ex_date="2024-01-03", payment_date="2024-01-08",
    )
    result = _run(tmp_path, actions=[dividend])
    dividend_flows = [row for row in result.cash_ledger if row["kind"] == "dividend"]
    assert any(row["action_id"] == "div-s00" and row["amount"] > 0 for row in dividend_flows)


def test_account_equity_reconciles_daily(tmp_path):
    result = _run(tmp_path)
    assert result.metrics["max_reconciliation_error"] <= 1e-6
    for row in result.equity_curve:
        assert abs(row["equity"] - row["cash"] - row["positions_value"]) <= 1e-6
        positions_total = next(item for item in result.positions if item["date"] == row["date"])
        assert abs(sum(position["value"] for position in positions_total["positions"]) - row["positions_value"]) <= 1e-6


def test_unverified_prices_are_rejected(tmp_path):
    store, dataset, predictions, attestation, action_dataset = _fixture(tmp_path)
    blocked = attestation.model_copy(update={"attestation_status": "BLOCKED", "price_basis": "price_basis_unverified", "verified_symbols": []})
    with pytest.raises(AlphaError) as error:
        run_top_k_backtest(
            predictions, dataset, store=store, price_attestation=blocked,
            corporate_action_dataset=action_dataset,
        )
    assert error.value.code == AlphaErrorCode.PRICE_BASIS_UNVERIFIED


def test_double_cost_stress_is_reproducible(tmp_path):
    first = _run(tmp_path / "first", stress=2.0)
    second = _run(tmp_path / "second", stress=2.0)
    assert first.fills == second.fills
    assert first.equity_curve == second.equity_curve
    assert first.metrics["cost_stress_multiplier"] == 2.0
    assert first.metrics["commission_paid"] > first.metrics["baseline_commission_paid_estimate"]
