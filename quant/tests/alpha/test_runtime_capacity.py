from __future__ import annotations

import json

import pytest

from stock_quant.alpha.errors import AlphaError, AlphaErrorCode
from stock_quant.alpha.models.lightgbm_trainer import _model_fit_deadline_callback
from stock_quant.alpha.research.protocol import canonical_digest, canonical_request_payload
from stock_quant.alpha.runtime_budget import (
    HARD_TERMINATION_GRACE_SECONDS,
    PUBLICATION_SECONDS,
    TRAINING_DEFAULT_SECONDS,
    TRAINING_MAX_SECONDS,
    TRAINING_RUNTIME_CONTRACT,
    AttemptDeadline,
    effective_training_seconds,
    hard_attempt_seconds,
    normalize_training_request,
)
from stock_quant.compiler import digest
from stock_quant.jobs import JobStore


def _request(**overrides):
    payload = {
        "kind": "train_alpha_model",
        "feature_set_id": "sha256:" + "1" * 64,
        "resource_budget": {"max_rows": 25_000_000},
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (_request(), 120),
        (_request(max_seconds=300), 300),
        (_request(resource_budget={"max_seconds": 700, "max_rows": 1}), 700),
        (_request(max_seconds=900, resource_budget={"max_seconds": 900}), 900),
    ],
)
def test_new_training_budget_normalization(raw, expected):
    normalized = normalize_training_request(raw)
    assert normalized["max_seconds"] == expected
    assert normalized["resource_budget"]["max_seconds"] == expected
    assert normalized["resource_budget"].get("max_rows", 25_000_000) == raw["resource_budget"].get("max_rows", 25_000_000)
    assert normalized["budget_contract_version"] == TRAINING_RUNTIME_CONTRACT


def test_budget_normalization_rejects_conflict_and_platform_overflow():
    with pytest.raises(ValueError, match="conflicting max_seconds"):
        normalize_training_request(_request(max_seconds=30, resource_budget={"max_seconds": 60}))
    for raw in (
        _request(max_seconds=TRAINING_MAX_SECONDS + 1),
        _request(resource_budget={"max_seconds": TRAINING_MAX_SECONDS + 1}),
        _request(max_seconds=0),
        _request(max_seconds=True),
    ):
        with pytest.raises(ValueError, match="must be an integer"):
            normalize_training_request(raw)


def test_budget_normalization_does_not_coerce_invalid_resource_budget_shape():
    raw = _request(resource_budget=["not", "an", "object"])
    assert normalize_training_request(raw) == raw


def test_budget_normalization_preserves_scientific_request_identity():
    left = _request(max_seconds=120, resource_budget={"max_seconds": 120, "max_rows": 10})
    right = _request(max_seconds=1800, resource_budget={"max_seconds": 1800, "max_rows": 10})
    assert canonical_request_payload(left) == canonical_request_payload(right)
    assert canonical_digest(canonical_request_payload(left)) == canonical_digest(canonical_request_payload(right))
    assert canonical_request_payload(normalize_training_request(left)) == canonical_request_payload(left)


def test_legacy_budget_semantics_and_new_absolute_attempt_cap():
    legacy = _request(max_seconds=120, resource_budget={"max_seconds": 1800, "max_rows": 1})
    assert effective_training_seconds(legacy) == TRAINING_DEFAULT_SECONDS
    assert hard_attempt_seconds(legacy) == TRAINING_DEFAULT_SECONDS + HARD_TERMINATION_GRACE_SECONDS
    current = normalize_training_request(_request(resource_budget={"max_seconds": 1800, "max_rows": 1}))
    assert effective_training_seconds(current) == TRAINING_MAX_SECONDS
    assert hard_attempt_seconds(current) == TRAINING_MAX_SECONDS + PUBLICATION_SECONDS + HARD_TERMINATION_GRACE_SECONDS


def test_attempt_deadline_reports_phase_stage_and_preserves_publish_budget():
    now = [100.0]
    deadline = AttemptDeadline(started=100.0, compute_seconds=10, clock=lambda: now[0])
    now[0] = 109.0
    deadline.enter("training_join")
    now[0] = 109.5
    deadline.begin_publish()
    now[0] = 139.6
    with pytest.raises(AlphaError) as raised:
        deadline.check_publish("publish_serialization")
    assert raised.value.code == AlphaErrorCode.RESOURCE_LIMIT_EXCEEDED
    assert raised.value.context["stage"] == "publish_serialization"
    assert raised.value.context["phase"] == "publication"
    assert raised.value.context["effective_budget_seconds"] == PUBLICATION_SECONDS


def test_lightgbm_iteration_callback_uses_shared_deadline():
    now = [10.0]
    deadline = AttemptDeadline(started=10.0, compute_seconds=5, clock=lambda: now[0])
    callback = _model_fit_deadline_callback(deadline)
    callback({"iteration": 0})
    now[0] = 16.0
    with pytest.raises(AlphaError) as raised:
        callback({"iteration": 1})
    assert raised.value.context["stage"] == "model_fit"
    assert raised.value.context["effective_budget_seconds"] == 5


def _claim(store, job_id="runtime-publish"):
    with store.connection:
        store.connection.execute(
            "INSERT INTO jobs(id,status,request,config_hash) VALUES (?,'queued','{}',?)",
            (job_id, digest(job_id)),
        )
    claim = store.claim()
    assert claim is not None
    return claim


def test_publication_deadline_fails_claim_without_partial_catalog(tmp_path):
    with JobStore(tmp_path / "registry.sqlite3") as store:
        claim = _claim(store)
        now = [100.0]
        deadline = AttemptDeadline(started=0.0, compute_seconds=100, clock=lambda: now[0])
        deadline.begin_publish()
        now[0] = 131.0
        assert store.begin_publish(
            claim, {"training_metrics": [{"status": "ok"}]},
            checkpoint=lambda: deadline.check_publish("publish_serialization"),
        ) is False
        job = store.get_job(claim["id"])
        assert job["status"] == "failed"
        assert job["error_code"] == "RESOURCE_LIMIT_EXCEEDED"
        assert "publish_serialization" in job["error_message"]
        assert store.connection.execute(
            "SELECT count(*) FROM result_artifacts WHERE job_id=?", (claim["id"],),
        ).fetchone() == (0,)


def test_job_submission_normalizes_new_training_budget_and_keeps_config_integrity(tmp_path):
    with JobStore(tmp_path / "registry.sqlite3") as store:
        raw = {
            **_request(resource_budget={"max_seconds": 600, "max_rows": 100}),
            "label_name": "stock_forward_return", "target_type": "stock_return",
            "stock_dataset_id": "sha256:" + "2" * 64, "horizon_days": 5,
            "evaluation_mode": "validation_only", "research_protocol_hash": "sha256:" + "3" * 64,
            "split": {
                "train_start": "2024-01-01T00:00:00Z", "train_end": "2024-02-01T00:00:00Z",
                "validation_start": "2024-02-01T00:00:00Z", "validation_end": "2024-03-01T00:00:00Z",
            },
        }
        submitted = store.submit_alpha(raw)
        row = store.connection.execute(
            "SELECT request,config_hash FROM jobs WHERE id=?", (submitted["job_id"],),
        ).fetchone()
        persisted = json.loads(row[0])
        assert persisted["max_seconds"] == persisted["resource_budget"]["max_seconds"] == 600
        assert persisted["budget_contract_version"] == TRAINING_RUNTIME_CONTRACT
        assert digest(persisted) == row[1]
