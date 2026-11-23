from __future__ import annotations

import io
import json
import sys
from datetime import timedelta

import pytest

from stock_quant.alpha.data.catalog import DatasetStore
from stock_quant.alpha.errors import AlphaErrorCode
from stock_quant.alpha.models.inference import predict_alpha
from stock_quant.alpha.models.lightgbm_trainer import AlphaModelRegistry, train_alpha
from stock_quant.alpha.models.temporal_split import TemporalSplit
from stock_quant.alpha.jobs.requests import ALPHA_REQUEST_ADAPTER
from stock_quant import __main__ as bridge
from stock_quant.compiler import digest
from stock_quant.jobs import JobStore, execute_job

from .test_alpha_research_jobs import _completed_freeze, _fixture as _jobs_fixture
from .test_validation_only_training import _fixture


def _training_job(**overrides):
    request = {
        "kind": "train_alpha_model",
        "feature_set_id": "sha256:" + "1" * 64,
        "label_name": "stock_forward_return",
        "target_type": "stock_return",
        "stock_dataset_id": "sha256:" + "2" * 64,
        "horizon_days": 5,
        "evaluation_mode": "validation_only",
        "research_protocol_hash": "sha256:" + "3" * 64,
        "split": {
            "train_start": "2024-01-01T00:00:00Z",
            "train_end": "2024-02-01T00:00:00Z",
            "validation_start": "2024-02-01T00:00:00Z",
            "validation_end": "2024-03-01T00:00:00Z",
        },
    }
    request.update(overrides)
    return request


@pytest.mark.parametrize(
    ("overrides", "reason", "fields"),
    [
        ({"evaluation_mode": None}, "RESEARCH_MODE_REQUIRED", ["evaluation_mode"]),
        ({"evaluation_mode": "legacy_inline_test"}, "LEGACY_INLINE_TEST_DISABLED", ["evaluation_mode"]),
        ({"evaluation_mode": "unknown_mode"}, "RESEARCH_MODE_REQUIRED", ["evaluation_mode"]),
        ({"split": {"train_start": "2024-01-01T00:00:00Z", "train_end": "2024-02-01T00:00:00Z", "validation_start": "2024-02-01T00:00:00Z", "validation_end": "2024-03-01T00:00:00Z", "test_start": "2024-03-01T00:00:00Z", "test_end": "2024-04-01T00:00:00Z"}}, "TEST_WINDOW_NOT_ALLOWED", ["split.test_end", "split.test_start"]),
        ({"research_protocol_hash": None}, "RESEARCH_PROTOCOL_REQUIRED", ["research_protocol_hash"]),
        ({"stock_dataset_id": None}, "STOCK_LABEL_DATASET_REQUIRED", ["stock_dataset_id"]),
        ({"research_protocol_hash": "not-a-hash"}, None, ["research_protocol_hash"]),
        ({"split": {"train_start": "2024-02-01T00:00:00Z", "train_end": "2024-01-01T00:00:00Z", "validation_start": "2024-02-01T00:00:00Z", "validation_end": "2024-03-01T00:00:00Z"}}, None, ["split"]),
        ({"split": {"train_start": "2024-01-01T00:00:00Z", "train_end": "2024-02-01T00:00:00Z", "validation_start": "2024-02-01T00:00:00Z", "validation_end": "2024-03-01T00:00:00Z", "embargo_days": -1}}, None, ["split.embargo_days"]),
        ({"unexpected": True}, None, ["unexpected"]),
    ],
)
def test_training_admission_rejects_before_enqueue_and_audits(tmp_path, overrides, reason, fields):
    with JobStore(tmp_path / "registry.sqlite3") as jobs:
        with pytest.raises(ValueError):
            jobs.submit_alpha(_training_job(**overrides))
        assert jobs.connection.execute("SELECT count(*) FROM jobs").fetchone() == (0,)
        audit = jobs.list_alpha_research_audit()
        assert len(audit) == 1
        assert audit[0]["event_type"] == "TRAINING_SUBMISSION_REJECTED"
        assert audit[0]["detail"]["source"] == "runtime"
        assert audit[0]["detail"]["fields"] == fields
        if reason is not None:
            assert audit[0]["detail"]["reason"] == reason


