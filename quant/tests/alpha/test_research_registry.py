from __future__ import annotations

import json
from pathlib import Path

import pytest
from stock_quant.alpha.errors import AlphaError, AlphaErrorCode
from stock_quant.alpha.models.registry import AlphaModelRegistry
from stock_quant.compiler import digest
from stock_quant.jobs import JobStore


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
        "candidate_definitions": [{"seed": 7}],
        "allowed_job_kinds": ["train_alpha_model", "walk_forward_alpha"],
        "data_constraints": {
            "feature_set_ids": ["sha256:" + "2" * 64],
            "stock_dataset_ids": ["sha256:" + "1" * 64],
        },
        "label_specifications": [{"label_name": "stock_forward_return", "target_type": "stock_return", "horizon_days": 5}],
        "selection_policy": {
            "primary_metric": "rank_ic",
            "minimum_rank_ic": 0.0,
            "minimum_label_coverage": 0.5,
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
        second_campaign = registry.register_campaign(_campaign(campaign_name="Renamed but reused"))
        report = registry.build_evidence_report(
            campaign_id=campaign.campaign_id,
            candidate_ledger_hash=snapshot.candidate_ledger_hash,
        )
        exact = [
            finding for finding in report.data_reuse_summary
            if finding.status == "EXACT_EVIDENCE_REUSE"
            and second_campaign.campaign_id in finding.campaign_ids
        ]
        assert exact
        assert report.scope_completeness.global_trial_history_complete is False
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
