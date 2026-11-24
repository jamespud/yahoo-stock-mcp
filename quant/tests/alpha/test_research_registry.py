from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from stock_quant.alpha.data.catalog import DatasetStore
from stock_quant.alpha.errors import AlphaError, AlphaErrorCode
from stock_quant.alpha.models.registry import AlphaModelRegistry
from stock_quant.alpha.models.research_freeze import (
    FrozenResearchRecord,
    FrozenResearchStore,
    freeze_identity,
)
from stock_quant.alpha.research_admission import authorize_oos
from stock_quant.compiler import digest
from stock_quant.jobs import JobStore, execute_job


def _campaign(**overrides):
    payload = {
        "campaign_name": "Synthetic short reversal",
        "research_objective": "Test whether governed evidence can reject selection bias.",
        "research_scope": {"synthetic": True},
        "development_data_scope": {
            "asset_class": "equity",
            "market": "US",
            "universe_definition": {"symbols": ["AAA", "BBB"]},
            "development_start": "2024-01-01T00:00:00Z",
            "development_end": "2024-03-01T00:00:00Z",
            "data_provider": "synthetic",
            "stock_dataset_ids": ["sha256:" + "1" * 64],
            "feature_dataset_ids": ["sha256:" + "2" * 64],
            "label_definitions": [{"label_name": "stock_forward_return", "horizon_days": 5}],
        },
        "resource_policy": {
            "max_registered_trials": 4,
            "max_submitted_trials": 4,
            "max_distinct_candidate_definitions": 4,
            "max_evaluation_jobs": 4,
            "max_execution_attempts": 8,
            "max_protocol_revisions": 2,
        },
    }
    payload.update(overrides)
    return payload


def _protocol(campaign_id, **overrides):
    payload = {
        "campaign_id": campaign_id,
        "hypothesis_definitions": [{
            "hypothesis_key": "short_reversal",
            "title": "Five-day short reversal",
            "economic_rationale": "Short-horizon reversals may follow liquidity shocks.",
            "expected_direction": "positive",
            "target_variable": "stock_forward_return",
            "factor_spec_hash": None,
            "primary_metric": "rank_ic",
            "null_hypothesis": "The candidate has no positive rank IC.",
            "comparison_family": ["lightgbm_vs_ridge"],
        }],
        "candidate_definitions": [{"seed": seed} for seed in (7, 8, 9, 10, 11)],
        "allowed_job_kinds": ["train_alpha_model", "walk_forward_alpha"],
        "data_constraints": {
            "feature_set_ids": ["sha256:" + "2" * 64],
            "stock_dataset_ids": ["sha256:" + "1" * 64],
        },
        "label_specifications": [{"label_name": "stock_forward_return", "target_type": "stock_return", "horizon_days": 5}],
        "selection_policy": {
            "primary_metric": "rank_ic",
            "minimum_rank_ic": 0.0,
            "minimum_label_coverage": 0.0,
            "allowed_research_status": ["RESEARCH_DATA_VALIDATED", "RESEARCH_EXPLORATORY"],
        },
    }
    payload.update(overrides)
    return payload


def _training_request(**overrides):
    payload = {
        "kind": "train_alpha_model",
        "feature_set_id": "sha256:" + "2" * 64,
        "label_name": "stock_forward_return",
        "target_type": "stock_return",
        "stock_dataset_id": "sha256:" + "1" * 64,
        "horizon_days": 5,
        "evaluation_mode": "validation_only",
        "research_protocol_hash": "sha256:" + "0" * 64,
        "split": {
            "train_start": "2024-01-01T00:00:00Z",
            "train_end": "2024-02-01T00:00:00Z",
            "validation_start": "2024-02-01T00:00:00Z",
            "validation_end": "2024-03-01T00:00:00Z",
        },
        "hyperparameters": {"n_estimators": 10},
        "random_seed": 7,
        "min_rank_ic": 0.0,
        "resource_budget": {"max_seconds": 30, "max_rows": 1000},
    }
    payload.update(overrides)
    return payload


def _setup_registry(tmp_path):
    jobs = JobStore(tmp_path / "registry.sqlite3")
    registry = jobs.research_registry
    campaign = registry.register_campaign(_campaign())
    protocol = registry.register_protocol(_protocol(campaign.campaign_id))
    return jobs, registry, campaign, protocol


def _register_trial(registry, protocol, *, seed=7, purpose="INITIAL_HYPOTHESIS_TEST"):
    request = _training_request(research_protocol_hash=protocol.protocol_hash, random_seed=seed)
    trial = registry.register_trial(
        campaign_id=protocol.definition.campaign_id,
        protocol_hash=protocol.protocol_hash,
        hypothesis_id=protocol.registered_hypotheses[0].hypothesis_id,
        request=request,
        trial_purpose=purpose,
    )
    return request, trial


def _publish_training(jobs, request, trial, *, rank_ic=0.1, model_text="tree"):
    model_registry = AlphaModelRegistry(Path(jobs.path).with_suffix(".models"))
    model = model_registry.save(model_text=model_text, metadata={
        "feature_set_id": request["feature_set_id"],
        "label_definition_hash": "sha256:" + "3" * 64,
        "label_spec": {"label_name": "stock_forward_return"},
        "label_source_dataset_id": request["stock_dataset_id"],
        "factor_spec_hash": "sha256:" + "4" * 64,
        "calendar_version": "synthetic",
        "training_window": request["split"],
        "validation_window": request["split"],
        "evaluation_mode": "validation_only",
        "research_protocol_hash": request["research_protocol_hash"],
        "selected_iteration": 1,
        "hyperparameters": {},
        "feature_schema": ["value"],
        "feature_schema_hash": "sha256:" + "5" * 64,
        "environment_hash": "sha256:" + "6" * 64,
        "random_seed": request["random_seed"],
        "feature_importance": {"value": 1.0},
        "train_metrics": {},
        "validation_metrics": {},
        "validation_gate_passed": True,
    })
    submission = jobs.submit_alpha({**request, "research_trial_id": trial.trial_id, "idempotency_key": f"submit:{trial.trial_id}"})
    claim = jobs.claim()
    assert claim is not None
    result = {
        "summary": {"status": "COMPLETED", "model_id": model["model_id"]},
        "dataset": {"section": "dataset", "model_id": model["model_id"], "model_set_id": model["model_set_id"]},
        "training_metrics": [{
            "validation": {"rank_ic_mean": rank_ic, "prediction_coverage": 0.9},
            "baseline_comparison": None,
        }],
        "model_registry": [model],
        "diagnostics": [{"status": "OK"}],
        "reproducibility": {"model_hash": model["model_hash"]},
    }
    assert jobs.begin_publish(claim, result)
    assert jobs.complete_publish(claim)
    return submission, model


def test_campaign_and_protocol_identities_are_server_generated_and_deterministic(tmp_path):
    jobs, registry, campaign, protocol = _setup_registry(tmp_path)
    try:
        with pytest.raises(ValueError):
            registry.register_protocol({**_protocol(campaign.campaign_id), "protocol_hash": "sha256:" + "f" * 64})
        assert campaign.campaign_id == digest(campaign.definition.model_dump(mode="json"))
        assert protocol.protocol_hash.startswith("sha256:")
        assert registry.get_protocol(protocol.protocol_hash).protocol_hash == protocol.protocol_hash
        assert registry.verify_ledger(campaign.campaign_id).status == "VERIFIED"
    finally:
        jobs.connection.close()


def test_protocol_revision_requires_parent_and_cannot_increase_campaign_budget(tmp_path):
    jobs, registry, campaign, protocol = _setup_registry(tmp_path)
    try:
        revised = _protocol(
            campaign.campaign_id,
            protocol_revision=2,
            parent_protocol_hash=protocol.protocol_hash,
            modification_reason="Changed the predeclared seed range.",
        )
        registered = registry.register_protocol(revised)
        assert registered.definition.protocol_revision == 2

        increased = _protocol(
            campaign.campaign_id,
            protocol_revision=3,
            parent_protocol_hash=registered.protocol_hash,
            modification_reason="Attempt to increase budget.",
        )
        increased["trial_budget"] = {
            "max_registered_trials": 10,
            "max_submitted_trials": 10,
            "max_distinct_candidate_definitions": 10,
            "max_evaluation_jobs": 10,
            "max_execution_attempts": 20,
            "max_protocol_revisions": 2,
        }
        with pytest.raises(AlphaError) as error:
            registry.register_protocol(increased)
        assert error.value.code == AlphaErrorCode.RESEARCH_PROTOCOL_BUDGET_EXCEEDED
    finally:
        jobs.connection.close()


def test_trial_registration_is_canonical_and_idempotent_submission_does_not_duplicate_count(tmp_path):
    jobs, registry, campaign, protocol = _setup_registry(tmp_path)
    try:
        request, trial = _register_trial(registry, protocol)
        first = jobs.submit_alpha({**request, "research_trial_id": trial.trial_id, "idempotency_key": "same"})
        second = jobs.submit_alpha({**request, "research_trial_id": trial.trial_id, "idempotency_key": "same"})
        assert first["job_id"] == second["job_id"]
        accounting = registry.trial_accounting(campaign.campaign_id)
        assert accounting.registered_trial_count == 1
        assert accounting.submitted_trial_count == 1
        assert accounting.distinct_trial_definition_count == 1
        events = [event.event_type for event in registry.list_events(campaign.campaign_id)]
        assert events.count("TRIAL_SUBMITTED") == 1
    finally:
        jobs.connection.close()