@pytest.mark.parametrize("raw", [
    {"kind": "train_alpha_model", "evaluation_mode": "legacy_inline_test"},
    {"kind": "train_alpha_model"},
])
def test_worker_rejects_queued_legacy_or_modelless_jobs_before_dataset_reads(tmp_path, monkeypatch, raw):
    with JobStore(tmp_path / "registry.sqlite3") as jobs:
        payload = raw
        jobs.connection.execute(
            "INSERT INTO jobs(id,status,request,config_hash) VALUES (?, 'queued', ?, ?)",
            ("alpha_legacy_queued", json.dumps(payload), digest(payload)),
        )
        jobs.connection.commit()
        observed = []
        original = DatasetStore.__init__
        def spy(self, *args, **kwargs):
            observed.append((args, kwargs))
            return original(self, *args, **kwargs)
        monkeypatch.setattr(DatasetStore, "__init__", spy)
        claim = jobs.claim()
        assert claim is not None
        assert execute_job(jobs, claim) is True
        job = jobs.get_job(claim["id"])
        assert job["status"] == "failed"
        assert job["error_code"] in {"RESEARCH_MODE_REQUIRED", "LEGACY_INLINE_TEST_DISABLED"}
        assert observed == []


def test_direct_train_alpha_rejects_legacy_before_dataset_reads(tmp_path):
    store, _, split, request, labels = _fixture(tmp_path)
    legacy = TemporalSplit(
        train_start=split.train_start,
        train_end=split.train_end,
        validation_start=split.validation_start,
        validation_end=split.validation_end,
        test_start=split.validation_end,
        test_end=split.validation_end + timedelta(days=10),
    )
    observed = []
    original = store.read_rows
    store.read_rows = lambda *args, **kwargs: observed.append(args) or original(*args, **kwargs)
    with pytest.raises(Exception, match="LEGACY_INLINE_TEST_DISABLED"):
        train_alpha(
            request.model_copy(update={"evaluation_mode": "legacy_inline_test", "split": legacy}),
            store=store,
            registry=AlphaModelRegistry(tmp_path / "models"),
            label_rows=labels,
        )
    assert observed == []


def test_public_prediction_rejects_validation_only_model_before_feature_reads(tmp_path):
    store, feature_set, split, request, labels = _fixture(tmp_path)
    registry = AlphaModelRegistry(tmp_path / "models")
    model = train_alpha(request, store=store, registry=registry, label_rows=labels)
    observed = []
    original = store.read_rows
    store.read_rows = lambda *args, **kwargs: observed.append(args) or original(*args, **kwargs)
    with pytest.raises(Exception, match="RESEARCH_PREDICTION_REQUIRES_FREEZE"):
        predict_alpha(
            model.model_set_id,
            feature_set.dataset_id,
            split.validation_end,
            split.validation_end + timedelta(days=10),
            store=store,
            registry=registry,
        )
    assert observed == []


