from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from stock_quant.alpha.contracts import StockDatasetRequest
from stock_quant.alpha.data.catalog import DatasetStore
from stock_quant.alpha.errors import AlphaErrorCode
from stock_quant.alpha.models.lightgbm_trainer import AlphaModelRegistry
from stock_quant.alpha.models.research_freeze import FreezeRequest, FrozenResearchStore, freeze_alpha_selection
from stock_quant.jobs import JobStore, execute_job


def _fixture(tmp_path, *, monkeypatch=None):
    registry_path = tmp_path / "registry.sqlite3"
    store = DatasetStore(registry_path.with_suffix(".alpha"))
    rng = np.random.default_rng(20261119)
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    bars, features = [], []
    for day in range(120):
        at = start + timedelta(days=day)
        available = at.replace(hour=21, minute=1)
        for instrument in range(8):
            close = 100 + instrument + day + rng.normal(scale=0.2)
            bars.append({
                "instrument_id": f"S{instrument:02d}", "start_at": at,
                "end_at": at.replace(hour=21), "available_at": available,
                "open": close, "high": close + 1, "low": close - 1, "close": close,
                "volume": 1000 + day, "adjustment": "raw", "source": "fixture",
                "source_version": "v1", "currency": "USD",
            })
            x1, x2 = rng.normal(size=2)
            for name, value in (("f1", x1), ("f2", x2)):
                features.append({
                    "decision_at": available, "instrument_id": f"S{instrument:02d}",
                    "factor_name": name, "factor_value": float(value),
                    "feature_available_at": available, "eligible": True,
                    "quality_flags": [], "factor_version": "test-v1",
                })
    dataset = store.publish_stock(
        StockDatasetRequest(source="fixture", symbols=[f"S{i:02d}" for i in range(8)], start=start, end=start + timedelta(days=121)),
        bars, calendar_version="fixture", source_version="v1", data_quality={},
    )
    feature_set = store.publish_feature_panel(
        features, parent_dataset_id=dataset.dataset_id,
        factor_spec_hash="sha256:" + "b" * 64,
        instrument_ids=[f"S{i:02d}" for i in range(8)], start_at=start,
        end_at=start + timedelta(days=121), calendar_version="fixture",
        price_basis="price_basis_unverified",
    )
    return registry_path, store, dataset, feature_set