def test_registered_protocol_cannot_be_downgraded_by_omitting_trial_id(tmp_path):
    jobs, registry, _campaign, protocol = _setup_registry(tmp_path)
    try:
        request, _trial = _register_trial(registry, protocol)
        with pytest.raises(AlphaError) as error:
            jobs.submit_alpha(request)
        assert error.value.code == AlphaErrorCode.RESEARCH_TRIAL_NOT_REGISTERED
        assert jobs.connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
    finally:
        jobs.connection.close()


def test_request_mismatch_and_registration_budget_fail_closed(tmp_path):
    jobs, registry, _campaign, protocol = _setup_registry(tmp_path)
    try:
        request, trial = _register_trial(registry, protocol)
        with pytest.raises(AlphaError) as error:
            jobs.submit_alpha({**request, "random_seed": 8, "research_trial_id": trial.trial_id})
        assert error.value.code == AlphaErrorCode.RESEARCH_TRIAL_REQUEST_MISMATCH
        for seed in (8, 9, 10):
            _register_trial(registry, protocol, seed=seed, purpose="PARAMETER_VARIATION")
        with pytest.raises(AlphaError) as budget:
            _register_trial(registry, protocol, seed=11, purpose="PARAMETER_VARIATION")
        assert budget.value.code == AlphaErrorCode.RESEARCH_TRIAL_BUDGET_EXCEEDED
    finally:
        jobs.connection.close()


def test_lifecycle_event_chain_snapshot_selection_and_deterministic_report(tmp_path):
    jobs, registry, campaign, protocol = _setup_registry(tmp_path)
    try:
        request, trial = _register_trial(registry, protocol)
        submission, model = _publish_training(jobs, request, trial, rank_ic=0.12)
        events = [event.event_type for event in registry.list_events(campaign.campaign_id)]
        assert events[-3:] == [
            "TRIAL_EXECUTION_STARTED", "TRIAL_PUBLISHING", "TRIAL_COMPLETED",
        ]
        snapshot, selection = registry.seal_candidate_ledger(
            campaign_id=campaign.campaign_id,
            protocol_hash=protocol.protocol_hash,
        )
        assert snapshot.registered_trials[0].result_hash == jobs.connection.execute(
            "SELECT result_hash FROM jobs WHERE id=?", (submission["job_id"],)
        ).fetchone()[0]
        assert selection.selection_status == "CANDIDATE_SELECTED_FOR_FURTHER_RESEARCH"
        assert selection.selected_model_id == model["model_id"]
        events = [event.event_type for event in registry.list_events(campaign.campaign_id)]
        assert events[-2:] == ["LEDGER_SEALED", "SELECTION_REGISTERED"]
        report1 = registry.build_evidence_report(
            campaign_id=campaign.campaign_id, candidate_ledger_hash=snapshot.candidate_ledger_hash,
        )
        report2 = registry.build_evidence_report(
            campaign_id=campaign.campaign_id, candidate_ledger_hash=snapshot.candidate_ledger_hash,
        )
        assert report1.report_hash == report2.report_hash
        assert report1.trial_accounting.completed_evaluation_count == 1
        assert report1.scope_completeness.global_trial_history_complete is False
        assert report1.research_verdict == "RESEARCH_EVIDENCE_INTEGRITY_VERIFIED"
        repeated_snapshot, repeated_selection = registry.seal_candidate_ledger(
            campaign_id=campaign.campaign_id, protocol_hash=protocol.protocol_hash,
        )
        assert repeated_snapshot.candidate_ledger_hash == snapshot.candidate_ledger_hash
        assert repeated_selection.selection_hash == selection.selection_hash
        seal_events = [event.event_type for event in registry.list_events(campaign.campaign_id)]
        assert seal_events.count("LEDGER_SEALED") == 1
        assert seal_events.count("SELECTION_REGISTERED") == 1
    finally:
        jobs.connection.close()


def test_failed_trial_is_retained_and_selection_can_return_no_candidate(tmp_path):
    jobs, registry, campaign, protocol = _setup_registry(tmp_path)
    try:
        request, trial = _register_trial(registry, protocol)
        jobs.submit_alpha({**request, "research_trial_id": trial.trial_id})
        claim = jobs.claim()
        assert claim is not None
        assert jobs.finish(claim, error=RuntimeError("synthetic failure"), error_code="DATA_UNAVAILABLE")
        snapshot, selection = registry.seal_candidate_ledger(
            campaign_id=campaign.campaign_id, protocol_hash=protocol.protocol_hash,
        )
        assert snapshot.registered_trials[0].terminal_state == "FAILED"
        assert snapshot.excluded_candidates[0]["reason"] == "JOB_FAILED"
        assert selection.selection_status == "NO_ELIGIBLE_CANDIDATE"
        assert selection.selected_model_id is None
        assert registry.trial_accounting(campaign.campaign_id).failed_trial_count == 1
    finally:
        jobs.connection.close()


def test_ledger_hash_chain_detects_tampering(tmp_path):
    jobs, registry, campaign, protocol = _setup_registry(tmp_path)
    try:
        _register_trial(registry, protocol)
        assert registry.verify_ledger(campaign.campaign_id).status == "VERIFIED"
        jobs.connection.execute(
            "UPDATE alpha_research_events SET detail=? WHERE campaign_id=? AND event_type='TRIAL_REGISTERED'",
            (json.dumps({"tampered": True}), campaign.campaign_id),
        )
        jobs.connection.commit()
        integrity = registry.verify_ledger(campaign.campaign_id)
        assert integrity.status == "FAILED"
        assert "event hash mismatch" in integrity.failure_reason
    finally:
        jobs.connection.close()


def test_result_access_is_tracked_without_paginated_access_inflating_result_count(tmp_path):
    jobs, registry, campaign, protocol = _setup_registry(tmp_path)
    try:
        request, trial = _register_trial(registry, protocol)
        submission, _ = _publish_training(jobs, request, trial)
        jobs.get_result(submission["job_id"], "summary")
        jobs.get_result(submission["job_id"], "dataset")
        accounting = registry.trial_accounting(campaign.campaign_id)
        assert accounting.result_exposure_count == 1
        assert accounting.result_access_request_count == 2
        exposure_events = [
            event for event in registry.list_events(campaign.campaign_id)
            if event.event_type == "TRIAL_RESULT_ACCESSED"
        ]
        assert len(exposure_events) == 2
    finally:
        jobs.connection.close()


def test_trial_definition_identity_expands_defaults_and_changes_with_scientific_parameters():
    from stock_quant.alpha.research.protocol import trial_identities

    minimal = _training_request()
    explicit = _training_request(
        horizon_days=5,
        random_seed=7,
        hyperparameters={"n_estimators": 10},
        resource_budget={"max_seconds": 30, "max_rows": 1000},
        min_rank_ic=0.0,
    )
    assert trial_identities(minimal)[2] == trial_identities(explicit)[2]
    assert trial_identities(minimal)[2] != trial_identities(_training_request(horizon_days=10))[2]
    assert trial_identities(minimal)[2] != trial_identities(_training_request(random_seed=8))[2]
    operational_only = _training_request(max_seconds=99, resource_budget={"max_seconds": 99, "max_rows": 2000})
    assert trial_identities(minimal)[2] == trial_identities(operational_only)[2]
    assert trial_identities(minimal)[1] != trial_identities(operational_only)[1]
    utc_request = _training_request()
    offset_request = _training_request(split={
        "train_start": "2024-01-01T08:00:00+08:00",
        "train_end": "2024-02-01T08:00:00+08:00",
        "validation_start": "2024-02-01T08:00:00+08:00",
        "validation_end": "2024-03-01T08:00:00+08:00",
    })
    from stock_quant.alpha.jobs.requests import ALPHA_REQUEST_ADAPTER
    utc_payload = ALPHA_REQUEST_ADAPTER.validate_python(utc_request).model_dump(mode="json")
    offset_payload = ALPHA_REQUEST_ADAPTER.validate_python(offset_request).model_dump(mode="json")
    assert trial_identities(utc_payload)[2] == trial_identities(offset_payload)[2]


def test_worker_lease_loss_and_retry_are_counted_without_duplicate_scientific_trial(tmp_path):
    jobs, registry, campaign, protocol = _setup_registry(tmp_path)
    try:
        request, trial = _register_trial(registry, protocol)
        _submission = jobs.submit_alpha({**request, "research_trial_id": trial.trial_id})
        first_claim = jobs.claim()
        assert first_claim is not None
        jobs.connection.execute("UPDATE jobs SET lease_until=0 WHERE id=?", (first_claim["id"],))
        jobs.connection.commit()
        second_claim = jobs.claim()
        assert second_claim is not None
        assert jobs.begin_publish(second_claim, {"summary": {"status": "completed"}})
        assert jobs.complete_publish(second_claim)
        events = [event.event_type for event in registry.list_events(campaign.campaign_id)]
        assert events.count("TRIAL_EXECUTION_STARTED") == 1
        assert events.count("TRIAL_WORKER_LOST") == 1
        assert events.count("TRIAL_RETRY_STARTED") == 1
        accounting = registry.trial_accounting(campaign.campaign_id)
        assert accounting.registered_trial_count == 1
        assert accounting.execution_attempt_count == 2
        assert accounting.completed_evaluation_count == 1
    finally:
        jobs.connection.close()


