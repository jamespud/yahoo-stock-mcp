from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from stock_quant.alpha.data.catalog import DatasetStore
from stock_quant.alpha.errors import AlphaError, AlphaErrorCode
from stock_quant.alpha.models.lightgbm_trainer import AlphaModelRegistry, TrainAlphaRequest, train_alpha
from stock_quant.alpha.models.research_freeze import FreezeRequest, FrozenResearchStore, freeze_alpha_selection
from stock_quant.alpha.models.temporal_split import TemporalSplit

from .test_validation_only_training import _fixture as fixture
from .test_validation_only_training import _fixture


def _test_datasets(store, feature_set):
    from stock_quant.alpha.contracts import StockDatasetRequest
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return store.publish_stock(
        StockDatasetRequest(source="fixture", symbols=["S00"], start=start, end=start + timedelta(days=1)),
        [{"instrument_id": "S00", "start_at": start, "end_at": start + timedelta(hours=6),
          "available_at": start + timedelta(hours=6, minutes=1), "open": 100, "high": 101,
          "low": 99, "close": 100, "volume": 1, "adjustment": "raw", "source": "fixture",
          "source_version": "v1", "currency": "USD"}],
        calendar_version="fixture", source_version="v1", data_quality={},
    ).dataset_id


def _request(model_id, start, end, feature_set_id, label_dataset_id):
    return FreezeRequest(
        research_protocol_hash="sha256:" + "0" * 64,
        candidate_ledger_hash="sha256:" + "1" * 64,
        selected_model_ids=[model_id],
        test_feature_set_id=feature_set_id,
        test_label_source_dataset_id=label_dataset_id,
        test_start=start,
        test_end=end,
        selection_reason="highest validation rank_ic_mean",
        label_name="stock_forward_return",
        horizon_days=5,
    )


def test_freeze_rejects_model_without_validation_only_evidence(tmp_path):
    store, feature_set, split, request, labels = _fixture(tmp_path)
    test_feature = feature_set.dataset_id
    test_label = _test_datasets(store, feature_set)
    registry = AlphaModelRegistry(tmp_path / "models")
    legacy = train_alpha(request.model_copy(update={"evaluation_mode": "legacy_inline_test", "split": TemporalSplit(
        train_start=split.train_start, train_end=split.train_end,
        validation_start=split.validation_start, validation_end=split.validation_end,
        test_start=split.validation_end, test_end=split.validation_end + timedelta(days=10),
    )}), store=store, registry=registry, label_rows=labels)
    freeze = FrozenResearchStore(tmp_path / "freezes")
    with pytest.raises(AlphaError) as info:
        freeze_alpha_selection(_request(legacy.model_id, split.validation_end, split.validation_end + timedelta(days=10), test_feature, test_label), store=store, model_registry=registry, freeze_store=freeze)
    assert info.value.code == AlphaErrorCode.DATASET_MISMATCH


def test_freeze_is_content_addressed(tmp_path):
    store, feature_set, split, request, labels = _fixture(tmp_path)
    test_feature = feature_set.dataset_id
    test_label = _test_datasets(store, feature_set)
    registry = AlphaModelRegistry(tmp_path / "models")
    first = train_alpha(request, store=store, registry=registry, label_rows=labels)
    second = train_alpha(request, store=store, registry=registry, label_rows=labels)
    freeze = FrozenResearchStore(tmp_path / "freezes")
    record = freeze_alpha_selection(_request(first.model_id, split.validation_end, split.validation_end + timedelta(days=10), test_feature, test_label), store=store, model_registry=registry, freeze_store=freeze)
    repeated = freeze_alpha_selection(_request(second.model_id, split.validation_end, split.validation_end + timedelta(days=10), test_feature, test_label), store=store, model_registry=registry, freeze_store=freeze)
    assert record.freeze_id == repeated.freeze_id
    assert record.frozen_model_ids == [first.model_id]
    assert record.model_hashes[first.model_id] == first.model_hash
    assert record.research_protocol_hash == "sha256:" + "0" * 64
    assert record.test_dataset_hashes["test_label_source_dataset_id"] == store.load_manifest(test_label).content_hash


def test_freeze_rejects_model_hash_change(tmp_path):
    store, feature_set, split, request, labels = _fixture(tmp_path)
    test_feature = feature_set.dataset_id
    test_label = _test_datasets(store, feature_set)
    registry = AlphaModelRegistry(tmp_path / "models")
    manifest = train_alpha(request, store=store, registry=registry, label_rows=labels)
    (registry.model_path(manifest.model_id) / "model.txt").write_text("mutated")
    freeze = FrozenResearchStore(tmp_path / "freezes")
    with pytest.raises(AlphaError) as info:
        freeze_alpha_selection(_request(manifest.model_id, split.validation_end, split.validation_end + timedelta(days=10), test_feature, test_label), store=store, model_registry=registry, freeze_store=freeze)
    assert info.value.code == AlphaErrorCode.MODEL_NOT_FOUND


def test_freeze_rejects_test_window_mutation(tmp_path):
    store, feature_set, split, request, labels = _fixture(tmp_path)
    test_feature = feature_set.dataset_id
    test_label = _test_datasets(store, feature_set)
    registry = AlphaModelRegistry(tmp_path / "models")
    manifest = train_alpha(request, store=store, registry=registry, label_rows=labels)
    freeze = FrozenResearchStore(tmp_path / "freezes")
    original = freeze_alpha_selection(_request(manifest.model_id, split.validation_end, split.validation_end + timedelta(days=10), test_feature, test_label), store=store, model_registry=registry, freeze_store=freeze)
    mutated = _request(manifest.model_id, split.validation_end, split.validation_end + timedelta(days=11), test_feature, test_label)
    with pytest.raises(AlphaError):
        freeze.rewrite(original.freeze_id, mutated, model_hashes={manifest.model_id: manifest.model_hash}, test_dataset_hashes={"test_label_source_dataset_id": test_label})


def test_test_dataset_is_unreadable_from_validation_training_path(tmp_path):
    store, _, split, request, labels = _fixture(tmp_path)
    test_feature_set_id = "sha256:" + "4" * 64
    observed = []
    original = store.read_rows
    def spy(dataset_id, columns=None, start_at=None, end_at=None):
        observed.append(dataset_id)
        return original(dataset_id, columns, start_at, end_at)
    store.read_rows = spy
    train_alpha(request, store=store, registry=AlphaModelRegistry(tmp_path / "models"), label_rows=labels)
    assert test_feature_set_id not in observed
    assert observed == [request.feature_set_id]