def test_validation_freeze_and_oos_execute_through_alpha_jobs(tmp_path, monkeypatch):
    registry_path, store, dataset, feature_set = _fixture(tmp_path)
    observed_reads = []
    original_read_rows = DatasetStore.read_rows
    def spy(self, dataset_id, columns=None, start_at=None, end_at=None):
        observed_reads.append((dataset_id, start_at, end_at))
        return original_read_rows(self, dataset_id, columns, start_at, end_at)
    monkeypatch.setattr(DatasetStore, "read_rows", spy)
    with JobStore(registry_path) as jobs:
        train = jobs.submit_alpha({
            "kind": "train_alpha_model", "feature_set_id": feature_set.dataset_id,
            "label_name": "stock_forward_return", "target_type": "stock_return",
            "stock_dataset_id": dataset.dataset_id, "horizon_days": 5,
            "evaluation_mode": "validation_only",
            "research_protocol_hash": "sha256:" + "0" * 64,
            "split": {
                "train_start": "2024-01-01T00:00:00Z", "train_end": "2024-02-15T00:00:00Z",
                "validation_start": "2024-02-15T00:00:00Z", "validation_end": "2024-03-15T00:00:00Z",
            },
            "hyperparameters": {"n_estimators": 20, "min_child_samples": 2},
            "min_rank_ic": -1, "resource_budget": {"max_seconds": 60, "max_rows": 10000},
        })
        assert execute_job(jobs, jobs.claim()) is True
        trained = jobs.get_job(train["job_id"])
        assert trained["status"] == "completed"
        model_id = jobs.get_result(train["job_id"], "dataset")["model_id"]
        stock_reads = [item for item in observed_reads if item[0] == dataset.dataset_id]
        assert stock_reads
        assert all(end == datetime(2024, 3, 15, tzinfo=timezone.utc) for _, _, end in stock_reads)

        freeze = jobs.submit_alpha({
            "kind": "freeze_alpha_selection", "research_protocol_hash": "sha256:" + "0" * 64,
            "candidate_ledger_hash": "sha256:" + "1" * 64,
            "selected_model_ids": [model_id], "test_feature_set_id": feature_set.dataset_id,
            "test_label_source_dataset_id": dataset.dataset_id,
            "test_start": "2024-03-15T00:00:00Z", "test_end": "2024-04-20T00:00:00Z",
            "selection_reason": "validation winner", "label_name": "stock_forward_return",
            "target_type": "stock_return", "horizon_days": 5,
        })
        assert execute_job(jobs, jobs.claim()) is True
        frozen = jobs.get_job(freeze["job_id"])
        assert frozen["status"] == "completed"
        freeze_id = jobs.get_result(freeze["job_id"], "summary")["freeze_id"]
        freeze_path = registry_path.with_suffix(".freezes") / (freeze_id.removeprefix("sha256:") + ".json")
        freeze_path.unlink()

        oos = jobs.submit_alpha({"kind": "evaluate_alpha_oos", "freeze_id": freeze_id,
                                 "idempotency_key": f"alpha_oos:{freeze_id}"})
        assert execute_job(jobs, jobs.claim()) is True
        evaluated = jobs.get_job(oos["job_id"])
        assert evaluated["status"] == "completed", evaluated
        summary = jobs.get_result(oos["job_id"], "summary")
        assert "rank_ic_mean" in summary
        assert summary["evaluation_window"]["start"] == datetime(2024, 3, 15, tzinfo=timezone.utc).isoformat()
        repeated = jobs.submit_alpha({"kind": "evaluate_alpha_oos", "freeze_id": freeze_id,
                                      "idempotency_key": f"alpha_oos:{freeze_id}"})
        assert repeated["job_id"] == oos["job_id"]
        audit = jobs.list_alpha_research_audit(freeze_id=freeze_id)
        assert [item["event_type"] for item in audit] == [
            "OOS_SUBMISSION", "OOS_EXECUTION_ATTEMPT", "OOS_COMPLETED", "OOS_REPEAT_REQUEST",
        ]
        assert {item["job_id"] for item in audit} == {oos["job_id"]}


def test_freeze_is_not_published_when_job_cannot_begin_publication(tmp_path, monkeypatch):
    registry_path, _, dataset, feature_set = _fixture(tmp_path)
    with JobStore(registry_path) as jobs:
        train = jobs.submit_alpha({
            "kind": "train_alpha_model", "feature_set_id": feature_set.dataset_id,
            "label_name": "stock_forward_return", "target_type": "stock_return",
            "stock_dataset_id": dataset.dataset_id, "horizon_days": 5,
            "evaluation_mode": "validation_only", "research_protocol_hash": "sha256:" + "0" * 64,
            "split": {
                "train_start": "2024-01-01T00:00:00Z", "train_end": "2024-02-15T00:00:00Z",
                "validation_start": "2024-02-15T00:00:00Z", "validation_end": "2024-03-15T00:00:00Z",
            },
            "hyperparameters": {"n_estimators": 20, "min_child_samples": 2},
            "min_rank_ic": -1, "resource_budget": {"max_seconds": 60, "max_rows": 10000},
        })
        assert execute_job(jobs, jobs.claim()) is True
        model_id = jobs.get_result(train["job_id"], "dataset")["model_id"]
        freeze_job = jobs.submit_alpha({
            "kind": "freeze_alpha_selection", "research_protocol_hash": "sha256:" + "0" * 64,
            "candidate_ledger_hash": "sha256:" + "1" * 64,
            "selected_model_ids": [model_id], "test_feature_set_id": feature_set.dataset_id,
            "test_label_source_dataset_id": dataset.dataset_id,
            "test_start": "2024-03-15T00:00:00Z", "test_end": "2024-04-20T00:00:00Z",
            "selection_reason": "validation winner", "label_name": "stock_forward_return",
            "target_type": "stock_return", "horizon_days": 5,
        })
        monkeypatch.setattr(jobs, "begin_publish", lambda claim, result: False)
        assert execute_job(jobs, jobs.claim()) is False
        assert jobs.get_job(freeze_job["job_id"])["status"] == "running"
        freeze_root = registry_path.with_suffix(".freezes")
        assert list(freeze_root.glob("*.json")) == []