def test_cross_campaign_exact_dataset_reuse_is_reported_as_not_independent(tmp_path):
    jobs, registry, campaign, protocol = _setup_registry(tmp_path)
    try:
        request, trial = _register_trial(registry, protocol)
        _publish_training(jobs, request, trial)
        snapshot, _selection = registry.seal_candidate_ledger(
            campaign_id=campaign.campaign_id, protocol_hash=protocol.protocol_hash,
        )
        before = registry.build_evidence_report(
            campaign_id=campaign.campaign_id,
            candidate_ledger_hash=snapshot.candidate_ledger_hash,
        )
        second_campaign = registry.register_campaign(_campaign(campaign_name="Renamed but reused"))
        after = registry.build_evidence_report(
            campaign_id=campaign.campaign_id,
            candidate_ledger_hash=snapshot.candidate_ledger_hash,
        )
        assert after.report_hash == before.report_hash
        exact = [
            finding for finding in registry._data_reuse(campaign)
            if finding.status == "EXACT_EVIDENCE_REUSE"
            and second_campaign.campaign_id in finding.campaign_ids
        ]
        assert exact
        assert after.scope_completeness.global_trial_history_complete is False
    finally:
        jobs.connection.close()


def test_managed_freeze_requires_real_selection_and_rejects_fake_or_stale_ledgers(tmp_path):
    jobs, registry, campaign, protocol = _setup_registry(tmp_path)
    test_ids = {"feature": "sha256:" + "8" * 64, "label": "sha256:" + "9" * 64}
    try:
        request, trial = _register_trial(registry, protocol)
        _submission, model = _publish_training(jobs, request, trial)
        snapshot, selection = registry.seal_candidate_ledger(
            campaign_id=campaign.campaign_id, protocol_hash=protocol.protocol_hash,
        )
        with pytest.raises(ValueError, match="RESEARCH_EVIDENCE_HASH_MISMATCH") as fake:
            jobs.submit_alpha({
                "kind": "freeze_alpha_selection",
                "research_protocol_hash": protocol.protocol_hash,
                "candidate_ledger_hash": "sha256:" + "a" * 64,
                "selected_model_ids": [selection.selected_model_id],
                "test_feature_set_id": test_ids["feature"],
                "test_label_source_dataset_id": test_ids["label"],
                "test_start": "2024-03-01T00:00:00Z",
                "test_end": "2024-04-01T00:00:00Z",
                "selection_reason": "registered selection",
                "label_name": "stock_forward_return",
                "target_type": "stock_return",
                "horizon_days": 5,
            })
        assert "RESEARCH_EVIDENCE_HASH_MISMATCH" in str(fake.value)
        managed_freeze = jobs.submit_alpha({
            "kind": "freeze_alpha_selection",
            "research_protocol_hash": protocol.protocol_hash,
            "candidate_ledger_hash": snapshot.candidate_ledger_hash,
            "selected_model_ids": [selection.selected_model_id],
            "test_feature_set_id": test_ids["feature"],
            "test_label_source_dataset_id": test_ids["label"],
            "test_start": "2024-03-01T00:00:00Z",
            "test_end": "2024-04-01T00:00:00Z",
            "selection_reason": "registered selection",
            "label_name": "stock_forward_return",
            "target_type": "stock_return",
            "horizon_days": 5,
        })
        assert managed_freeze["status"] == "queued"

        second_request, second_trial = _register_trial(
            registry, protocol, seed=8, purpose="REPRODUCIBILITY_CHECK",
        )
        jobs.submit_alpha({**second_request, "research_trial_id": second_trial.trial_id})
        with pytest.raises(AlphaError) as stale:
            registry.verify_managed_freeze(
                protocol_hash=protocol.protocol_hash,
                candidate_ledger_hash=snapshot.candidate_ledger_hash,
                selected_model_id=model["model_id"],
            )
        assert stale.value.code == AlphaErrorCode.RESEARCH_LEDGER_STALE
    finally:
        jobs.connection.close()


def test_rejected_managed_request_is_audited_against_the_registered_trial(tmp_path):
    jobs, registry, campaign, protocol = _setup_registry(tmp_path)
    try:
        request, trial = _register_trial(registry, protocol)
        with pytest.raises(ValueError, match="RESEARCH_MODE_REQUIRED"):
            jobs.submit_alpha({**request, "research_trial_id": trial.trial_id, "evaluation_mode": None})
        assert registry.get_trial(trial.trial_id).status == "REJECTED"
        events = [event.event_type for event in registry.list_events(campaign.campaign_id)]
        assert "TRIAL_REJECTED" in events
        assert registry.trial_accounting(campaign.campaign_id).rejected_submission_count == 1
    finally:
        jobs.connection.close()


def test_queued_or_running_cancellation_remains_in_ledger(tmp_path):
    jobs, registry, campaign, protocol = _setup_registry(tmp_path)
    try:
        request, trial = _register_trial(registry, protocol)
        submission = jobs.submit_alpha({**request, "research_trial_id": trial.trial_id})
        cancelled = jobs.cancel(submission["job_id"])
        assert cancelled["status"] == "cancelled"
        assert registry.get_trial(trial.trial_id).status == "CANCELLED"
        events = [event.event_type for event in registry.list_events(campaign.campaign_id)]
        assert "TRIAL_CANCELLED" in events
        assert registry.trial_accounting(campaign.campaign_id).cancelled_trial_count == 1
    finally:
        jobs.connection.close()


def test_concurrent_trial_registration_cannot_break_campaign_budget(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    path = tmp_path / "registry.sqlite3"
    with JobStore(path) as setup:
        campaign = setup.research_registry.register_campaign(_campaign(resource_policy={
            "max_registered_trials": 1,
            "max_submitted_trials": 1,
            "max_distinct_candidate_definitions": 1,
            "max_evaluation_jobs": 1,
            "max_execution_attempts": 2,
            "max_protocol_revisions": 1,
        }))
        protocol = setup.research_registry.register_protocol(_protocol(campaign.campaign_id))
        campaign_id = campaign.campaign_id
        protocol_hash = protocol.protocol_hash
        hypothesis_id = protocol.registered_hypotheses[0].hypothesis_id
    barrier = Barrier(2)

    def register(seed):
        with JobStore(path) as jobs:
            barrier.wait()
            try:
                jobs.research_registry.register_trial(
                    campaign_id=campaign_id,
                    protocol_hash=protocol_hash,
                    hypothesis_id=hypothesis_id,
                    request=_training_request(research_protocol_hash=protocol_hash, random_seed=seed),
                    trial_purpose="PARAMETER_VARIATION",
                )
                return "registered"
            except AlphaError as error:
                return error.code.value

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(register, (8, 9)))
    assert outcomes.count("registered") == 1
    assert outcomes.count("RESEARCH_TRIAL_BUDGET_EXCEEDED") == 1
    with JobStore(path) as jobs:
        assert jobs.connection.execute(
            "SELECT count(*) FROM alpha_research_trials WHERE campaign_id=?", (campaign_id,)
        ).fetchone()[0] == 1


def test_invalid_baseline_report_hash_cannot_become_selected_evidence(tmp_path):
    jobs, registry, campaign, protocol = _setup_registry(tmp_path)
    try:
        request, trial = _register_trial(registry, protocol)
        model_registry = AlphaModelRegistry(Path(jobs.path).with_suffix(".models"))
        model = model_registry.save(model_text="tree", metadata={
            "feature_set_id": request["feature_set_id"], "label_definition_hash": "sha256:" + "3" * 64,
            "label_spec": {"label_name": "stock_forward_return"}, "label_source_dataset_id": request["stock_dataset_id"],
            "factor_spec_hash": "sha256:" + "4" * 64, "calendar_version": "synthetic",
            "training_window": request["split"], "validation_window": request["split"],
            "evaluation_mode": "validation_only", "research_protocol_hash": request["research_protocol_hash"],
            "selected_iteration": 1, "hyperparameters": {}, "feature_schema": ["value"],
            "feature_schema_hash": "sha256:" + "5" * 64, "environment_hash": "sha256:" + "6" * 64,
            "random_seed": request["random_seed"], "feature_importance": {"value": 1.0},
            "train_metrics": {}, "validation_metrics": {}, "validation_gate_passed": True,
        })
        jobs.submit_alpha({**request, "research_trial_id": trial.trial_id})
        claim = jobs.claim()
        fake_baseline = {
            "schema_version": "alpha-baseline-comparison-v1",
            "comparison_protocol_hash": "sha256:" + "a" * 64,
            "research_protocol_hash": request["research_protocol_hash"],
            "feature_set_id": request["feature_set_id"],
            "label_source_dataset_id": request["stock_dataset_id"],
            "label_definition_hash": "sha256:" + "b" * 64,
            "label_definition": {},
            "factor_spec_hash": "sha256:" + "c" * 64,
            "development_window": {},
            "cohort_identity": {
                "train_identity_hash": "sha256:" + "d" * 64,
                "validation_identity_hash": "sha256:" + "e" * 64,
            },
            "data_quality": {},
            "common_cohort_coverage_by_decision": [],
            "omitted_common_cohort_decisions": 0,
            "metric_interpretation": "fake",
            "candidates": [],
            "paired_comparisons": [],
            "exclusions": {},
            "reproducibility": {},
            "comparison_status": "COMPARISON_COMPLETED",
            "report_hash": "sha256:" + "f" * 64,
        }
        assert jobs.begin_publish(claim, {
            "summary": {"status": "completed"},
            "dataset": {"section": "dataset", "model_id": model["model_id"], "model_set_id": model["model_set_id"]},
            "training_metrics": [{
                "validation": {"rank_ic_mean": 0.5, "prediction_coverage": 0.99},
                "baseline_comparison": fake_baseline,
            }],
            "model_registry": [model],
            "diagnostics": [{"status": "OK"}],
            "reproducibility": {"model_hash": model["model_hash"]},
        })
        assert jobs.complete_publish(claim)
        snapshot, selection = registry.seal_candidate_ledger(
            campaign_id=campaign.campaign_id, protocol_hash=protocol.protocol_hash,
        )
        assert snapshot.excluded_candidates[0]["reason"] == "BASELINE_EVIDENCE_HASH_MISMATCH"
        assert selection.selection_status == "NO_ELIGIBLE_CANDIDATE"
    finally:
        jobs.connection.close()


