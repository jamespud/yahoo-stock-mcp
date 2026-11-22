from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from stock_quant.alpha.contracts import StockDatasetRequest
from stock_quant.alpha.data.catalog import DatasetStore
from stock_quant.alpha.models.labels import LabelRow
from stock_quant.alpha.models.lightgbm_trainer import AlphaModelRegistry, ModelResourceBudget, TrainAlphaRequest, _train_legacy_inline_test as train_alpha
from stock_quant.alpha.models.inference import predict_alpha
from stock_quant.alpha.models.temporal_split import TemporalSplit


def _fixture(tmp_path, *, noise=False):
    store = DatasetStore(tmp_path / "alpha")
    rng = np.random.default_rng(42)
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    feature_rows, label_rows = [], []
    for day in range(120):
        decision = start + timedelta(days=day)
        available = decision + timedelta(hours=1)
        decision = available
        for instrument in range(20):
            x1, x2, x3, x4 = rng.normal(size=4)
            target = 0.02 * rng.normal() if noise else 0.4 * x1 + 0.2 * x2 - 0.1 * x3 + 0.03 * rng.normal()
            for name, value in (("f1", x1), ("f2", x2), ("f3", x3), ("f4", x4)):
                feature_rows.append({"decision_at": decision, "instrument_id": f"S{instrument:02d}", "factor_name": name,
                                     "factor_value": float(value), "feature_available_at": available,
                                     "eligible": True, "quality_flags": [], "factor_version": "test-v1"})
            label_rows.append(LabelRow(feature_decision_at=decision, entry_at=decision + timedelta(days=1),
                                       exit_at=decision + timedelta(days=5), label_available_at=decision + timedelta(days=5),
                                       label_value=float(target), label_quality=[], instrument_id=f"S{instrument:02d}",
                                       label_name="stock_forward_return"))
    parent = store.publish_stock(
        StockDatasetRequest(source="fixture", symbols=["S00"], start=start, end=start + timedelta(days=1)),
        [{"instrument_id": "S00", "start_at": start, "end_at": start + timedelta(hours=6),
          "available_at": start + timedelta(hours=6, minutes=1), "open": 100, "high": 101,
          "low": 99, "close": 100, "volume": 1, "adjustment": "raw", "source": "fixture",
          "source_version": "v1", "currency": "USD"}],
        calendar_version="fixture", source_version="v1", data_quality={},
    )
    feature_set = store.publish_feature_panel(feature_rows, parent_dataset_id=parent.dataset_id, factor_spec_hash="sha256:" + "b" * 64,
                                              instrument_ids=[f"S{i:02d}" for i in range(20)], start_at=start,
                                              end_at=start + timedelta(days=121), calendar_version="fixture", price_basis="price_basis_unverified")
    split = TemporalSplit(
        train_start=start, train_end=start + timedelta(days=70), validation_start=start + timedelta(days=70),
        validation_end=start + timedelta(days=90), test_start=start + timedelta(days=90), test_end=start + timedelta(days=120),
    )
    request = TrainAlphaRequest(
        feature_set_id=feature_set.dataset_id, label_name="stock_forward_return", target_type="stock_return",
        evaluation_mode="legacy_inline_test", split=split,
        hyperparameters={"learning_rate": 0.08, "num_leaves": 15, "min_child_samples": 5, "n_estimators": 80},
        random_seed=7, resource_budget=ModelResourceBudget(max_seconds=60, max_rows=5000), min_rank_ic=0.03,
    )
    return store, feature_set, split, request, label_rows


def test_lightgbm_training_and_predictions_are_reproducible(tmp_path):
    store, feature_set, split, request, labels = _fixture(tmp_path)
    registry = AlphaModelRegistry(tmp_path / "models")
    first = train_alpha(request, store=store, registry=registry, label_rows=labels)
    second = train_alpha(request, store=store, registry=registry, label_rows=labels)
    assert first.model_hash == second.model_hash
    assert first.model_id == second.model_id
    assert first.validation_gate_passed is True
    assert first.is_alpha_certified is False
    prediction = predict_alpha(first.model_set_id, feature_set.dataset_id, split.test_start, split.test_end, store=store, registry=registry)
    rows = store.read_rows(prediction.dataset_id)
    assert rows
    assert prediction.dataset_kind == "prediction_panel"
    assert {row["model_id"] for row in rows} == {first.model_id}


def test_noise_fixture_is_not_certified_as_alpha(tmp_path):
    store, _, _, request, labels = _fixture(tmp_path / "noise", noise=True)
    registry = AlphaModelRegistry(tmp_path / "noise-models")
    manifest = train_alpha(request, store=store, registry=registry, label_rows=labels)
    assert manifest.validation_gate_passed is False
    assert manifest.is_alpha_certified is False


def test_corrupt_model_and_future_fold_predicting_past_are_rejected(tmp_path):
    store, feature_set, split, request, labels = _fixture(tmp_path)
    registry = AlphaModelRegistry(tmp_path / "models")
    manifest = train_alpha(request, store=store, registry=registry, label_rows=labels)
    model_file = registry.model_path(manifest.model_id) / "model.txt"
    model_file.write_text("corrupt")
    with pytest.raises(Exception):
        registry.load(manifest.model_id)
    with pytest.raises(Exception):
        predict_alpha(manifest.model_set_id, feature_set.dataset_id, split.train_start, split.validation_start, store=store, registry=registry)


def test_training_honors_configured_embargo(tmp_path):
    store, _, _, request, labels = _fixture(tmp_path)
    normal = train_alpha(request, store=store, registry=AlphaModelRegistry(tmp_path / "normal"), label_rows=labels)
    split = request.split.model_copy(update={"embargo_days": 5})
    embargoed_request = request.model_copy(update={"split": split})
    embargoed = train_alpha(embargoed_request, store=store, registry=AlphaModelRegistry(tmp_path / "embargoed"), label_rows=labels)
    assert embargoed.metrics["train"]["sample_count"] < normal.metrics["train"]["sample_count"]


def test_training_rejects_feature_available_after_decision():
    from stock_quant.alpha.errors import AlphaError, AlphaErrorCode
    from stock_quant.alpha.models.lightgbm_trainer import _feature_frame
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(AlphaError) as info:
        _feature_frame([{"decision_at": now, "instrument_id": "AAPL", "factor_name": "late", "factor_value": 1.0,
                         "feature_available_at": now + timedelta(minutes=1), "eligible": True, "quality_flags": [], "factor_version": "v1"}])
    assert info.value.code == AlphaErrorCode.DATASET_MISMATCH


def test_prediction_cannot_start_inside_validation_window(tmp_path):
    from stock_quant.alpha.errors import AlphaError, AlphaErrorCode
    store, feature_set, split, request, labels = _fixture(tmp_path)
    registry = AlphaModelRegistry(tmp_path / "models")
    manifest = train_alpha(request, store=store, registry=registry, label_rows=labels)
    with pytest.raises(AlphaError) as info:
        predict_alpha(manifest.model_set_id, feature_set.dataset_id, split.train_start, split.validation_end,
                      store=store, registry=registry)
    assert info.value.code == AlphaErrorCode.DATASET_MISMATCH