def test_oos_rejects_freeze_without_completed_freeze_job(tmp_path, monkeypatch):
    registry_path, alpha_store, dataset, feature_set = _fixture(tmp_path)
    with JobStore(registry_path) as jobs:
        train = jobs.submit_alpha({
            "kind": "train_alpha_model", "feature_set_id": feature_set.dataset_id,
            "label_name": "stock_forward_return", "target_type": "stock_return",
            "stock_dataset_id": dataset.dataset_id, "horizon_days": 5,
            "evaluation_mode": "validation_only", "research_protocol_hash": "sha256:" + "0" * 64,
            "split": {
                "train_start": "2024-01-01T00:00:00Z", "train_end": "2024-02-15T00:00:00Z",
                "validation_start": "2024-02-15T00:00:00Z", "validation_end": "2024-03-15T00:00:00Z",
            },
            "hyperparameters": {"n_estimators": 20, "min_child_samples": 2},
            "min_rank_ic": -1, "resource_budget": {"max_seconds": 60, "max_rows": 10000},
        })
        assert execute_job(jobs, jobs.claim()) is True
        model_id = jobs.get_result(train["job_id"], "dataset")["model_id"]
        record = freeze_alpha_selection(
            FreezeRequest(
                research_protocol_hash="sha256:" + "0" * 64,
                candidate_ledger_hash="sha256:" + "1" * 64,
                selected_model_ids=[model_id], test_feature_set_id=feature_set.dataset_id,
                test_label_source_dataset_id=dataset.dataset_id,
                test_start=datetime(2024, 3, 15, tzinfo=timezone.utc),
                test_end=datetime(2024, 4, 20, tzinfo=timezone.utc),
                selection_reason="validation winner", label_name="stock_forward_return",
                target_type="stock_return", horizon_days=5,
            ),
            store=alpha_store,
            model_registry=AlphaModelRegistry(registry_path.with_suffix(".models")),
            freeze_store=FrozenResearchStore(registry_path.with_suffix(".freezes")),
        )
        observed_reads = []
        original_read_rows = DatasetStore.read_rows
        def spy(self, dataset_id, columns=None, start_at=None, end_at=None):
            observed_reads.append(dataset_id)
            return original_read_rows(self, dataset_id, columns, start_at, end_at)
        monkeypatch.setattr(DatasetStore, "read_rows", spy)
        oos = jobs.submit_alpha({
            "kind": "evaluate_alpha_oos", "freeze_id": record.freeze_id,
            "idempotency_key": f"alpha_oos:{record.freeze_id}",
        })
        assert execute_job(jobs, jobs.claim()) is True
        failed = jobs.get_job(oos["job_id"])
        assert failed["status"] == "failed"
        assert failed["error_code"] == AlphaErrorCode.DATASET_MISMATCH.value
        assert observed_reads == []


def test_oos_submission_and_repeat_audit_persist_across_store_reopen(tmp_path):
    registry_path, _, _, _ = _fixture(tmp_path)
    freeze_id = "sha256:" + "a" * 64
    with JobStore(registry_path) as jobs:
        submitted = jobs.submit_alpha({
            "kind": "evaluate_alpha_oos", "freeze_id": freeze_id,
            "idempotency_key": f"alpha_oos:{freeze_id}",
        })
        repeated = jobs.submit_alpha({
            "kind": "evaluate_alpha_oos", "freeze_id": freeze_id,
            "idempotency_key": f"alpha_oos:{freeze_id}",
        })
        assert repeated["job_id"] == submitted["job_id"]
    with JobStore(registry_path) as jobs:
        audit = jobs.list_alpha_research_audit(freeze_id=freeze_id)
    assert [item["event_type"] for item in audit] == ["OOS_SUBMISSION", "OOS_REPEAT_REQUEST"]
    assert {item["job_id"] for item in audit} == {submitted["job_id"]}