def _factor_spec(period: int = 5) -> dict:
    return {
        "schema_version": "1.0",
        "timeframe": "1d",
        "nodes": [
            {"id": "ret", "operator": "pct_change", "inputs": ["close"], "params": {"period": period}},
            {"id": "rsi", "operator": "rsi", "inputs": ["close"], "params": {"period": 5}},
        ],
        "outputs": ["ret", "rsi"],
    }


def _baseline_config(ridge_alpha: float = 1.0, period: int = 5) -> dict:
    return {
        "schema_version": "1.0",
        "factor_spec": _factor_spec(period),
        "reversal_output": "ret",
        "rsi_output": "rsi",
        "ridge_alpha": ridge_alpha,
    }


def _walk_forward_request(protocol_hash: str, mode: str = "expanding") -> dict:
    return {
        "kind": "walk_forward_alpha",
        "evaluation_mode": "validation_only",
        "research_protocol_hash": protocol_hash,
        "feature_set_id": "sha256:" + "2" * 64,
        "stock_dataset_id": "sha256:" + "1" * 64,
        "label_spec": {
            "label_name": "stock_forward_return", "target_type": "stock_return",
            "stock_dataset_id": "sha256:" + "1" * 64, "horizon_days": 5,
        },
        "development_window": {
            "development_start": "2024-01-02T00:00:00Z", "development_end": "2024-05-02T00:00:00Z",
        },
        "baseline_comparison": _baseline_config(),
        "lightgbm_hyperparameters": {"n_estimators": 10},
        "random_seed": 7,
        "walk_forward_config": {
            "schema_version": "1.0", "mode": mode, "train_sessions": 30,
            "validation_sessions": 10, "inner_validation_sessions": 5, "step_sessions": 10,
            "embargo_days": 0, "min_folds": 3, "max_folds": 12,
            "inference": {
                "bootstrap_method": "moving_block", "bootstrap_block_sessions": 5,
                "bootstrap_replicates": 25, "confidence_level": 0.95, "multiple_testing_method": "holm",
            },
        },
        "resource_budget": {
            "max_seconds": 120, "max_total_physical_rows": 50000, "max_total_training_samples": 50000,
            "max_partitions": 100, "max_lineage_nodes": 20, "max_bootstrap_replicates": 50,
        },
        "max_seconds": 120,
    }


def _factor_hash() -> str:
    from stock_quant.alpha.factors.ast import FactorSpec
    from stock_quant.alpha.factors.compiler import compile_factor_spec
    return compile_factor_spec(FactorSpec.model_validate(_factor_spec())).spec_hash


def _factor_spec_with_extra() -> dict:
    spec = _factor_spec()
    spec["nodes"].append({"id": "extra", "operator": "rolling_std", "inputs": ["ret"], "params": {"period": 5}})
    spec["outputs"].append("extra")
    return spec


def _freeze_request_for_selection(protocol, selection, model_id):
    return {
        "kind": "freeze_alpha_selection",
        "research_protocol_hash": protocol.protocol_hash,
        "candidate_ledger_hash": selection.candidate_ledger_hash,
        "selected_model_ids": [model_id],
        "test_feature_set_id": "sha256:" + "8" * 64,
        "test_label_source_dataset_id": "sha256:" + "9" * 64,
        "test_start": "2024-03-01T00:00:00Z",
        "test_end": "2024-04-01T00:00:00Z",
        "selection_reason": "registered selection",
        "label_name": "stock_forward_return",
        "target_type": "stock_return",
        "horizon_days": 5,
    }


def test_worker_rejects_stale_managed_freeze_before_reads_or_publication(tmp_path):
    jobs, registry, campaign, protocol = _setup_registry(tmp_path)
    try:
        request, trial = _register_trial(registry, protocol)
        _submission, model = _publish_training(jobs, request, trial)
        _snapshot, selection = registry.seal_candidate_ledger(
            campaign_id=campaign.campaign_id, protocol_hash=protocol.protocol_hash,
        )
        freeze_job = jobs.submit_alpha(_freeze_request_for_selection(protocol, selection, model["model_id"]))
        _register_trial(registry, protocol, seed=8, purpose="REPRODUCIBILITY_CHECK")
        claim = jobs.claim()
        assert claim is not None
        execute_job(jobs, claim)
        failed = jobs.get_job(freeze_job["job_id"])
        assert failed["status"] == "failed"
        assert failed["error_code"] == AlphaErrorCode.RESEARCH_LEDGER_STALE.value
        assert "RESEARCH_LEDGER_STALE" in failed["error_message"]
        assert not list(Path(jobs.path).with_suffix(".freezes").glob("*.json"))
    finally:
        jobs.connection.close()


def test_stale_managed_freeze_cannot_issue_oos_grant(tmp_path):
    jobs, registry, campaign, protocol = _setup_registry(tmp_path)
    try:
        request, trial = _register_trial(registry, protocol)
        _submission, model = _publish_training(jobs, request, trial)
        snapshot, selection = registry.seal_candidate_ledger(
            campaign_id=campaign.campaign_id, protocol_hash=protocol.protocol_hash,
        )
        freeze_request = _freeze_request_for_selection(protocol, selection, model["model_id"])
        jobs.submit_alpha(freeze_request)
        body = {
            "schema_version": "1.0",
            "research_protocol_hash": protocol.protocol_hash,
            "candidate_ledger_hash": snapshot.candidate_ledger_hash,
            "frozen_model_ids": [model["model_id"]],
            "model_hashes": {model["model_id"]: model["model_hash"]},
            "test_dataset_hashes": {
                "test_feature_set_id": freeze_request["test_feature_set_id"],
                "test_label_source_dataset_id": freeze_request["test_label_source_dataset_id"],
            },
            "test_dataset_ids": {
                "test_feature_set_id": freeze_request["test_feature_set_id"],
                "test_label_source_dataset_id": freeze_request["test_label_source_dataset_id"],
            },
            "test_window": {"start": freeze_request["test_start"], "end": freeze_request["test_end"]},
            "label_spec": {
                "label_name": freeze_request["label_name"], "target_type": freeze_request["target_type"],
                "horizon_days": freeze_request["horizon_days"],
                "label_source_dataset_id": freeze_request["test_label_source_dataset_id"],
                "label_version": "observed-record-v1",
            },
            "selection_reason": freeze_request["selection_reason"],
        }
        record = FrozenResearchRecord(
            **body,
            freeze_id=digest(body),
            frozen_at=datetime.now(UTC),
        )
        assert freeze_identity(record) == record.freeze_id
        FrozenResearchStore(Path(jobs.path).with_suffix(".freezes")).save(record)
        claim = jobs.claim()
        assert claim is not None
        assert jobs.begin_publish(claim, {
            "summary": record.model_dump(mode="json"),
            "reproducibility": {
                "freeze_id": record.freeze_id,
                "research_protocol_hash": protocol.protocol_hash,
                "candidate_ledger_hash": snapshot.candidate_ledger_hash,
            },
        })
        assert jobs.complete_publish(claim)
        _register_trial(registry, protocol, seed=8, purpose="REPRODUCIBILITY_CHECK")
        with pytest.raises(AlphaError) as error:
            authorize_oos(
                record.freeze_id,
                job_store=jobs,
                datasets=DatasetStore(Path(jobs.path).with_suffix(".alpha")),
                model_registry=AlphaModelRegistry(Path(jobs.path).with_suffix(".models")),
            )
        assert error.value.code == AlphaErrorCode.FROZEN_OOS_AUTHORIZATION_INVALID
        assert error.value.context["reason"] == AlphaErrorCode.RESEARCH_LEDGER_STALE.value
    finally:
        jobs.connection.close()


def test_training_label_horizon_factor_and_ridge_mutations_fail_closed(tmp_path):
    jobs, registry, campaign, _protocol_default = _setup_registry(tmp_path)
    try:
        factor = _factor_spec()
        protocol = registry.register_protocol(_protocol(
            campaign.campaign_id,
            protocol_revision=2,
            parent_protocol_hash=_protocol_default.protocol_hash,
            modification_reason="Constrain scientific request fields.",
            factor_specifications=[factor],
            hypothesis_definitions=[{
                ** _protocol(campaign.campaign_id)["hypothesis_definitions"][0],
                "factor_spec_hash": _factor_hash(),
                "candidate_constraints": {"random_seed": 7},
            }],
            candidate_definitions=[{
                "seed": 7,
                "model": "lightgbm_v1",
                "baseline_comparison": {
                    "factor_spec": factor,
                    "reversal_output": "ret",
                    "rsi_output": "rsi",
                    "ridge_alpha": 1.0,
                },
            }],
            evaluation_policy={"primary_metric": "rank_ic", "minimum_rank_ic": 0.0},
            selection_policy={
                "primary_metric": "rank_ic", "minimum_rank_ic": 0.0,
                "minimum_label_coverage": 0.0, "minimum_model_execution_coverage": 0.0,
                "minimum_end_to_end_coverage": 0.0,
            },
        ))
        base = _training_request(
            research_protocol_hash=protocol.protocol_hash,
            baseline_comparison=_baseline_config(),
        )
        for mutated, field in (
            ({**base, "horizon_days": 10}, "label"),
            ({**base, "baseline_comparison": _baseline_config(ridge_alpha=2.0)}, "candidate_definitions"),
            ({**base, "baseline_comparison": _baseline_config(period=6)}, "factor_spec"),
            ({**base, "random_seed": 8}, "candidate_definitions"),
            ({**base, "min_rank_ic": -0.5}, "min_rank_ic"),
        ):
            with pytest.raises((AlphaError, ValueError)) as error:
                registry.register_trial(
                    campaign_id=campaign.campaign_id,
                    protocol_hash=protocol.protocol_hash,
                    hypothesis_id=protocol.registered_hypotheses[0].hypothesis_id,
                    request=mutated,
                    trial_purpose="PARAMETER_VARIATION",
                )
            detail = json.dumps(getattr(error.value, "context", {})) + str(error.value)
            assert field in detail or (field == "factor_spec" and "pct_change" in detail)
    finally:
        jobs.connection.close()


