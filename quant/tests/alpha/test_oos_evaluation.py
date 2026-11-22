from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from stock_quant.alpha.data.catalog import DatasetStore
from stock_quant.alpha.errors import AlphaError, AlphaErrorCode
from stock_quant.alpha.models.lightgbm_trainer import AlphaModelRegistry, TrainAlphaRequest, train_alpha
from stock_quant.alpha.models.oos_evaluation import evaluate_alpha_oos
from stock_quant.alpha.models.research_freeze import (
    FreezeRequest, FrozenResearchStore, freeze_alpha_selection, freeze_identity,
)
from stock_quant.alpha.research_admission import _FrozenOosGrant, _OOS_GRANT_SEAL, authorize_oos

from .test_validation_only_training import _fixture


def _test_grant(record):
    return _FrozenOosGrant(record=record, _seal=_OOS_GRANT_SEAL)


def _frozen(tmp_path):
    store, feature_set, split, request, labels = _fixture(tmp_path)
    test_label = store.publish_stock(
        __import__("stock_quant.alpha.contracts", fromlist=["StockDatasetRequest"]).StockDatasetRequest(
            source="fixture", symbols=["S00"], start=split.validation_end, end=split.validation_end + timedelta(days=1)),
        [{"instrument_id": "S00", "start_at": split.validation_end, "end_at": split.validation_end + timedelta(hours=6),
          "available_at": split.validation_end + timedelta(hours=6, minutes=1), "open": 100, "high": 101,
          "low": 99, "close": 100, "volume": 1, "adjustment": "raw", "source": "fixture",
          "source_version": "v1", "currency": "USD"}],
        calendar_version="fixture", source_version="v1", data_quality={},
    ).dataset_id
    registry = AlphaModelRegistry(tmp_path / "models")
    manifest = train_alpha(request, store=store, registry=registry, label_rows=labels)
    freeze = FrozenResearchStore(tmp_path / "freezes")
    record = freeze_alpha_selection(FreezeRequest(
        research_protocol_hash="sha256:" + "0" * 64,
        candidate_ledger_hash="sha256:" + "1" * 64,
        selected_model_ids=[manifest.model_id],
        test_feature_set_id=feature_set.dataset_id,
        test_label_source_dataset_id=test_label,
        test_start=split.validation_end,
        test_end=split.validation_end + timedelta(days=10),
        selection_reason="highest validation rank_ic_mean",
        label_name="stock_forward_return",
        horizon_days=5,
    ), store=store, model_registry=registry, freeze_store=freeze)
    return store, registry, record


def test_oos_evaluation_is_idempotent(tmp_path):
    store, registry, record = _frozen(tmp_path)
    first = evaluate_alpha_oos(_test_grant(record), store=store, model_registry=registry)
    second = evaluate_alpha_oos(_test_grant(record), store=store, model_registry=registry)
    assert first["freeze_id"] == second["freeze_id"]
    assert first["summary"] == second["summary"]
    assert first["prediction_dataset_id"] == second["prediction_dataset_id"]


def test_concurrent_oos_submission_has_single_authoritative_result(tmp_path):
    store, registry, record = _frozen(tmp_path)
    first = evaluate_alpha_oos(_test_grant(record), store=store, model_registry=registry)
    second = evaluate_alpha_oos(_test_grant(record), store=store, model_registry=registry)
    assert first == second


def test_oos_evaluation_uses_only_frozen_model(tmp_path, monkeypatch):
    store, registry, record = _frozen(tmp_path)
    module = __import__("stock_quant.alpha.models.oos_evaluation", fromlist=["_predict_alpha_for_frozen_oos"])
    original = module._predict_alpha_for_frozen_oos
    observed = []
    def spy(frozen_record, feature_set_id, start, end, *, store, registry):
        observed.append((frozen_record.freeze_id, feature_set_id, start, end))
        return original(frozen_record, feature_set_id, start, end, store=store, registry=registry)
    module._predict_alpha_for_frozen_oos = spy
    try:
        result = evaluate_alpha_oos(_test_grant(record), store=store, model_registry=registry)
    finally:
        module._predict_alpha_for_frozen_oos = original
    assert observed
    assert observed[0][0] == record.freeze_id
    assert observed[0][1] == record.test_dataset_ids["test_feature_set_id"]
    assert result["summary"]["evaluation_window"] == record.test_window


def test_oos_evaluation_rejects_a_bare_freeze_record(tmp_path):
    store, registry, record = _frozen(tmp_path)
    with pytest.raises(AlphaError) as info:
        evaluate_alpha_oos(record, store=store, model_registry=registry)
    assert info.value.code == AlphaErrorCode.FROZEN_OOS_AUTHORIZATION_INVALID


@pytest.mark.parametrize("mutation", ["protocol", "test_dataset_hash", "label_spec"])
def test_oos_authorization_rejects_tampered_freeze_record(tmp_path, mutation):
    store, registry, record = _frozen(tmp_path)
    if mutation == "protocol":
        tampered = record.model_copy(update={"research_protocol_hash": "sha256:" + "9" * 64})
    elif mutation == "test_dataset_hash":
        hashes = dict(record.test_dataset_hashes)
        hashes["test_label_source_dataset_id"] = "sha256:" + "8" * 64
        tampered = record.model_copy(update={"test_dataset_hashes": hashes})
    else:
        label_spec = dict(record.label_spec)
        label_spec["horizon_days"] = 20
        tampered = record.model_copy(update={"label_spec": label_spec})
    tampered = tampered.model_copy(update={"freeze_id": freeze_identity(tampered)})

    class CompletedFreezeJobs:
        path = str(tmp_path / f"tampered-{mutation}.sqlite3")

        def completed_freeze_job(self, freeze_id):
            return {"record": tampered.model_dump(mode="json")}

    with pytest.raises(AlphaError) as info:
        authorize_oos(
            tampered.freeze_id,
            job_store=CompletedFreezeJobs(),
            datasets=store,
            model_registry=registry,
        )
    assert info.value.code == AlphaErrorCode.FROZEN_OOS_AUTHORIZATION_INVALID