def test_cancelled_freeze_job_cleans_staging_and_never_publishes(tmp_path, monkeypatch):
    registry_path, _, dataset, feature_set = _fixture(tmp_path)
    with JobStore(registry_path) as jobs:
        train = jobs.submit_alpha({
            "kind": "train_alpha_model", "feature_set_id": feature_set.dataset_id,
            "label_name": "stock_forward_return", "target_type": "stock_return",
            "stock_dataset_id": dataset.dataset_id, "horizon_days": 5,
            "evaluation_mode": "validation_only", "research_protocol_hash": "sha256:" + "0" * 64,
            "split": {
                "train_start": "2024-01-01T00:00:00Z", "train_end": "2024-02-15T00:00:00Z",
                "validation_start": "2024-02-15T00:00:00Z", "validation_end": "2024-03-15T00:00:00Z",
            },
            "hyperparameters": {"n_estimators": 20, "min_child_samples": 2},
            "min_rank_ic": -1, "resource_budget": {"max_seconds": 60, "max_rows": 10000},
        })
        assert execute_job(jobs, jobs.claim()) is True
        model_id = jobs.get_result(train["job_id"], "dataset")["model_id"]
        freeze_job = jobs.submit_alpha({
            "kind": "freeze_alpha_selection", "research_protocol_hash": "sha256:" + "0" * 64,
            "candidate_ledger_hash": "sha256:" + "1" * 64,
            "selected_model_ids": [model_id], "test_feature_set_id": feature_set.dataset_id,
            "test_label_source_dataset_id": dataset.dataset_id,
            "test_start": "2024-03-15T00:00:00Z", "test_end": "2024-04-20T00:00:00Z",
            "selection_reason": "validation winner", "label_name": "stock_forward_return",
            "target_type": "stock_return", "horizon_days": 5,
        })
        original_stage = FrozenResearchStore.stage
        def stage_then_cancel(self, record, *, checkpoint=None):
            staged = original_stage(self, record, checkpoint=checkpoint)
            jobs.cancel(freeze_job["job_id"])
            return staged
        monkeypatch.setattr(FrozenResearchStore, "stage", stage_then_cancel)
        assert execute_job(jobs, jobs.claim()) is False
        freeze_root = registry_path.with_suffix(".freezes")
        assert list(freeze_root.glob("*.json")) == []
        assert list(freeze_root.glob(".freeze-*.tmp")) == []


def test_oos_legacy_and_custom_idempotency_keys_replay_against_same_freeze_job(tmp_path):
    registry_path, _, _, _ = _fixture(tmp_path)
    freeze_id = "sha256:" + "d" * 64
    with JobStore(registry_path) as jobs:
        first = jobs.submit_alpha({"kind": "evaluate_alpha_oos", "freeze_id": freeze_id})
        repeated = jobs.submit_alpha({
            "kind": "evaluate_alpha_oos", "freeze_id": freeze_id,
            "idempotency_key": "legacy-custom-oos-key",
        })
        assert repeated["job_id"] == first["job_id"]
    with JobStore(registry_path) as jobs:
        assert execute_job(jobs, jobs.claim()) is True
        failed = jobs.get_job(first["job_id"])
        assert failed["status"] == "failed"
        assert failed["error_code"] == AlphaErrorCode.DATASET_MISMATCH.value
        audit = jobs.list_alpha_research_audit(freeze_id=freeze_id)
    assert [item["event_type"] for item in audit] == [
        "OOS_SUBMISSION", "OOS_REPEAT_REQUEST", "OOS_EXECUTION_ATTEMPT", "OOS_FAILED",
    ]


def test_oos_finish_failure_path_is_audited(tmp_path):
    registry_path, _, _, _ = _fixture(tmp_path)
    freeze_id = "sha256:" + "e" * 64
    with JobStore(registry_path) as jobs:
        job = jobs.submit_alpha({"kind": "evaluate_alpha_oos", "freeze_id": freeze_id})
        claim = jobs.claim()
        assert jobs.finish(claim, error=RuntimeError("executor unavailable"), error_code="DATA_UNAVAILABLE") is True
        audit = jobs.list_alpha_research_audit(freeze_id=freeze_id)
    assert [item["event_type"] for item in audit] == [
        "OOS_SUBMISSION", "OOS_EXECUTION_ATTEMPT", "OOS_FAILED",
    ]
    assert audit[-1]["detail"] == {"status": "failed", "error_code": "DATA_UNAVAILABLE"}