def test_walk_forward_mode_and_stopping_policy_are_enforced(tmp_path):
    jobs, registry, campaign, _protocol_default = _setup_registry(tmp_path)
    try:
        factor = _factor_spec()
        policy = {
            "primary_metric": "rank_ic", "minimum_rank_ic": -1.0,
            "minimum_label_coverage": 0.0, "minimum_model_execution_coverage": 0.0,
            "minimum_end_to_end_coverage": 0.0,
        }
        protocol = registry.register_protocol(_protocol(
            campaign.campaign_id,
            protocol_revision=2,
            parent_protocol_hash=_protocol_default.protocol_hash,
            modification_reason="Constrain walk-forward mode and stopping rule.",
            allowed_job_kinds=["walk_forward_alpha"],
            factor_specifications=[factor],
            selection_policy=policy,
            evaluation_policy={"primary_metric": "rank_ic", "minimum_rank_ic": -1.0, "walk_forward_mode": "expanding"},
            stopping_policy={"max_seconds": 60, "max_folds": 5, "max_bootstrap_replicates": 25},
        ))
        base = _walk_forward_request(protocol.protocol_hash)
        base["max_seconds"] = 60
        base["walk_forward_config"]["max_folds"] = 5
        registry.register_trial(
            campaign_id=campaign.campaign_id, protocol_hash=protocol.protocol_hash,
            hypothesis_id=protocol.registered_hypotheses[0].hypothesis_id,
            request=base, trial_purpose="ROBUSTNESS_CHECK",
        )
        for mutated, field in (
            ({**base, "walk_forward_config": {**base["walk_forward_config"], "mode": "rolling"}}, "walk_forward_config.mode"),
            ({**base, "max_seconds": 120}, "max_seconds"),
            ({**base, "walk_forward_config": {**base["walk_forward_config"], "max_folds": 12}}, "max_folds"),
        ):
            with pytest.raises(AlphaError) as error:
                registry.register_trial(
                    campaign_id=campaign.campaign_id, protocol_hash=protocol.protocol_hash,
                    hypothesis_id=protocol.registered_hypotheses[0].hypothesis_id,
                    request=mutated, trial_purpose="ROBUSTNESS_CHECK",
                )
            assert field in json.dumps(error.value.context)
    finally:
        jobs.connection.close()


@pytest.mark.parametrize(
    ("selection_policy", "reason"),
    [
        ({"minimum_label_coverage": 0.5}, "LABEL_COVERAGE_NOT_ESTIMABLE"),
        ({"minimum_model_execution_coverage": 0.95}, "BELOW_MODEL_EXECUTION_COVERAGE_THRESHOLD"),
        ({"minimum_end_to_end_coverage": 0.5}, "END_TO_END_COVERAGE_NOT_ESTIMABLE"),
    ],
)
def test_selection_coverage_metrics_use_distinct_evidence(tmp_path, selection_policy, reason):
    jobs, registry, campaign, _protocol_default = _setup_registry(tmp_path)
    try:
        policy = {
            "primary_metric": "rank_ic", "minimum_rank_ic": -1.0,
            "minimum_label_coverage": 0.0, "minimum_model_execution_coverage": 0.0,
            "minimum_end_to_end_coverage": 0.0,
            **selection_policy,
        }
        protocol = registry.register_protocol(_protocol(
            campaign.campaign_id,
            protocol_revision=2,
            parent_protocol_hash=_protocol_default.protocol_hash,
            modification_reason="Constrain coverage metrics.",
            selection_policy=policy,
            evaluation_policy=policy,
        ))
        request, trial = _register_trial(registry, protocol)
        _publish_training(jobs, request, trial, rank_ic=0.1)
        snapshot, selection = registry.seal_candidate_ledger(
            campaign_id=campaign.campaign_id, protocol_hash=protocol.protocol_hash,
        )
        assert selection.selection_status == "NO_ELIGIBLE_CANDIDATE"
        assert snapshot.excluded_candidates[0]["reason"] == reason
    finally:
        jobs.connection.close()


def test_walk_forward_requirement_blocks_training_only_selection(tmp_path):
    jobs, registry, campaign, _protocol_default = _setup_registry(tmp_path)
    try:
        policy = {
            "primary_metric": "rank_ic", "minimum_rank_ic": -1.0,
            "minimum_label_coverage": 0.0, "minimum_model_execution_coverage": 0.0,
            "minimum_end_to_end_coverage": 0.0,
        }
        protocol = registry.register_protocol(_protocol(
            campaign.campaign_id,
            protocol_revision=2,
            parent_protocol_hash=_protocol_default.protocol_hash,
            modification_reason="Require walk-forward evidence.",
            selection_policy=policy,
            evaluation_policy={**policy, "requires_walk_forward": True},
        ))
        request, trial = _register_trial(registry, protocol)
        _publish_training(jobs, request, trial)
        snapshot, selection = registry.seal_candidate_ledger(
            campaign_id=campaign.campaign_id, protocol_hash=protocol.protocol_hash,
        )
        assert selection.selection_status == "NO_ELIGIBLE_CANDIDATE"
        assert snapshot.excluded_candidates[0]["reason"] == "WALK_FORWARD_CANDIDATE_BINDING_NOT_SUPPORTED"
        report = registry.build_evidence_report(
            campaign_id=campaign.campaign_id,
            candidate_ledger_hash=snapshot.candidate_ledger_hash,
        )
        assert any("WALK_FORWARD_CANDIDATE_BINDING_NOT_SUPPORTED" in risk for risk in report.unresolved_risks)
    finally:
        jobs.connection.close()


def test_other_protocol_trials_remain_in_ledger_but_not_current_selection_pool(tmp_path):
    jobs, registry, campaign, first = _setup_registry(tmp_path)
    try:
        request, trial = _register_trial(registry, first)
        _publish_training(jobs, request, trial)
        second = registry.register_protocol(_protocol(
            campaign.campaign_id,
            protocol_revision=2,
            parent_protocol_hash=first.protocol_hash,
            modification_reason="New selection protocol.",
        ))
        second_request, second_trial = _register_trial(registry, second, seed=8)
        _publish_training(jobs, second_request, second_trial)
        snapshot, selection = registry.seal_candidate_ledger(
            campaign_id=campaign.campaign_id, protocol_hash=second.protocol_hash,
        )
        assert {item.trial_id for item in snapshot.registered_trials} == {trial.trial_id, second_trial.trial_id}
        assert selection.considered_trial_ids == [trial.trial_id, second_trial.trial_id]
        incompatible = [item for item in snapshot.excluded_candidates if item["reason"] == "PROTOCOL_NOT_COMPATIBLE_WITH_SELECTION"]
        assert [item["trial_id"] for item in incompatible] == [trial.trial_id]
        assert selection.selected_trial_id == second_trial.trial_id
    finally:
        jobs.connection.close()


def test_protocol_budget_is_enforced_in_addition_to_campaign_budget(tmp_path):
    jobs, registry, _campaign_default, _protocol_default = _setup_registry(tmp_path)
    try:
        campaign = registry.register_campaign(_campaign(campaign_name="Protocol budget", resource_policy={
            "max_registered_trials": 4, "max_submitted_trials": 4,
            "max_distinct_candidate_definitions": 4, "max_evaluation_jobs": 4,
            "max_execution_attempts": 8, "max_protocol_revisions": 1,
        }))
        budget = {
            "max_registered_trials": 1, "max_submitted_trials": 1,
            "max_distinct_candidate_definitions": 1, "max_evaluation_jobs": 1,
            "max_execution_attempts": 2, "max_protocol_revisions": 1,
        }
        protocol = registry.register_protocol(_protocol(
            campaign.campaign_id, trial_budget=budget, resource_policy=budget,
        ))
        _register_trial(registry, protocol)
        with pytest.raises(AlphaError) as error:
            _register_trial(registry, protocol, seed=8)
        assert error.value.code == AlphaErrorCode.RESEARCH_TRIAL_BUDGET_EXCEEDED
        assert "protocol" in error.value.context.get("scope", "") or error.value.context.get("scope") == "protocol"
    finally:
        jobs.connection.close()


