from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from stock_quant.alpha.data.catalog import DatasetStore
from stock_quant.alpha.errors import AlphaError, AlphaErrorCode
from stock_quant.alpha.models.lightgbm_trainer import AlphaModelRegistry, TrainAlphaRequest, _train_legacy_inline_test as train_legacy_alpha, train_alpha
from stock_quant.alpha.models.research_freeze import FreezeRequest, FrozenResearchStore, freeze_alpha_selection
from stock_quant.alpha.models.temporal_split import TemporalSplit

from .test_validation_only_training import _fixture as fixture
from .test_validation_only_training import _fixture


def _test_datasets(store, feature_set, *, source_version="v1"):
    from stock_quant.alpha.contracts import StockDatasetRequest
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return store.publish_stock(
        StockDatasetRequest(source="fixture", symbols=["S00"], start=start, end=start + timedelta(days=1)),
        [{"instrument_id": "S00", "start_at": start, "end_at": start + timedelta(hours=6),
          "available_at": start + timedelta(hours=6, minutes=1), "open": 100, "high": 101,
          "low": 99, "close": 100, "volume": 1, "adjustment": "raw", "source": "fixture",
          "source_version": source_version, "currency": "USD"}],
        calendar_version="fixture", source_version=source_version, data_quality={},
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
    legacy = train_legacy_alpha(request.model_copy(update={"evaluation_mode": "legacy_inline_test", "split": TemporalSplit(
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


def test_freeze_rejects_protocol_hash_mismatch(tmp_path):
    store, feature_set, split, request, labels = _fixture(tmp_path)
    test_feature = feature_set.dataset_id
    test_label = _test_datasets(store, feature_set)
    registry = AlphaModelRegistry(tmp_path / "models")
    manifest = train_alpha(
        request.model_copy(update={"research_protocol_hash": "sha256:" + "2" * 64}),
        store=store, registry=registry, label_rows=labels,
    )
    freeze = FrozenResearchStore(tmp_path / "freezes")
    freeze_request = _request(
        manifest.model_id, split.validation_end, split.validation_end + timedelta(days=10),
        test_feature, test_label,
    ).model_copy(update={"research_protocol_hash": "sha256:" + "3" * 64})
    with pytest.raises(AlphaError) as info:
        freeze_alpha_selection(freeze_request, store=store, model_registry=registry, freeze_store=freeze)
    assert info.value.code == AlphaErrorCode.DATASET_MISMATCH
    assert info.value.context["mismatches"][0]["field"] == "research_protocol_hash"


def test_freeze_rejects_label_horizon_mismatch(tmp_path):
    store, feature_set, split, request, labels = _fixture(tmp_path)
    test_feature = feature_set.dataset_id
    test_label = _test_datasets(store, feature_set)
    registry = AlphaModelRegistry(tmp_path / "models")
    manifest = train_alpha(request, store=store, registry=registry, label_rows=labels)
    freeze = FrozenResearchStore(tmp_path / "freezes")
    freeze_request = _request(
        manifest.model_id, split.validation_end, split.validation_end + timedelta(days=10),
        test_feature, test_label,
    ).model_copy(update={"horizon_days": 20})
    with pytest.raises(AlphaError) as info:
        freeze_alpha_selection(freeze_request, store=store, model_registry=registry, freeze_store=freeze)
    assert info.value.code == AlphaErrorCode.DATASET_MISMATCH
    assert any(item["field"] == "label_spec" for item in info.value.context["mismatches"])


def test_freeze_rejects_test_window_before_validation_end(tmp_path):
    store, feature_set, split, request, labels = _fixture(tmp_path)
    test_feature = feature_set.dataset_id
    test_label = _test_datasets(store, feature_set)
    registry = AlphaModelRegistry(tmp_path / "models")
    manifest = train_alpha(request, store=store, registry=registry, label_rows=labels)
    freeze = FrozenResearchStore(tmp_path / "freezes")
    freeze_request = _request(
        manifest.model_id, split.validation_end - timedelta(days=1), split.validation_end + timedelta(days=10),
        test_feature, test_label,
    )
    with pytest.raises(AlphaError) as info:
        freeze_alpha_selection(freeze_request, store=store, model_registry=registry, freeze_store=freeze)
    assert info.value.code == AlphaErrorCode.DATASET_MISMATCH
    assert any(item["field"] == "test_start" for item in info.value.context["mismatches"])


def test_freeze_rejects_label_source_lineage_mismatch(tmp_path):
    store, feature_set, split, request, labels = _fixture(tmp_path)
    test_feature = feature_set.dataset_id
    test_label = _test_datasets(store, feature_set, source_version="v2")
    registry = AlphaModelRegistry(tmp_path / "models")
    manifest = train_alpha(request, store=store, registry=registry, label_rows=labels)
    freeze = FrozenResearchStore(tmp_path / "freezes")
    freeze_request = _request(
        manifest.model_id, split.validation_end, split.validation_end + timedelta(days=10),
        test_feature, test_label,
    )
    with pytest.raises(AlphaError) as info:
        freeze_alpha_selection(freeze_request, store=store, model_registry=registry, freeze_store=freeze)
    assert info.value.code == AlphaErrorCode.DATASET_MISMATCH
    assert any(item["field"] == "label_source.source_version" for item in info.value.context["mismatches"])


def test_freeze_rejects_factor_spec_mismatch(tmp_path):
    store, feature_set, split, request, labels = _fixture(tmp_path)
    test_label = _test_datasets(store, feature_set)
    alternate_feature = store.publish_feature_panel(
        store.read_rows(feature_set.dataset_id), parent_dataset_id=store.load_manifest(feature_set.dataset_id).parent_dataset_ids[0],
        factor_spec_hash="sha256:" + "c" * 64,
        instrument_ids=feature_set.instrument_ids, start_at=feature_set.start_at, end_at=feature_set.end_at,
        calendar_version=feature_set.calendar_version, price_basis=feature_set.price_basis,
    )
    registry = AlphaModelRegistry(tmp_path / "models")
    manifest = train_alpha(request, store=store, registry=registry, label_rows=labels)
    freeze = FrozenResearchStore(tmp_path / "freezes")
    freeze_request = _request(
        manifest.model_id, split.validation_end, split.validation_end + timedelta(days=10),
        alternate_feature.dataset_id, test_label,
    )
    with pytest.raises(AlphaError) as info:
        freeze_alpha_selection(freeze_request, store=store, model_registry=registry, freeze_store=freeze)
    assert info.value.code == AlphaErrorCode.DATASET_MISMATCH
    assert any(item["field"] == "factor_spec_hash" for item in info.value.context["mismatches"])


def test_freeze_rejects_calendar_mismatch(tmp_path):
    store, feature_set, split, request, labels = _fixture(tmp_path)
    test_label = _test_datasets(store, feature_set)
    alternate_feature = store.publish_feature_panel(
        store.read_rows(feature_set.dataset_id), parent_dataset_id=store.load_manifest(feature_set.dataset_id).parent_dataset_ids[0],
        factor_spec_hash=feature_set.source_version,
        instrument_ids=feature_set.instrument_ids, start_at=feature_set.start_at, end_at=feature_set.end_at,
        calendar_version="alternate-calendar", price_basis=feature_set.price_basis,
    )
    registry = AlphaModelRegistry(tmp_path / "models")
    manifest = train_alpha(request, store=store, registry=registry, label_rows=labels)
    freeze = FrozenResearchStore(tmp_path / "freezes")
    with pytest.raises(AlphaError) as info:
        freeze_alpha_selection(
            _request(manifest.model_id, split.validation_end, split.validation_end + timedelta(days=10),
                     alternate_feature.dataset_id, test_label),
            store=store, model_registry=registry, freeze_store=freeze,
        )
    assert info.value.code == AlphaErrorCode.DATASET_MISMATCH
    assert any(item["field"] == "calendar_version" for item in info.value.context["mismatches"])


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


def test_freeze_store_does_not_publish_when_fenced_checkpoint_fails(tmp_path):
    store, feature_set, split, request, labels = _fixture(tmp_path)
    test_feature = feature_set.dataset_id
    test_label = _test_datasets(store, feature_set)
    registry = AlphaModelRegistry(tmp_path / "models")
    manifest = train_alpha(request, store=store, registry=registry, label_rows=labels)
    freeze = FrozenResearchStore(tmp_path / "freezes")
    record = freeze_alpha_selection(
        _request(
            manifest.model_id, split.validation_end, split.validation_end + timedelta(days=10),
            test_feature, test_label,
        ),
        store=store,
        model_registry=registry,
    )
    calls = 0
    def checkpoint():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("OWNERSHIP_LOST")
    with pytest.raises(RuntimeError):
        freeze.save(record, checkpoint=checkpoint)
    assert calls == 2
    assert list(freeze.root.glob("*.json")) == []