def test_oos_submission_requires_completed_freeze_before_enqueue(tmp_path):
    freeze_id = "sha256:" + "f" * 64
    with JobStore(tmp_path / "registry.sqlite3") as jobs:
        with pytest.raises(ValueError, match="FROZEN_OOS_AUTHORIZATION_INVALID"):
            jobs.submit_alpha({"kind": "evaluate_alpha_oos", "freeze_id": freeze_id})
        assert jobs.connection.execute("SELECT count(*) FROM jobs").fetchone() == (0,)
        audit = jobs.list_alpha_research_audit(freeze_id=freeze_id)
    assert audit == [{
        "event_id": audit[0]["event_id"],
        "event_type": "OOS_SUBMISSION_REJECTED",
        "freeze_id": freeze_id,
        "job_id": "",
        "idempotency_key": None,
        "attempt": None,
        "detail": {"reason": "FROZEN_OOS_AUTHORIZATION_INVALID", "message": "OOS evaluation requires a completed Alpha freeze job"},
        "created_at": audit[0]["created_at"],
    }]


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"evaluation_mode": None}, "RESEARCH_MODE_REQUIRED"),
        ({"evaluation_mode": "legacy_inline_test"}, "LEGACY_INLINE_TEST_DISABLED"),
    ],
)
def test_training_mode_is_rejected_by_python_rpc_without_enqueue_or_wake(
    tmp_path, monkeypatch, capsys, overrides, reason,
):
    registry = str(tmp_path / "registry.sqlite3")
    job = _training_job(**overrides)
    payload = {"method": "submit_alpha", "registry_path": registry, "job": job}
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode())))
    wakes = []
    monkeypatch.setattr(bridge, "wake_worker", wakes.append)
    with pytest.raises(SystemExit, match="1"):
        bridge.main()
    assert wakes == []
    assert reason in json.loads(capsys.readouterr().out)["error"]["message"]
    with JobStore(registry) as jobs:
        assert jobs.connection.execute("SELECT count(*) FROM jobs").fetchone() == (0,)
        event, = jobs.list_alpha_research_audit()
    assert event["event_type"] == "TRAINING_SUBMISSION_REJECTED"
    assert event["detail"]["reason"] == reason


def test_oos_submission_revalidates_frozen_model_hash_before_enqueue(tmp_path):
    registry_path, _, dataset, feature_set = _jobs_fixture(tmp_path)
    with JobStore(registry_path) as jobs:
        freeze_id = _completed_freeze(jobs, dataset, feature_set)
        frozen = jobs.completed_freeze_job(freeze_id)
        model_id = frozen["record"]["frozen_model_ids"][0]
        model_file = registry_path.with_suffix(".models") / model_id.removeprefix("sha256:") / "model.txt"
        model_file.write_text("tampered")
        jobs_before = jobs.connection.execute("SELECT count(*) FROM jobs").fetchone()[0]
        with pytest.raises(ValueError, match="FROZEN_OOS_AUTHORIZATION_INVALID"):
            jobs.submit_alpha({"kind": "evaluate_alpha_oos", "freeze_id": freeze_id})
        assert jobs.connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == jobs_before
        assert jobs.list_alpha_research_audit(freeze_id=freeze_id)[-1]["event_type"] == "OOS_SUBMISSION_REJECTED"


def test_worker_rejects_queued_public_prediction_of_validation_only_model_before_feature_reads(tmp_path, monkeypatch):
    store, feature_set, split, request, labels = _fixture(tmp_path)
    registry_path = tmp_path / "registry.sqlite3"
    registry = AlphaModelRegistry(registry_path.with_suffix(".models"))
    model = train_alpha(request, store=store, registry=registry, label_rows=labels)
    raw = {
        "kind": "predict_alpha", "model_set_id": model.model_set_id,
        "feature_set_id": feature_set.dataset_id,
        "start": split.validation_end.isoformat(), "end": (split.validation_end + timedelta(days=10)).isoformat(),
    }
    payload = ALPHA_REQUEST_ADAPTER.validate_python(raw).model_dump(mode="json")
    with JobStore(registry_path) as jobs:
        jobs.connection.execute(
            "INSERT INTO jobs(id,status,request,config_hash) VALUES (?, 'queued', ?, ?)",
            ("alpha_blocked_prediction", json.dumps(payload), digest(payload)),
        )
        jobs.connection.commit()
        observed = []
        original = DatasetStore.read_rows
        monkeypatch.setattr(DatasetStore, "read_rows", lambda self, *args, **kwargs: observed.append(args) or original(self, *args, **kwargs))
        assert execute_job(jobs, jobs.claim()) is True
        failed = jobs.get_job("alpha_blocked_prediction")
    assert failed["status"] == "failed"
    assert failed["error_code"] == "RESEARCH_PREDICTION_REQUIRES_FREEZE"
    assert observed == []