def test_attempt_budget_exhaustion_fails_head_job_and_continues_other_campaign(tmp_path):
    jobs, registry, _campaign_a, _protocol_a = _setup_registry(tmp_path)
    try:
        campaign_b = registry.register_campaign(_campaign(campaign_name="Second campaign", resource_policy={
            "max_registered_trials": 1, "max_submitted_trials": 1,
            "max_distinct_candidate_definitions": 1, "max_evaluation_jobs": 1,
            "max_execution_attempts": 1, "max_protocol_revisions": 1,
        }))
        protocol_b = registry.register_protocol(_protocol(campaign_b.campaign_id))
        # Tighten campaign A through a new campaign because Protocol budgets cannot increase.
        campaign_a_tight = registry.register_campaign(_campaign(
            campaign_name="First campaign tight",
            resource_policy={
                "max_registered_trials": 1, "max_submitted_trials": 1,
                "max_distinct_candidate_definitions": 1, "max_evaluation_jobs": 1,
                "max_execution_attempts": 1, "max_protocol_revisions": 1,
            },
        ))
        protocol_a_tight = registry.register_protocol(_protocol(campaign_a_tight.campaign_id))
        request_a, trial_a = _register_trial(registry, protocol_a_tight)
        request_b, trial_b = _register_trial(registry, protocol_b)
        job_a = jobs.submit_alpha({**request_a, "research_trial_id": trial_a.trial_id})
        job_b = jobs.submit_alpha({**request_b, "research_trial_id": trial_b.trial_id})
        jobs.connection.execute("UPDATE jobs SET created_at=1 WHERE id=?", (job_a["job_id"],))
        jobs.connection.commit()
        first_claim = jobs.claim()
        assert first_claim["id"] == job_a["job_id"]
        jobs.connection.execute("UPDATE jobs SET lease_until=0 WHERE id=?", (job_a["job_id"],))
        jobs.connection.commit()
        second_claim = jobs.claim()
        assert second_claim["id"] == job_b["job_id"]
        failed = jobs.get_job(job_a["job_id"])
        assert failed["status"] == "failed"
        assert failed["error_code"] == AlphaErrorCode.RESEARCH_TRIAL_BUDGET_EXCEEDED.value
        events = registry.list_events(campaign_a_tight.campaign_id)
        assert any(event.event_type == "TRIAL_FAILED" and event.reason_code == "RESEARCH_TRIAL_BUDGET_EXCEEDED" for event in events)
        assert any(event.event_type == "TRIAL_EXECUTION_STARTED" for event in registry.list_events(campaign_b.campaign_id))
    finally:
        jobs.connection.close()


def test_two_campaign_trial_registrations_are_concurrent_and_isolated(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    path = tmp_path / "registry.sqlite3"
    with JobStore(path) as setup:
        campaigns = []
        protocols = []
        for index in range(2):
            campaign = setup.research_registry.register_campaign(_campaign(
                campaign_name=f"Concurrent {index}",
                resource_policy={
                    "max_registered_trials": 1, "max_submitted_trials": 1,
                    "max_distinct_candidate_definitions": 1, "max_evaluation_jobs": 1,
                    "max_execution_attempts": 2, "max_protocol_revisions": 1,
                },
            ))
            protocol = setup.research_registry.register_protocol(_protocol(campaign.campaign_id))
            campaigns.append(campaign)
            protocols.append(protocol)
    barrier = Barrier(2)

    def register(index):
        with JobStore(path) as jobs:
            barrier.wait()
            protocol = protocols[index]
            return jobs.research_registry.register_trial(
                campaign_id=protocol.definition.campaign_id,
                protocol_hash=protocol.protocol_hash,
                hypothesis_id=protocol.registered_hypotheses[0].hypothesis_id,
                request=_training_request(research_protocol_hash=protocol.protocol_hash),
                trial_purpose="INITIAL_HYPOTHESIS_TEST",
            ).trial_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(register, (0, 1)))
    assert len(set(ids)) == 2
    with JobStore(path) as jobs:
        for campaign in campaigns:
            assert jobs.connection.execute(
                "SELECT count(*) FROM alpha_research_trials WHERE campaign_id=?", (campaign.campaign_id,)
            ).fetchone()[0] == 1


def test_rejection_does_not_overwrite_completed_trial_status(tmp_path):
    jobs, registry, campaign, protocol = _setup_registry(tmp_path)
    try:
        request, trial = _register_trial(registry, protocol)
        _publish_training(jobs, request, trial)
        with pytest.raises(AlphaError) as error:
            jobs.submit_alpha({**request, "research_trial_id": trial.trial_id, "random_seed": 8})
        assert error.value.code == AlphaErrorCode.RESEARCH_TRIAL_REQUEST_MISMATCH
        assert registry.get_trial(trial.trial_id).status == "COMPLETED"
        rejected = [
            event for event in registry.list_events(campaign.campaign_id)
            if event.event_type == "TRIAL_REJECTED"
        ]
        assert rejected[-1].detail["previous_status"] == "COMPLETED"
        assert rejected[-1].detail["status_preserved"] is True
    finally:
        jobs.connection.close()


def test_ledger_pagination_and_snapshot_report_are_stable_across_new_evidence(tmp_path):
    jobs, registry, campaign, protocol = _setup_registry(tmp_path)
    try:
        request, trial = _register_trial(registry, protocol)
        submission, _model = _publish_training(jobs, request, trial)
        snapshot, _selection = registry.seal_candidate_ledger(
            campaign_id=campaign.campaign_id, protocol_hash=protocol.protocol_hash,
        )
        first = registry.read_research_ledger(campaign_id=campaign.campaign_id, section="events", limit=2)
        assert first["next_cursor"]
        report_before = registry.read_research_report(
            campaign_id=campaign.campaign_id, candidate_ledger_hash=snapshot.candidate_ledger_hash,
            section="summary",
        )
        jobs.get_result(submission["job_id"], "summary")
        repeated_snapshot, _repeated_selection = registry.seal_candidate_ledger(
            campaign_id=campaign.campaign_id, protocol_hash=protocol.protocol_hash,
        )
        assert repeated_snapshot.candidate_ledger_hash == snapshot.candidate_ledger_hash
        _register_trial(registry, protocol, seed=8, purpose="ROBUSTNESS_CHECK")
        second = registry.read_research_ledger(
            campaign_id=campaign.campaign_id, section="events", limit=2, cursor=first["next_cursor"],
        )
        assert len(second["rows"]) == 2
        assert all(event["event_sequence"] <= first["ledger_watermark"] for event in second["rows"])
        registry.register_campaign(_campaign(campaign_name="Later external evidence"))
        report_after = registry.read_research_report(
            campaign_id=campaign.campaign_id, candidate_ledger_hash=snapshot.candidate_ledger_hash,
            section="summary",
        )
        assert report_after["report_hash"] == report_before["report_hash"]
        rebuilt = registry.build_evidence_report(
            campaign_id=campaign.campaign_id,
            ledger_watermark=snapshot.ledger_watermark,
        )
        assert rebuilt.report_hash == report_before["report_hash"]
    finally:
        jobs.connection.close()


def test_report_pagination_cursor_survives_new_exposure_and_trial(tmp_path):
    jobs, registry, campaign, _protocol_default = _setup_registry(tmp_path)
    try:
        second_hypothesis = {
            **_protocol(campaign.campaign_id)["hypothesis_definitions"][0],
            "hypothesis_key": "second_hypothesis",
            "title": "Second hypothesis",
        }
        protocol = registry.register_protocol(_protocol(
            campaign.campaign_id,
            protocol_revision=2,
            parent_protocol_hash=_protocol_default.protocol_hash,
            modification_reason="Add a second hypothesis.",
            hypothesis_definitions=[
                _protocol(campaign.campaign_id)["hypothesis_definitions"][0],
                second_hypothesis,
            ],
        ))
        request, trial = _register_trial(registry, protocol)
        submission, _model = _publish_training(jobs, request, trial)
        snapshot, _selection = registry.seal_candidate_ledger(
            campaign_id=campaign.campaign_id, protocol_hash=protocol.protocol_hash,
        )
        first = registry.read_research_report(
            campaign_id=campaign.campaign_id, candidate_ledger_hash=snapshot.candidate_ledger_hash,
            section="hypotheses", limit=1,
        )
        assert first["next_cursor"]
        jobs.get_result(submission["job_id"], "summary")
        _register_trial(registry, protocol, seed=8, purpose="ROBUSTNESS_CHECK")
        second = registry.read_research_report(
            campaign_id=campaign.campaign_id, candidate_ledger_hash=snapshot.candidate_ledger_hash,
            section="hypotheses", limit=1, cursor=first["next_cursor"],
        )
        assert len(second["rows"]) == 1
        assert first["rows"][0]["hypothesis_id"] != second["rows"][0]["hypothesis_id"]
        assert {first["rows"][0]["hypothesis_key"], second["rows"][0]["hypothesis_key"]} == {
            "short_reversal", "second_hypothesis",
        }
    finally:
        jobs.connection.close()


def _managed_freeze_record(protocol, snapshot, selection, model, freeze_request):
    body = {
        "schema_version": "1.0",
        "research_protocol_hash": protocol.protocol_hash,
        "candidate_ledger_hash": snapshot.candidate_ledger_hash,
        "frozen_model_ids": [model["model_id"]],
        "model_hashes": {model["model_id"]: model["model_hash"]},
        "test_dataset_hashes": {
            "test_feature_set_id": freeze_request["test_feature_set_id"],
            "test_label_source_dataset_id": freeze_request["test_label_source_dataset_id"],
        },
        "test_dataset_ids": {
            "test_feature_set_id": freeze_request["test_feature_set_id"],
            "test_label_source_dataset_id": freeze_request["test_label_source_dataset_id"],
        },
        "test_window": {"start": freeze_request["test_start"], "end": freeze_request["test_end"]},
        "label_spec": {
            "label_name": freeze_request["label_name"], "target_type": freeze_request["target_type"],
            "horizon_days": freeze_request["horizon_days"],
            "label_source_dataset_id": freeze_request["test_label_source_dataset_id"],
            "label_version": "observed-record-v1",
        },
        "selection_reason": freeze_request["selection_reason"],
    }
    return FrozenResearchRecord(**body, freeze_id=digest(body), frozen_at=datetime.now(UTC))


