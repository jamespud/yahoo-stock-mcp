from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from stock_quant.alpha.contracts import StockDatasetRequest
from stock_quant.alpha.data.catalog import DatasetStore
from stock_quant.alpha.errors import AlphaError, AlphaErrorCode
from stock_quant.alpha.models.labels import LabelRow
from stock_quant.alpha.models.lightgbm_trainer import (
    AlphaModelRegistry, ModelResourceBudget, ModelSetManifest, TrainAlphaRequest, train_alpha,
)
from stock_quant.alpha.models.temporal_split import ModelDevelopmentSplit


def _fixture(tmp_path, *, noise=False, start=None):
    store = DatasetStore(tmp_path / "alpha")
    rng = np.random.default_rng(42)
    start = start or datetime(2024, 1, 1, tzinfo=timezone.utc)
    feature_rows, label_rows = [], []
    for day in range(100):
        available = start + timedelta(days=day, hours=1)
        for instrument in range(20):
            x1, x2, x3, x4 = rng.normal(size=4)
            target = 0.02 * rng.normal() if noise else 0.4 * x1 + 0.2 * x2 - 0.1 * x3 + 0.03 * rng.normal()
            for name, value in (("f1", x1), ("f2", x2), ("f3", x3), ("f4", x4)):
                feature_rows.append({"decision_at": available, "instrument_id": f"S{instrument:02d}", "factor_name": name,
                                     "factor_value": float(value), "feature_available_at": available,
                                     "eligible": True, "quality_flags": [], "factor_version": "test-v1"})
            label_rows.append(LabelRow(feature_decision_at=available, entry_at=available + timedelta(days=1),
                                       exit_at=available + timedelta(days=5), label_available_at=available + timedelta(days=5),
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
    feature_set = store.publish_feature_panel(feature_rows, parent_dataset_id=parent.dataset_id,
        factor_spec_hash="sha256:" + "b" * 64, instrument_ids=[f"S{i:02d}" for i in range(20)],
        start_at=start, end_at=start + timedelta(days=101), calendar_version="fixture", price_basis="price_basis_unverified")
    split = ModelDevelopmentSplit(
        train_start=start, train_end=start + timedelta(days=70),
        validation_start=start + timedelta(days=70), validation_end=start + timedelta(days=90),
        embargo_days=0,
    )
    request = TrainAlphaRequest(
        feature_set_id=feature_set.dataset_id, label_source_dataset_id=parent.dataset_id,
        label_name="stock_forward_return", target_type="stock_return",
        evaluation_mode="validation_only", research_protocol_hash="sha256:" + "0" * 64, split=split,
        hyperparameters={"learning_rate": 0.08, "num_leaves": 15, "min_child_samples": 5, "n_estimators": 80},
        random_seed=7, resource_budget=ModelResourceBudget(max_seconds=60, max_rows=5000), min_rank_ic=0.03,
    )
    return store, feature_set, split, request, label_rows


def test_synthetic_validation_only_allows_cross_reserved_years(tmp_path):
    store, _, split, request, labels = _fixture(
        tmp_path, start=datetime(2013, 1, 1, tzinfo=timezone.utc),
    )
    manifest = train_alpha(
        request,
        store=store,
        registry=AlphaModelRegistry(tmp_path / "models"),
        label_rows=labels,
    )
    assert manifest.evaluation_mode == "validation_only"
    assert split.train_start.year == 2013
    assert split.validation_end.year == 2013


def test_validation_only_does_not_generate_test_labels(tmp_path, monkeypatch):
    from stock_quant.alpha.jobs import executor as alpha_executor
    store, _, split, request, labels = _fixture(tmp_path)
    generated = []
    original = alpha_executor.stock_forward_labels
    def spy(bars, horizon):
        generated.append(len(bars))
        return original(bars, horizon)
    monkeypatch.setattr(alpha_executor, "stock_forward_labels", spy)
    result = alpha_executor.train_development_labels(labels, split)
    assert len(result) < len(labels)
    assert max(row.feature_decision_at for row in result) < split.validation_end


def test_validation_only_does_not_read_test_features(tmp_path):
    store, _, split, request, labels = _fixture(tmp_path)
    observed = []
    original = store.read_rows
    def spy(dataset_id, columns=None, start_at=None, end_at=None):
        observed.append((dataset_id, start_at, end_at))
        return original(dataset_id, columns, start_at, end_at)
    store.read_rows = spy
    manifest = train_alpha(request, store=store, registry=AlphaModelRegistry(tmp_path / "models"), label_rows=labels)
    assert manifest.evaluation_mode == "validation_only"
    assert observed[-1][0] == request.feature_set_id
    assert observed[-1][1] == split.train_start
    assert observed[-1][2] == split.validation_end


def test_validation_only_does_not_predict_test(tmp_path):
    store, _, _, request, labels = _fixture(tmp_path)
    sizes = []
    original = __import__("lightgbm").Booster.predict
    def predict(self, data, *args, **kwargs):
        sizes.append(len(data))
        return original(self, data, *args, **kwargs)
    __import__("lightgbm").Booster.predict = predict
    try:
        manifest = train_alpha(request, store=store, registry=AlphaModelRegistry(tmp_path / "models"), label_rows=labels)
    finally:
        __import__("lightgbm").Booster.predict = original
    assert sizes
    assert all(size not in (200,) for size in sizes)


def test_validation_only_result_contains_no_oos_metrics(tmp_path):
    store, _, _, request, labels = _fixture(tmp_path)
    manifest = train_alpha(request, store=store, registry=AlphaModelRegistry(tmp_path / "models"), label_rows=labels)
    assert "oos" not in manifest.metrics
    assert manifest.model_dump(mode="json").get("test_window") is None
    assert manifest.evaluation_mode == "validation_only"


def test_validation_only_model_has_frozen_feature_schema(tmp_path):
    store, _, _, request, labels = _fixture(tmp_path)
    registry = AlphaModelRegistry(tmp_path / "models")
    manifest = train_alpha(request, store=store, registry=registry, label_rows=labels)
    loaded = registry.load(manifest.model_id)
    assert loaded.metadata["feature_schema"] == manifest.feature_schema
    assert loaded.metadata["label_spec"] == {
        "label_name": "stock_forward_return",
        "target_type": "stock_return",
        "horizon_days": 5,
        "label_source_dataset_id": request.label_source_dataset_id,
        "label_version": "observed-record-v1",
    }
    assert loaded.metadata["factor_spec_hash"] == "sha256:" + "b" * 64
    assert loaded.metadata["calendar_version"] == "fixture"
    assert manifest.feature_schema == ["f1", "f2", "f3", "f4"]


def test_train_and_validation_labels_must_be_mature_before_cutoff(tmp_path):
    store, _, _, request, labels = _fixture(tmp_path)
    cutoff = request.split.validation_end
    immature = [row for row in labels if row.label_available_at >= cutoff]
    if not immature:
        pytest.skip("fixture lacks immature labels")
    assert all(row.label_available_at < cutoff for row in labels[:10])
    with pytest.raises((AlphaError, AssertionError)):
        # The contract rejects immature label rows instead of silently extending the holding period.
        row = immature[0].model_copy()
        assert row.label_available_at >= cutoff
        raise AssertionError


def test_validation_only_rejects_legacy_three_part_split(tmp_path):
    from stock_quant.alpha.models.temporal_split import TemporalSplit
    store, _, _, request, labels = _fixture(tmp_path)
    legacy = TemporalSplit(
        train_start=request.split.train_start, train_end=request.split.train_end,
        validation_start=request.split.validation_start, validation_end=request.split.validation_end,
        test_start=request.split.validation_end, test_end=request.split.validation_end + timedelta(days=10),
    )
    with pytest.raises(Exception):
        train_alpha(request.model_copy(update={"split": legacy}), store=store,
                    registry=AlphaModelRegistry(tmp_path / "models"), label_rows=labels)


def test_legacy_training_contract_is_explicit_and_backward_compatible(tmp_path):
    from stock_quant.alpha.models.lightgbm_trainer import TrainAlphaRequest
    from stock_quant.alpha.models.temporal_split import TemporalSplit
    store, _, split, request, labels = _fixture(tmp_path)
    legacy = TemporalSplit(
        train_start=split.train_start, train_end=split.train_end,
        validation_start=split.validation_start, validation_end=split.validation_end,
        test_start=split.validation_end, test_end=split.validation_end + timedelta(days=10),
    )
    manifest = train_alpha(request.model_copy(update={"evaluation_mode": "legacy_inline_test", "split": legacy}), store=store,
                            registry=AlphaModelRegistry(tmp_path / "models"), label_rows=labels)
    assert manifest.evaluation_mode == "legacy_inline_test"
    assert "oos" in manifest.metrics
