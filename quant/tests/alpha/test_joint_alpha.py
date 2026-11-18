from datetime import datetime, timedelta, timezone

from stock_quant.alpha.contracts import StockDatasetRequest
from stock_quant.alpha.data.catalog import DatasetStore
from stock_quant.alpha.factors.engine import combine_feature_panels
from stock_quant.alpha.models.lightgbm_trainer import AlphaModelRegistry, ModelResourceBudget, TrainAlphaRequest, train_alpha
from stock_quant.alpha.models.labels import LabelRow
from stock_quant.alpha.models.temporal_split import TemporalSplit


def test_stock_and_option_features_join_by_pit_and_train_together(tmp_path):
    store = DatasetStore(tmp_path / "alpha")
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    stock_rows, option_rows, labels = [], [], []
    for day in range(60):
        decision = start + timedelta(days=day, hours=1)
        option_decision = decision + timedelta(hours=1)
        for i in range(12):
            stock_rows.append({"decision_at": decision, "instrument_id": f"S{i:02d}", "factor_name": "momentum", "factor_value": float(i + day % 3), "feature_available_at": decision, "eligible": True, "quality_flags": [], "factor_version": "fixture"})
            # This option feature is known only after the stock decision and joins on a later PIT day.
            option_rows.append({"decision_at": option_decision, "instrument_id": f"S{i:02d}", "factor_name": "atm_iv", "factor_value": float((i * 3 + day) % 7) / 100, "feature_available_at": option_decision, "eligible": True, "quality_flags": [], "factor_version": "fixture"})
            labels.append(LabelRow(feature_decision_at=decision, entry_at=decision + timedelta(days=1), exit_at=decision + timedelta(days=5), label_available_at=decision + timedelta(days=5), label_value=0.2 * i / 12 + 0.01 * ((i * 3 + day) % 7) - 0.03, label_quality=[], instrument_id=f"S{i:02d}", label_name="stock_forward_return"))
    source = store.publish_stock(
        StockDatasetRequest(source="fixture", symbols=["S00"], start=start, end=start + timedelta(days=1)),
        [{"instrument_id": "S00", "start_at": start, "end_at": start + timedelta(hours=6), "available_at": start + timedelta(hours=6, minutes=1), "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1, "adjustment": "raw", "source": "fixture", "source_version": "v1", "currency": "USD"}],
        calendar_version="fixture", source_version="v1", data_quality={},
    )
    parent = store.publish_feature_panel(stock_rows, parent_dataset_id=source.dataset_id, factor_spec_hash="sha256:" + "b" * 64, instrument_ids=[f"S{i:02d}" for i in range(12)], start_at=start, end_at=start + timedelta(days=61), calendar_version="fixture", price_basis="price_basis_unverified")
    option = store.publish_feature_panel(option_rows, parent_dataset_id=parent.dataset_id, factor_spec_hash="sha256:" + "c" * 64, instrument_ids=[f"S{i:02d}" for i in range(12)], start_at=start, end_at=start + timedelta(days=61), calendar_version="fixture", price_basis="price_basis_unverified")
    combined = combine_feature_panels(parent, [option], store=store)
    names = {row["factor_name"] for row in store.read_rows(combined.dataset_id)}
    assert names == {"momentum", "atm_iv"}
    assert combined.parent_dataset_ids == [parent.dataset_id, option.dataset_id]
    split = TemporalSplit(train_start=start, train_end=start + timedelta(days=35), validation_start=start + timedelta(days=35), validation_end=start + timedelta(days=45), test_start=start + timedelta(days=45), test_end=start + timedelta(days=60))
    request = TrainAlphaRequest(feature_set_id=combined.dataset_id, label_name="stock_forward_return", target_type="stock_return_with_option_features", split=split, random_seed=3, resource_budget=ModelResourceBudget(max_seconds=60, max_rows=10000))
    model = train_alpha(request, store=store, registry=AlphaModelRegistry(tmp_path / "models"), label_rows=labels)
    assert "atm_iv" in model.feature_schema
    assert model.metrics["oos"]["prediction_coverage"] == 1.0