def test_worker_rejects_tampered_trial_job_request_before_execution(tmp_path):
    jobs, registry, _campaign, protocol = _setup_registry(tmp_path)
    try:
        request, trial = _register_trial(registry, protocol)
        submission = jobs.submit_alpha({**request, "research_trial_id": trial.trial_id})
        encoded = jobs.connection.execute(
            "SELECT request FROM jobs WHERE id=?", (submission["job_id"],),
        ).fetchone()[0]
        tampered = json.loads(encoded)
        tampered["random_seed"] = 8
        jobs.connection.execute(
            "UPDATE jobs SET request=?,config_hash=? WHERE id=?",
            (json.dumps(tampered), digest(tampered), submission["job_id"]),
        )
        jobs.connection.commit()
        claim = jobs.claim()
        assert claim is not None
        execute_job(jobs, claim)
        failed = jobs.get_job(submission["job_id"])
        assert failed["status"] == "failed"
        assert failed["error_code"] == AlphaErrorCode.RESEARCH_TRIAL_REQUEST_MISMATCH.value
    finally:
        jobs.connection.close()


def test_managed_freeze_rechecks_freshness_at_publication_boundary(tmp_path, monkeypatch):
    import stock_quant.alpha.jobs.executor as executor_module

    jobs, registry, campaign, protocol = _setup_registry(tmp_path)
    try:
        request, trial = _register_trial(registry, protocol)
        _submission, model = _publish_training(jobs, request, trial)
        snapshot, selection = registry.seal_candidate_ledger(
            campaign_id=campaign.campaign_id, protocol_hash=protocol.protocol_hash,
        )
        freeze_request = _freeze_request_for_selection(protocol, selection, model["model_id"])
        record = _managed_freeze_record(protocol, snapshot, selection, model, freeze_request)
        freeze_job = jobs.submit_alpha(freeze_request)
        monkeypatch.setattr(executor_module, "freeze_alpha_selection", lambda *args, **kwargs: record)
        original_begin_publish = jobs.begin_publish
        advanced = False

        def begin_and_advance(claim, result):
            nonlocal advanced
            changed = original_begin_publish(claim, result)
            if changed and not advanced:
                advanced = True
                _register_trial(registry, protocol, seed=8, purpose="REPRODUCIBILITY_CHECK")
            return changed

        monkeypatch.setattr(jobs, "begin_publish", begin_and_advance)
        claim = jobs.claim()
        assert claim is not None
        execute_job(jobs, claim)
        failed = jobs.get_job(freeze_job["job_id"])
        assert failed["status"] == "failed"
        assert failed["error_code"] == AlphaErrorCode.RESEARCH_LEDGER_STALE.value
        assert not list(Path(jobs.path).with_suffix(".freezes").glob("*.json"))
    finally:
        jobs.connection.close()



def test_protocol_revision_does_not_reset_campaign_trial_usage(tmp_path):
    jobs, registry, _default_campaign, _default_protocol = _setup_registry(tmp_path)
    try:
        campaign = registry.register_campaign(_campaign(campaign_name="Cumulative campaign", resource_policy={
            "max_registered_trials": 2, "max_submitted_trials": 2,
            "max_distinct_candidate_definitions": 2, "max_evaluation_jobs": 2,
            "max_execution_attempts": 4, "max_protocol_revisions": 3,
        }))
        first = registry.register_protocol(_protocol(campaign.campaign_id))
        _register_trial(registry, first)
        second = registry.register_protocol(_protocol(
            campaign.campaign_id,
            protocol_revision=2,
            parent_protocol_hash=first.protocol_hash,
            modification_reason="Change protocol without resetting campaign usage.",
        ))
        _register_trial(registry, second, seed=8)
        third = registry.register_protocol(_protocol(
            campaign.campaign_id,
            protocol_revision=3,
            parent_protocol_hash=second.protocol_hash,
            modification_reason="Campaign budget remains cumulative.",
        ))
        with pytest.raises(AlphaError) as error:
            _register_trial(registry, third, seed=9)
        assert error.value.code == AlphaErrorCode.RESEARCH_TRIAL_BUDGET_EXCEEDED
        assert error.value.context["scope"] == "campaign"
    finally:
        jobs.connection.close()


def test_unsupported_protocol_constraints_are_rejected_at_registration(tmp_path):
    jobs, registry, campaign, protocol = _setup_registry(tmp_path)
    try:
        with pytest.raises(ValueError, match="Unsupported Protocol evaluation/stopping constraints"):
            registry.register_protocol(_protocol(
                campaign.campaign_id,
                protocol_revision=2,
                parent_protocol_hash=protocol.protocol_hash,
                modification_reason="Declare unsupported constraint.",
                evaluation_policy={"primary_metric": "rank_ic", "minimum_rank_ic": 0.0, "implicit_hpo": True},
            ))
        with pytest.raises(ValueError, match="Unsupported Protocol evaluation/stopping constraints"):
            registry.register_protocol(_protocol(
                campaign.campaign_id,
                protocol_revision=2,
                parent_protocol_hash=protocol.protocol_hash,
                modification_reason="Declare unsupported stopping constraint.",
                stopping_policy={"max_seconds": 60, "patience": 3},
            ))
    finally:
        jobs.connection.close()



@pytest.mark.parametrize("variant", ["horizon", "factor_spec", "model_config"])
def test_unrelated_walk_forward_evidence_never_binds_training_candidate(tmp_path, monkeypatch, variant):
    jobs, registry, campaign, _protocol_default = _setup_registry(tmp_path)
    try:
        policy = {
            "primary_metric": "rank_ic", "minimum_rank_ic": -1.0,
            "minimum_label_coverage": 0.0, "minimum_model_execution_coverage": 0.0,
            "minimum_end_to_end_coverage": 0.0,
        }
        protocol = registry.register_protocol(_protocol(
            campaign.campaign_id,
            protocol_revision=2,
            parent_protocol_hash=_protocol_default.protocol_hash,
            modification_reason="Require candidate-bound walk-forward evidence.",
            allowed_job_kinds=["train_alpha_model", "walk_forward_alpha"],
            label_specifications=[],
            candidate_definitions=[],
            selection_policy=policy,
            evaluation_policy={**policy, "requires_walk_forward": True},
        ))
        request = _training_request(research_protocol_hash=protocol.protocol_hash)
        if variant == "horizon":
            request["horizon_days"] = 10
        elif variant == "factor_spec":
            spec = _factor_spec_with_extra()
            request["baseline_comparison"] = {
                "schema_version": "1.0", "factor_spec": spec,
                "reversal_output": "ret", "rsi_output": "rsi", "ridge_alpha": 1.0,
            }
        else:
            request["hyperparameters"] = {"n_estimators": 10, "num_leaves": 7}
        training_trial = registry.register_trial(
            campaign_id=campaign.campaign_id,
            protocol_hash=protocol.protocol_hash,
            hypothesis_id=protocol.registered_hypotheses[0].hypothesis_id,
            request=request,
            trial_purpose="INITIAL_HYPOTHESIS_TEST",
        )
        _publish_training(jobs, request, training_trial)
        wf_request = _walk_forward_request(protocol.protocol_hash)
        wf_request["max_seconds"] = 60
        wf_trial = registry.register_trial(
            campaign_id=campaign.campaign_id,
            protocol_hash=protocol.protocol_hash,
            hypothesis_id=protocol.registered_hypotheses[0].hypothesis_id,
            request=wf_request,
            trial_purpose="ROBUSTNESS_CHECK",
        )
        wf_job = jobs.submit_alpha({**wf_request, "research_trial_id": wf_trial.trial_id})
        claim = jobs.claim()
        assert claim is not None
        assert jobs.begin_publish(claim, {"summary": {"status": "WF_COMPLETED"}, "walk_forward": [], "reproducibility": {}})
        assert jobs.complete_publish(claim)
        monkeypatch.setattr(
            registry, "_walk_forward_evidence",
            lambda trial, job_id: {"trial_id": trial.trial_id, "job_id": job_id, "status": "WF_COMPLETED"},
        )
        snapshot, selection = registry.seal_candidate_ledger(
            campaign_id=campaign.campaign_id, protocol_hash=protocol.protocol_hash,
        )
        training_exclusions = [
            item for item in snapshot.excluded_candidates
            if item["trial_id"] == training_trial.trial_id
        ]
        assert training_exclusions[0]["reason"] == "WALK_FORWARD_CANDIDATE_BINDING_NOT_SUPPORTED"
        assert selection.selection_status == "NO_ELIGIBLE_CANDIDATE"
        assert wf_job["job_id"] == registry.get_trial_job(wf_trial.trial_id).job_id
    finally:
        jobs.connection.close()


