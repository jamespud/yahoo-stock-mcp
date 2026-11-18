from datetime import datetime, timedelta, timezone

import pytest

from stock_quant.alpha.contracts import StockDatasetRequest
from stock_quant.alpha.data.catalog import DatasetStore
from stock_quant.alpha.portfolio.ranking import rank_predictions
from stock_quant.alpha.portfolio.stock_backtest import TopKPortfolioConfig, run_top_k_backtest
from stock_quant.alpha.portfolio.target_weights import equal_target_weights


def _fixture_store(tmp_path, symbols=25, days=60):
    store = DatasetStore(tmp_path / "alpha")
    start = datetime(2024, 1, 1, 14, 30, tzinfo=timezone.utc)
    rows = []
    for index, symbol in enumerate(f"S{i:03d}" for i in range(symbols)):
        for day in range(days):
            at = start + timedelta(days=day)
            close = 50 + index + day * (0.02 + index / 10000)
            rows.append({"instrument_id": symbol, "start_at": at, "end_at": at.replace(hour=21, minute=0),
                         "available_at": at.replace(hour=21, minute=1), "open": close - 0.1,
                         "high": close + 1, "low": close - 1, "close": close, "volume": 100000,
                         "adjustment": "raw", "source": "fixture", "source_version": "v1", "currency": "USD"})
    dataset = store.publish_stock(
        StockDatasetRequest(source="fixture", symbols=[f"S{i:03d}" for i in range(symbols)],
                            start=start, end=start + timedelta(days=days + 1), price_basis="raw_attested"),
        rows, calendar_version="fixture", source_version="v1", data_quality={"coverage": 1.0},
    )
    predictions = []
    for day in range(days):
        decision = (start + timedelta(days=day)).replace(hour=21, minute=0)
        for index in range(symbols):
            predictions.append({"decision_at": decision, "instrument_id": f"S{index:03d}",
                                "prediction_value": float((index * 7 + day * 3) % symbols),
                                "model_id": "sha256:" + "a" * 64, "model_set_id": "sha256:" + "b" * 64,
                                "fold": "test"})
    prediction_set = store.publish_prediction_panel(
        predictions, parent_feature_set_id=dataset.dataset_id, model_id="sha256:" + "a" * 64,
        model_set_id="sha256:" + "b" * 64, start_at=start, end_at=start + timedelta(days=days + 1),
        price_basis="raw_attested",
    )
    return store, dataset, prediction_set


def test_ranking_and_target_weights_enforce_top_k_and_caps():
    rows = [
        {"decision_at": datetime(2024, 1, 1, tzinfo=timezone.utc), "instrument_id": f"S{i}", "prediction_value": float(i), "eligible": True}
        for i in range(30)
    ]
    ranked = rank_predictions(rows, top_k=20)
    assert len(ranked) == 20
    assert [row["instrument_id"] for row in ranked[:3]] == ["S29", "S28", "S27"]
    weights = equal_target_weights(ranked, max_name_weight=0.05, max_gross_exposure=1.0)
    assert len(weights) == 20
    assert all(0 <= weight <= 0.05 + 1e-12 for weight in weights.values())
    assert sum(weights.values()) <= 1.0 + 1e-12


def test_top_k_backtest_reconciles_account_and_never_fills_on_signal_bar(tmp_path):
    store, dataset, prediction_set = _fixture_store(tmp_path)
    result = run_top_k_backtest(prediction_set, dataset, store=store, config=TopKPortfolioConfig(top_k=20, initial_cash=1_000_000))
    assert result.metrics["rebalance_count"] >= 1
    assert result.metrics["prediction_coverage"] == 1.0
    assert abs(result.final_account["equity"] - (result.final_account["cash"] + result.final_account["positions_value"])) < 1e-6
    assert abs(result.metrics["commission_paid"] - sum(fill["commission"] for fill in result.fills)) < 1e-9
    assert result.fills
    assert all(fill["time"] > fill["signal_at"] for fill in result.fills)
    assert all(abs(fill["price"] - fill["reference_open"]) > 0 for fill in result.fills if fill["side"] in {"buy", "sell"})


def test_hundred_stock_sparse_backtest_supports_multiple_rebalances(tmp_path):
    store, dataset, prediction_set = _fixture_store(tmp_path / "large", symbols=100, days=45)
    result = run_top_k_backtest(prediction_set, dataset, store=store, config=TopKPortfolioConfig(top_k=20, initial_cash=5_000_000))
    assert result.metrics["rebalance_count"] >= 3
    assert all(len(row["positions"]) <= 20 for row in result.positions)
    assert len(result.equity_curve) == 45


def test_top_k_rejects_unverified_execution_price_basis(tmp_path):
    from stock_quant.alpha.errors import AlphaError, AlphaErrorCode

    store, dataset, prediction_set = _fixture_store(tmp_path)
    manifest = dataset.model_copy(update={"price_basis": "price_basis_unverified"})
    with pytest.raises(AlphaError) as info:
        run_top_k_backtest(prediction_set, manifest, store=store, config=TopKPortfolioConfig(top_k=20))
    assert info.value.code == AlphaErrorCode.PRICE_BASIS_UNVERIFIED
