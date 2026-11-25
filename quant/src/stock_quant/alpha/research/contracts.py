"""Strict persisted contracts for the Alpha research experiment registry."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any, Literal

from pydantic import AwareDatetime, Field, PrivateAttr, model_validator

from ...contracts import StrictModel
from ..contracts import Hash

RegistrySchemaVersion = Literal["alpha-research-experiment-registry-v1"]
CampaignStatus = Literal["CAMPAIGN_OPEN", "CAMPAIGN_SEALED", "CAMPAIGN_CLOSED"]
TrialPurpose = Literal[
    "INITIAL_HYPOTHESIS_TEST",
    "PARAMETER_VARIATION",
    "DATA_VARIATION",
    "FACTOR_VARIATION",
    "ROBUSTNESS_CHECK",
    "REPRODUCIBILITY_CHECK",
    "FINAL_REFIT",
    "POST_HOC_EXPLORATION",
]
PreregistrationStatus = Literal[
    "PREREGISTERED_WITHIN_MANAGED_CAMPAIGN",
    "REGISTERED_AFTER_PRIOR_EXPOSURE",
    "POST_HOC_EXPLORATORY",
    "HISTORICAL_IMPORT_UNVERIFIED",
    "UNREGISTERED",
]
SelectionStatus = Literal[
    "CANDIDATE_SELECTED_FOR_FURTHER_RESEARCH",
    "NO_ELIGIBLE_CANDIDATE",
    "SELECTION_BLOCKED",
]


class ResearchTrialBudget(StrictModel):
    """Campaign-level hard limits plus explicitly reporting-only resource limits."""

    max_registered_trials: int = Field(default=100, ge=1, le=1_000_000, strict=True)
    max_submitted_trials: int = Field(default=100, ge=1, le=1_000_000, strict=True)
    max_distinct_candidate_definitions: int = Field(default=100, ge=1, le=1_000_000, strict=True)
    max_evaluation_jobs: int = Field(default=100, ge=1, le=1_000_000, strict=True)
    max_execution_attempts: int = Field(default=300, ge=1, le=10_000_000, strict=True)
    max_protocol_revisions: int = Field(default=10, ge=1, le=10_000, strict=True)
    max_model_fits: int | None = Field(default=None, ge=0, le=100_000_000, strict=True)
    max_total_training_samples: int | None = Field(default=None, ge=0, strict=True)
    max_total_runtime_seconds: int | None = Field(default=None, ge=0, strict=True)

    def hard_limits(self) -> dict[str, int]:
        return {
            "max_registered_trials": self.max_registered_trials,
            "max_submitted_trials": self.max_submitted_trials,
            "max_distinct_candidate_definitions": self.max_distinct_candidate_definitions,
            "max_evaluation_jobs": self.max_evaluation_jobs,
            "max_execution_attempts": self.max_execution_attempts,
            "max_protocol_revisions": self.max_protocol_revisions,
        }

    def reporting_only(self) -> dict[str, int | None]:
        return {
            "max_model_fits": self.max_model_fits,
            "max_total_training_samples": self.max_total_training_samples,
            "max_total_runtime_seconds": self.max_total_runtime_seconds,
        }


class ResearchDataScope(StrictModel):
    asset_class: str = Field(min_length=1, max_length=64)
    market: str = Field(min_length=1, max_length=64)
    universe_definition: dict[str, Any] = Field(default_factory=dict)
    development_start: AwareDatetime | None = None
    development_end: AwareDatetime | None = None
    data_provider: str | None = Field(default=None, max_length=128)
    provider_version: str | None = Field(default=None, max_length=128)
    stock_dataset_ids: list[Hash] = Field(default_factory=list)
    feature_dataset_ids: list[Hash] = Field(default_factory=list)
    parent_dataset_ids: list[Hash] = Field(default_factory=list)
    price_basis: str | None = Field(default=None, max_length=64)
    label_definitions: list[dict[str, Any]] = Field(default_factory=list)
    calendar_version: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def ordered_development_scope(self):
        if self.development_start is not None and self.development_end is not None and self.development_start >= self.development_end:
            raise ValueError("development_start must be before development_end")
        return self


class ResearchCampaignDefinition(StrictModel):
    schema_version: RegistrySchemaVersion = "alpha-research-experiment-registry-v1"
    campaign_name: str = Field(min_length=1, max_length=160)
    research_objective: str = Field(min_length=1, max_length=4096)
    research_scope: dict[str, Any] = Field(default_factory=dict)
    development_data_scope: ResearchDataScope
    related_campaign_ids: list[Hash] = Field(default_factory=list)
    resource_policy: ResearchTrialBudget = Field(default_factory=ResearchTrialBudget)


class ResearchCampaignRecord(StrictModel):
    schema_version: RegistrySchemaVersion = "alpha-research-experiment-registry-v1"
    campaign_id: Hash
    campaign_identity_hash: Hash
    definition: ResearchCampaignDefinition
    created_at: datetime
    campaign_status: CampaignStatus


class ResearchHypothesisDefinition(StrictModel):
    hypothesis_key: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=512)
    economic_rationale: str = Field(min_length=1, max_length=4096)
    expected_direction: Literal["positive", "negative", "two_sided"]
    target_variable: str = Field(min_length=1, max_length=128)
    factor_spec_hash: Hash | None = None
    primary_metric: str = Field(min_length=1, max_length=128)
    null_hypothesis: str = Field(min_length=1, max_length=2048)
    comparison_family: list[str] = Field(default_factory=list)
    candidate_constraints: dict[str, Any] = Field(default_factory=dict)


class ResearchSelectionPolicy(StrictModel):
    primary_metric: str = Field(default="rank_ic", min_length=1, max_length=128)
    minimum_rank_ic: float | None = None
    minimum_valid_folds: int = Field(default=0, ge=0, strict=True)
    minimum_valid_ic_sessions: int = Field(default=0, ge=0, strict=True)
    minimum_label_coverage: float = Field(default=0.0, ge=0.0, le=1.0)
    minimum_model_execution_coverage: float = Field(default=0.0, ge=0.0, le=1.0)
    minimum_end_to_end_coverage: float = Field(default=0.0, ge=0.0, le=1.0)
    allowed_research_status: list[str] = Field(default_factory=lambda: ["RESEARCH_DATA_VALIDATED", "RESEARCH_EXPLORATORY"])
    stability_constraints: dict[str, Any] = Field(default_factory=dict)
    tie_break_rule: Literal["highest_primary_metric_then_earliest_trial", "earliest_trial"] = (
        "highest_primary_metric_then_earliest_trial"
    )


class ResearchProtocolDefinition(StrictModel):
    schema_version: RegistrySchemaVersion = "alpha-research-experiment-registry-v1"
    campaign_id: Hash
    protocol_revision: int = Field(default=1, ge=1, strict=True)
    parent_protocol_hash: Hash | None = None
    modification_reason: str | None = Field(default=None, max_length=4096)
    hypothesis_definitions: list[ResearchHypothesisDefinition] = Field(min_length=1, max_length=512)
    candidate_definitions: list[dict[str, Any]] = Field(default_factory=list, max_length=512)
    allowed_job_kinds: list[Literal["train_alpha_model", "walk_forward_alpha"]] = Field(min_length=1)
    data_constraints: dict[str, Any] = Field(default_factory=dict)
    development_windows: list[dict[str, Any]] = Field(default_factory=list)
    label_specifications: list[dict[str, Any]] = Field(default_factory=list)
    factor_specifications: list[dict[str, Any]] = Field(default_factory=list)
    hyperparameter_constraints: dict[str, Any] = Field(default_factory=dict)
    seed_policy: dict[str, Any] = Field(default_factory=dict)
    evaluation_policy: dict[str, Any] = Field(default_factory=dict)
    selection_policy: ResearchSelectionPolicy = Field(default_factory=ResearchSelectionPolicy)
    inference_policy: dict[str, Any] = Field(default_factory=dict)
    stopping_policy: dict[str, Any] = Field(default_factory=dict)
    resource_policy: ResearchTrialBudget = Field(default_factory=ResearchTrialBudget)
    trial_budget: ResearchTrialBudget = Field(default_factory=ResearchTrialBudget)

    @model_validator(mode="after")
    def coherent_revision(self):
        keys = [definition.hypothesis_key for definition in self.hypothesis_definitions]
        if len(keys) != len(set(keys)):
            raise ValueError("hypothesis keys must be unique within a Protocol")
        if self.protocol_revision == 1:
            if self.parent_protocol_hash is not None:
                raise ValueError("initial Protocol revision cannot have a parent")
        elif self.parent_protocol_hash is None:
            raise ValueError("Protocol revision > 1 requires parent_protocol_hash")
        elif not self.modification_reason:
            raise ValueError("Protocol revision > 1 requires modification_reason")
        return self


class HypothesisRecord(StrictModel):
    campaign_id: Hash
    protocol_hash: Hash
    hypothesis_id: Hash
    definition: ResearchHypothesisDefinition
    hypothesis_definition_hash: Hash
    status: Literal["REGISTERED", "SUPPORTED", "REJECTED", "INCONCLUSIVE", "RETIRED"] = "REGISTERED"
    created_at: datetime


class ResearchProtocolRecord(StrictModel):
    definition: ResearchProtocolDefinition
    protocol_hash: Hash
    modification_ledger_watermark: int = Field(default=0, ge=0)
    exposure_context: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    registered_hypotheses: list[HypothesisRecord]


class ResearchTrialRecord(StrictModel):
    schema_version: RegistrySchemaVersion = "alpha-research-experiment-registry-v1"
    campaign_id: Hash
    trial_id: Hash
    trial_definition_hash: Hash
    canonical_request_hash: Hash
    canonical_request: dict[str, Any]
    evidence_scope_hash: Hash
    protocol_hash: Hash
    hypothesis_id: Hash
    parent_trial_id: Hash | None = None
    trial_purpose: TrialPurpose
    preregistration_status: PreregistrationStatus
    registered_at: datetime
    registered_ledger_watermark: int = Field(ge=0)
    status: Literal[
        "REGISTERED", "SUBMITTED", "RUNNING", "PUBLISHING", "COMPLETED",
        "FAILED", "CANCELLED", "REJECTED",
    ] = "REGISTERED"


class ResearchTrialJobRecord(StrictModel):
    campaign_id: Hash
    trial_id: Hash
    job_id: str
    job_config_hash: Hash
    request_hash: Hash
    created_at: datetime


class ResearchLedgerEvent(StrictModel):
    event_id: Hash
    event_sequence: int = Field(ge=1, strict=True)
    event_type: str = Field(min_length=1, max_length=64)
    campaign_id: Hash
    protocol_hash: Hash | None = None
    trial_id: Hash | None = None
    job_id: str | None = None
    attempt: int | None = Field(default=None, ge=0)
    request_hash: Hash | None = None
    result_hash: Hash | None = None
    reason_code: str | None = Field(default=None, max_length=128)
    detail: dict[str, Any] = Field(default_factory=dict)
    previous_event_hash: Hash | None = None
    event_hash: Hash
    created_at: datetime


class TrialAccounting(StrictModel):
    registered_trial_count: int = Field(ge=0)
    submitted_trial_count: int = Field(ge=0)
    distinct_trial_definition_count: int = Field(ge=0)
    execution_attempt_count: int = Field(ge=0)
    completed_evaluation_count: int = Field(ge=0)
    result_exposure_count: int = Field(ge=0)
    failed_trial_count: int = Field(ge=0)
    cancelled_trial_count: int = Field(ge=0)
    rejected_submission_count: int = Field(ge=0)
    model_fit_count: int = Field(ge=0)
    statistical_hypothesis_count: int = Field(ge=0)
    result_access_request_count: int = Field(default=0, ge=0)


class ResearchLedgerIntegrity(StrictModel):
    status: Literal["VERIFIED", "FAILED"]
    campaign_id: Hash
    ledger_watermark: int = Field(ge=0)
    event_root_hash: Hash | None = None
    failure_reason: str | None = None


class ScopeCompleteness(StrictModel):
    registry_scope: str = "THIS_SQLITE_REGISTRY_ONLY"
    managed_campaign_history_complete: bool
    job_lifecycle_reconciled: bool
    historical_import_complete: bool = False
    external_experiment_history_known: bool = False
    exposure_tracking_complete: bool = False
    global_trial_history_complete: Literal[False] = False
    ledger_integrity_status: Literal["VERIFIED", "FAILED"]


class ResearchTrialTerminalState(StrictModel):
    trial_id: Hash
    hypothesis_id: Hash
    protocol_hash: Hash
    trial_definition_hash: Hash
    canonical_request_hash: Hash | None = None
    trial_purpose: TrialPurpose
    preregistration_status: PreregistrationStatus
    terminal_state: str
    job_id: str | None = None
    job_status: str | None = None
    job_config_hash: Hash | None = None
    result_hash: Hash | None = None
    rejection_reason: str | None = None
    evidence_references: list[dict[str, Any]] = Field(default_factory=list)
    exclusion_reason: str | None = None


class ResearchLedgerSnapshot(StrictModel):
    schema_version: RegistrySchemaVersion = "alpha-research-experiment-registry-v1"
    campaign_id: Hash
    protocol_hash: Hash
    ledger_watermark: int = Field(ge=0, strict=True)
    ledger_event_root_hash: Hash | None = None
    registered_hypotheses: list[dict[str, Any]]
    registered_trials: list[ResearchTrialTerminalState]
    evidence_references: list[dict[str, Any]]
    excluded_candidates: list[dict[str, Any]]
    selection_policy_hash: Hash
    scope_completeness: ScopeCompleteness
    trial_accounting: TrialAccounting
    candidate_ledger_hash: Hash

    _stored_payload: dict[str, Any] | None = PrivateAttr(default=None)

    @classmethod
    def from_stored_payload(cls, payload: dict[str, Any]) -> ResearchLedgerSnapshot:
        snapshot = cls.model_validate(payload)
        snapshot._stored_payload = deepcopy(payload)
        return snapshot

    def model_dump(self, *args, **kwargs) -> dict[str, Any]:
        passthrough = {"mode", "by_alias", "round_trip", "warnings", "serialize_as_any", "fallback"}
        if self._stored_payload is not None and not args and not (set(kwargs) - passthrough):
            return deepcopy(self._stored_payload)
        return super().model_dump(*args, **kwargs)


class ExcludedCandidate(StrictModel):
    trial_id: Hash
    model_id: Hash | None = None
    reason: str
    detail: dict[str, Any] = Field(default_factory=dict)


class ResearchSelection(StrictModel):
    schema_version: RegistrySchemaVersion = "alpha-research-experiment-registry-v1"
    selection_id: Hash
    campaign_id: Hash
    protocol_hash: Hash
    candidate_ledger_hash: Hash
    selection_policy_hash: Hash
    considered_trial_ids: list[Hash]
    eligible_candidate_ids: list[Hash]
    excluded_candidates: list[ExcludedCandidate]
    selected_trial_id: Hash | None = None
    selected_model_id: Hash | None = None
    selection_reason: str
    evidence_references: list[dict[str, Any]] = Field(default_factory=list)
    selection_status: SelectionStatus
    selection_hash: Hash


class ResearchDataReuseFinding(StrictModel):
    status: Literal[
        "EXACT_EVIDENCE_REUSE",
        "POTENTIAL_OVERLAPPING_EVIDENCE",
        "DISTINCT_DECLARED_SCOPE",
        "INDEPENDENCE_UNVERIFIED",
    ]
    campaign_ids: list[Hash]
    reason: str
    detail: dict[str, Any] = Field(default_factory=dict)


class ResearchEvidenceReport(StrictModel):
    schema_version: Literal["alpha-research-evidence-report-v1"] = "alpha-research-evidence-report-v1"
    campaign_identity: dict[str, Any]
    protocol_identity: dict[str, Any]
    ledger_watermark: int = Field(ge=0)
    ledger_integrity: ResearchLedgerIntegrity
    scope_completeness: ScopeCompleteness
    trial_accounting: TrialAccounting
    resource_usage: dict[str, Any] = Field(default_factory=dict)
    hypothesis_summary: list[dict[str, Any]] = Field(default_factory=list)
    data_quality_summary: list[dict[str, Any]] = Field(default_factory=list)
    data_reuse_summary: list[ResearchDataReuseFinding] = Field(default_factory=list)
    result_exposure_summary: dict[str, Any] = Field(default_factory=dict)
    baseline_evidence: list[dict[str, Any]] = Field(default_factory=list)
    walk_forward_evidence: list[dict[str, Any]] = Field(default_factory=list)
    statistical_inference: dict[str, Any] = Field(default_factory=dict)
    multiple_testing_scope: Literal["CURRENT_PREREGISTERED_FAMILY_ONLY"] = "CURRENT_PREREGISTERED_FAMILY_ONLY"
    candidate_selection: dict[str, Any] | None = None
    unresolved_risks: list[str] = Field(default_factory=list)
    research_verdict: Literal[
        "RESEARCH_EVIDENCE_INTEGRITY_VERIFIED",
        "RESEARCH_EXPLORATORY_ONLY",
        "RESEARCH_INSUFFICIENT_EVIDENCE",
        "RESEARCH_EVIDENCE_INVALID",
        "RESEARCH_SELECTION_BLOCKED",
    ]
    evidence_references: list[dict[str, Any]] = Field(default_factory=list)
    report_hash: Hash