def test_trial_at_actual_registration_event_is_in_historical_view_but_later_trial_is_not(tmp_path):
    jobs, registry, campaign, protocol = _setup_registry(tmp_path)
    try:
        _request, first_trial = _register_trial(registry, protocol)
        registered_event = next(
            event for event in registry.list_events(campaign.campaign_id)
            if event.event_type == "TRIAL_REGISTERED" and event.trial_id == first_trial.trial_id
        )
        w1 = registered_event.event_sequence
        history = registry.read_research_ledger(
            campaign_id=campaign.campaign_id, section="trials", ledger_watermark=w1, limit=100,
        )
        assert [row["trial_id"] for row in history["rows"]] == [first_trial.trial_id]
        _register_trial(registry, protocol, seed=8, purpose="PARAMETER_VARIATION")
        history_again = registry.read_research_ledger(
            campaign_id=campaign.campaign_id, section="trials", ledger_watermark=w1, limit=100,
        )
        assert [row["trial_id"] for row in history_again["rows"]] == [first_trial.trial_id]
    finally:
        jobs.connection.close()


def test_running_trial_does_not_leak_later_completion_into_fixed_watermark_report(tmp_path):
    jobs, registry, campaign, protocol = _setup_registry(tmp_path)
    try:
        request, trial = _register_trial(registry, protocol)
        jobs.submit_alpha({**request, "research_trial_id": trial.trial_id})
        claim = jobs.claim()
        assert claim is not None
        w1 = registry.ledger_watermark(campaign.campaign_id)
        before = registry.build_evidence_report(
            campaign_id=campaign.campaign_id, protocol_hash=protocol.protocol_hash, ledger_watermark=w1,
        )
        assert before.trial_accounting.completed_evaluation_count == 0
        assert before.evidence_references == []
        assert before.baseline_evidence == []
        assert jobs.begin_publish(claim, {"summary": {"status": "completed"}})
        assert jobs.complete_publish(claim)
        after = registry.build_evidence_report(
            campaign_id=campaign.campaign_id, protocol_hash=protocol.protocol_hash, ledger_watermark=w1,
        )
        assert after.report_hash == before.report_hash
        assert after.evidence_references == []
    finally:
        jobs.connection.close()


def test_historical_exposure_summary_is_reconstructed_from_events_at_watermark(tmp_path):
    jobs, registry, campaign, protocol = _setup_registry(tmp_path)
    try:
        request, trial = _register_trial(registry, protocol)
        submission, _model = _publish_training(jobs, request, trial)
        jobs.get_result(submission["job_id"], "summary")
        w1 = registry.ledger_watermark(campaign.campaign_id)
        before = registry.build_evidence_report(
            campaign_id=campaign.campaign_id, protocol_hash=protocol.protocol_hash, ledger_watermark=w1,
        )
        assert before.result_exposure_summary["access_request_count"] == 1
        jobs.get_result(submission["job_id"], "summary")
        after = registry.build_evidence_report(
            campaign_id=campaign.campaign_id, protocol_hash=protocol.protocol_hash, ledger_watermark=w1,
        )
        assert after.report_hash == before.report_hash
        assert after.result_exposure_summary["access_request_count"] == 1
        assert after.result_exposure_summary["accesses"][0]["access_count"] == 1
    finally:
        jobs.connection.close()


def test_later_protocol_and_hypothesis_are_excluded_from_historical_views_and_report(tmp_path):
    jobs, registry, campaign, first = _setup_registry(tmp_path)
    try:
        _register_trial(registry, first)
        w1 = registry.ledger_watermark(campaign.campaign_id)
        report_before = registry.build_evidence_report(
            campaign_id=campaign.campaign_id, ledger_watermark=w1,
        )
        protocols_before = registry.read_research_ledger(
            campaign_id=campaign.campaign_id, section="protocols", ledger_watermark=w1,
        )
        hypotheses_before = registry.read_research_ledger(
            campaign_id=campaign.campaign_id, section="hypotheses", ledger_watermark=w1,
        )
        second = registry.register_protocol(_protocol(
            campaign.campaign_id,
            protocol_revision=2,
            parent_protocol_hash=first.protocol_hash,
            modification_reason="Later revision must not rewrite history.",
            hypothesis_definitions=[
                _protocol(campaign.campaign_id)["hypothesis_definitions"][0],
                {
                    **_protocol(campaign.campaign_id)["hypothesis_definitions"][0],
                    "hypothesis_key": "later_hypothesis",
                    "title": "Later Hypothesis",
                },
            ],
        ))
        assert second.definition.protocol_revision == 2
        report_after = registry.build_evidence_report(
            campaign_id=campaign.campaign_id, ledger_watermark=w1,
        )
        protocols_after = registry.read_research_ledger(
            campaign_id=campaign.campaign_id, section="protocols", ledger_watermark=w1,
        )
        hypotheses_after = registry.read_research_ledger(
            campaign_id=campaign.campaign_id, section="hypotheses", ledger_watermark=w1,
        )
        assert report_after.report_hash == report_before.report_hash
        assert [row["protocol_hash"] for row in protocols_after["rows"]] == [
            row["protocol_hash"] for row in protocols_before["rows"]
        ]
        assert [row["hypothesis_id"] for row in hypotheses_after["rows"]] == [
            row["hypothesis_id"] for row in hypotheses_before["rows"]
        ]
        assert "later_hypothesis" not in json.dumps(report_after.hypothesis_summary)
    finally:
        jobs.connection.close()


def test_historical_report_pagination_cursor_rebuilds_fixed_view_after_new_activity(tmp_path):
    jobs, registry, _default_campaign, _default_protocol = _setup_registry(tmp_path)
    try:
        campaign = registry.register_campaign(_campaign(campaign_name="Historical pagination", resource_policy={
            "max_registered_trials": 4, "max_submitted_trials": 4,
            "max_distinct_candidate_definitions": 4, "max_evaluation_jobs": 4,
            "max_execution_attempts": 8, "max_protocol_revisions": 3,
        }))
        first = registry.register_protocol(_protocol(
            campaign.campaign_id,
            hypothesis_definitions=[
                _protocol(campaign.campaign_id)["hypothesis_definitions"][0],
                {
                    **_protocol(campaign.campaign_id)["hypothesis_definitions"][0],
                    "hypothesis_key": "second_hypothesis",
                    "title": "Second Hypothesis",
                },
            ],
        ))
        report = registry.build_evidence_report(campaign_id=campaign.campaign_id)
        w1 = report.ledger_watermark
        first_page = registry.read_research_report(
            campaign_id=campaign.campaign_id, ledger_watermark=w1,
            section="hypotheses", limit=1,
        )
        assert first_page["next_cursor"]
        _register_trial(registry, first, seed=8, purpose="PARAMETER_VARIATION")
        registry.register_protocol(_protocol(
            campaign.campaign_id,
            protocol_revision=2,
            parent_protocol_hash=first.protocol_hash,
            modification_reason="Later activity after fixed report page.",
        ))
        second_page = registry.read_research_report(
            campaign_id=campaign.campaign_id, ledger_watermark=w1,
            section="hypotheses", limit=1, cursor=first_page["next_cursor"],
        )
        assert second_page["report_hash"] == first_page["report_hash"]
        assert first_page["rows"][0]["hypothesis_id"] != second_page["rows"][0]["hypothesis_id"]
    finally:
        jobs.connection.close()


def test_complete_publish_holds_immediate_write_lock_against_new_trial(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    jobs, registry, campaign, protocol = _setup_registry(tmp_path)
    try:
        request, trial = _register_trial(registry, protocol)
        _submission, model = _publish_training(jobs, request, trial)
        _snapshot, selection = registry.seal_candidate_ledger(
            campaign_id=campaign.campaign_id, protocol_hash=protocol.protocol_hash,
        )
        freeze_request = _freeze_request_for_selection(protocol, selection, model["model_id"])
        freeze_job = jobs.submit_alpha(freeze_request)
        checked = Event()
        release = Event()
        publisher_ready = Event()
        outcome: dict[str, object] = {}

        def publish():
            with JobStore(jobs.path) as publisher:
                claim = publisher.claim()
                outcome["claim"] = claim
                assert claim is not None
                assert publisher.begin_publish(claim, {"summary": {"status": "completed"}})
                original = publisher.research_registry.verify_managed_freeze

                def checked_then_wait(**kwargs):
                    result = original(**kwargs)
                    checked.set()
                    assert release.wait(5)
                    return result

                monkeypatch.setattr(
                    publisher.research_registry, "verify_managed_freeze", checked_then_wait,
                )
                publisher_ready.set()
                outcome["completed"] = publisher.complete_publish(claim)

        def register_new_trial():
            assert checked.wait(5)
            with JobStore(jobs.path) as second:
                return second.research_registry.register_trial(
                    campaign_id=campaign.campaign_id,
                    protocol_hash=protocol.protocol_hash,
                    hypothesis_id=protocol.registered_hypotheses[0].hypothesis_id,
                    request=_training_request(
                        research_protocol_hash=protocol.protocol_hash, random_seed=8,
                    ),
                    trial_purpose="REPRODUCIBILITY_CHECK",
                )

        with ThreadPoolExecutor(max_workers=2) as pool:
            publish_future = pool.submit(publish)
            assert publisher_ready.wait(5)
            trial_future = pool.submit(register_new_trial)
            assert checked.wait(5)
            assert not trial_future.done(), "new Trial entered the final freshness-check/CAS window"
            release.set()
            assert publish_future.result(timeout=10) is None
            new_trial = trial_future.result(timeout=10)
        assert outcome["completed"] is True
        assert jobs.get_job(freeze_job["job_id"])["status"] == "completed"
        assert registry.get_trial(new_trial.trial_id).registered_ledger_watermark > _snapshot.ledger_watermark
    finally:
        release.set()
        jobs.connection.close()
