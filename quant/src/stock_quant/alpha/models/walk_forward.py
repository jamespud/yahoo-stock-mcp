"""Leakage-safe development walk-forward validation for the fixed candidate set."""
from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from itertools import pairwise
from typing import Any, Literal

import lightgbm as lgb
import numpy as np
import pandas as pd
from pydantic import (
    AwareDatetime,
    Field,
    StrictFloat,
    StrictInt,
    field_validator,
    model_validator,
)
from pydantic_core import to_jsonable_python
from scipy.stats import spearmanr

from ...compiler import digest
from ...contracts import StrictModel
from ..contracts import DatasetManifest, Hash
from ..data.catalog import DatasetStore
from ..data.research_quality import ResearchQualityLimits, assess_research_quality
from ..data.session_coverage import mapped_stock_calendar
from ..errors import AlphaError, AlphaErrorCode
from ..research_admission import require_walk_forward_execution
from .baseline_comparison import (
    BaselineComparisonConfig,
    _assert_parquet_time_isolated,
    _lineage_manifests,
    _validate_factor_manifest_source,
    fit_train_only_ridge,
    fixed_factor_scores,
)
from .development_cohort import _feature_frame, _sample_keys_hash
from .evaluation import metrics, quantile_returns, rank_ic_by_decision
from .hyperparameters import INT32_MAX, validate_hyperparameters
from .labels import LabelRow, stock_forward_labels
from .lightgbm_trainer import lightgbm_parameters
from .registry import bytes_hash

REPORT_SCHEMA_VERSION = "alpha-walk-forward-v1"
PROTOCOL_VERSION = "alpha-walk-forward-protocol-v1"
CANDIDATE_KEYS = ("short_reversal_5", "rsi_5", "ridge_v1", "lightgbm_v1")
PAIR_DIRECTIONS = (
    ("lightgbm_v1", "ridge_v1"),
    ("lightgbm_v1", "short_reversal_5"),
    ("lightgbm_v1", "rsi_5"),
    ("ridge_v1", "short_reversal_5"),
)
PAIR_IDS = {"lightgbm_v1|ridge_v1": "H1", "lightgbm_v1|short_reversal_5": "H2",
            "lightgbm_v1|rsi_5": "H3", "ridge_v1|short_reversal_5": "H4"}
MAX_DECISION_DIAGNOSTICS = 512
STABILITY_MIN_POSITIVE_FRACTION = 2.0 / 3.0
STABILITY_MAX_FOLD_STD = 0.25


class WalkForwardDevelopmentWindow(StrictModel):
    development_start: AwareDatetime
    development_end: AwareDatetime

    @model_validator(mode="after")
    def ordered(self):
        if self.development_start >= self.development_end:
            raise ValueError("Expected development_start < development_end")
        return self


class WalkForwardLabelSpec(StrictModel):
    label_name: str = Field(min_length=1, max_length=128)
    target_type: Literal["stock_return"]
    stock_dataset_id: Hash
    horizon_days: int = Field(default=5, ge=1, le=252, strict=True)
    label_version: Literal["observed-record-v1", "session-aligned-v2"] | None = None


class BootstrapInferenceConfig(StrictModel):
    bootstrap_method: Literal["moving_block"] = "moving_block"
    bootstrap_block_sessions: int = Field(default=5, ge=1, le=252, strict=True)
    bootstrap_replicates: int = Field(default=1000, ge=1, le=2000, strict=True)
    confidence_level: StrictFloat = Field(default=0.95, ge=0.5, le=0.999)
    multiple_testing_method: Literal["holm"] = "holm"


class WalkForwardConfig(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    mode: Literal["expanding", "rolling"]
    train_sessions: int = Field(ge=6, le=5000, strict=True)
    validation_sessions: int = Field(ge=5, le=1000, strict=True)
    inner_validation_sessions: int = Field(ge=2, le=1000, strict=True)
    step_sessions: int = Field(ge=5, le=1000, strict=True)
    embargo_days: int = Field(default=0, ge=0, le=252, strict=True)
    min_folds: int = Field(default=3, ge=3, le=12, strict=True)
    max_folds: int = Field(default=12, ge=1, le=12, strict=True)
    inference: BootstrapInferenceConfig = Field(default_factory=BootstrapInferenceConfig)

    @model_validator(mode="after")
    def bounded_plan(self):
        if self.step_sessions != self.validation_sessions:
            raise ValueError("step_sessions must equal validation_sessions")
        if self.inner_validation_sessions >= self.train_sessions:
            raise ValueError("inner validation must leave a non-empty inner-fit window")
        if self.min_folds > self.max_folds:
            raise ValueError("min_folds must not exceed max_folds")
        return self


class WalkForwardResourceBudget(StrictModel):
    max_seconds: int = Field(default=1800, ge=1, le=86400, strict=True)
    max_total_physical_rows: int = Field(default=5_000_000, ge=1, strict=True)
    max_total_training_samples: int = Field(default=2_000_000, ge=1, strict=True)
    max_partitions: int = Field(default=1024, ge=1, strict=True)
    max_lineage_nodes: int = Field(default=512, ge=1, strict=True)
    max_bootstrap_replicates: int = Field(default=2000, ge=1, le=2000, strict=True)


class WalkForwardAlphaRequest(StrictModel):
    kind: Literal["walk_forward_alpha"] = "walk_forward_alpha"
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)
    evaluation_mode: Literal["validation_only"]
    research_protocol_hash: Hash
    feature_set_id: Hash
    stock_dataset_id: Hash
    label_spec: WalkForwardLabelSpec
    development_window: WalkForwardDevelopmentWindow
    baseline_comparison: BaselineComparisonConfig
    lightgbm_hyperparameters: dict[str, StrictInt | StrictFloat] = Field(default_factory=dict)
    random_seed: int = Field(default=0, strict=True, ge=-INT32_MAX - 1, le=INT32_MAX)
    walk_forward_config: WalkForwardConfig
    resource_budget: WalkForwardResourceBudget = Field(default_factory=WalkForwardResourceBudget)
    max_seconds: int = Field(default=1800, ge=1, le=86400, strict=True)

    _validate_hyperparameters = field_validator("lightgbm_hyperparameters")(validate_hyperparameters)

    @model_validator(mode="after")
    def coherent(self):
        if self.label_spec.stock_dataset_id != self.stock_dataset_id:
            raise ValueError("label_spec.stock_dataset_id must equal stock_dataset_id")
        if self.walk_forward_config.inference.bootstrap_replicates > self.resource_budget.max_bootstrap_replicates:
            raise ValueError("bootstrap_replicates exceeds the resource budget")
        return self


class FoldWindow(StrictModel):
    start: str
    end: str
    session_count: int = Field(ge=0, strict=True)


class FoldRecord(StrictModel):
    fold_id: Hash
    mode: Literal["expanding", "rolling"]
    train_window: FoldWindow
    inner_fit_window: FoldWindow
    inner_tuning_window: FoldWindow
    outer_validation_window: FoldWindow
    knowledge_cutoff: str
    purge_policy: dict[str, Any]
    train_identity_hash: Hash
    inner_tuning_identity_hash: Hash
    validation_identity_hash: Hash
    common_cohort_identity_hash: Hash
    model_hashes: dict[str, Hash]
    selected_iteration: int | None = Field(default=None, ge=1)
    candidate_metrics: dict[str, Any]
    paired_differences: dict[str, Any]
    coverage: dict[str, Any]
    exclusions: dict[str, Any]
    feature_importance: dict[str, Any]
    fold_status: Literal["FOLD_COMPLETED", "FOLD_INSUFFICIENT_DATA", "FOLD_INVALID"]
    fold_hash: Hash

    @model_validator(mode="after")
    def hash_binding(self):
        payload = self.model_dump(mode="json", exclude={"fold_hash"})
        if digest(payload) != self.fold_hash:
            raise ValueError("walk-forward fold hash mismatch")
        return self


class WalkForwardReport(StrictModel):
    schema_version: Literal["alpha-walk-forward-v1"]
    protocol_version: Literal["alpha-walk-forward-protocol-v1"]
    research_protocol_hash: Hash
    walk_forward_protocol_hash: Hash
    feature_set_id: Hash
    stock_dataset_id: Hash
    factor_spec_hash: Hash
    label_definition_hash: Hash
    development_window: dict[str, str]
    data_quality: dict[str, Any]
    fold_plan_hash: Hash
    folds: list[FoldRecord]
    pooled_oof_metrics: dict[str, Any]
    common_cohort: dict[str, Any]
    paired_comparisons: list[dict[str, Any]]
    uncertainty: dict[str, Any]
    multiple_testing: dict[str, Any]
    stability: dict[str, Any]
    exclusions: dict[str, Any]
    trial_ledger: list[dict[str, Any]]
    reproducibility: dict[str, Any]
    research_status: Literal["WF_COMPLETED", "WF_EXPLORATORY", "WF_INSUFFICIENT_EVIDENCE", "WF_INVALID"]
    report_hash: Hash

    @model_validator(mode="after")
    def hash_binding(self):
        fold_ids = [fold.fold_id for fold in self.folds]
        starts = [fold.outer_validation_window.start for fold in self.folds]
        if len(set(fold_ids)) != len(fold_ids) or starts != sorted(starts):
            raise ValueError("walk-forward folds must be unique and chronologically ordered")
        payload = self.model_dump(mode="json", exclude={"report_hash"})
        if digest(payload) != self.report_hash:
            raise ValueError("walk-forward report hash mismatch")
        return self


@dataclass(frozen=True)
class DecisionSession:
    session_date: str
    decision_at: datetime


@dataclass(frozen=True)
class PlannedFold:
    fold_id: str
    mode: str
    train_start: datetime
    train_end: datetime
    inner_fit_start: datetime
    inner_tuning_start: datetime
    inner_tuning_end: datetime
    outer_validation_start: datetime
    outer_validation_end: datetime
    knowledge_cutoff: datetime
    train_sessions: int
    inner_fit_sessions: int
    inner_tuning_sessions: int
    validation_sessions: int


@dataclass(frozen=True)
class WalkForwardPlan:
    walk_forward_protocol_hash: str
    label_definition_hash: str
    fold_plan_hash: str
    sessions: tuple[DecisionSession, ...]
    folds: tuple[PlannedFold, ...]
    exclusions: list[dict]


def _utc(value) -> datetime:
    if isinstance(value, pd.Timestamp):
        value = value.to_pydatetime()
    if not isinstance(value, datetime):
        raise TypeError("Expected datetime")
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat()


def _window(start: datetime, end: datetime, sessions: int) -> FoldWindow:
    return FoldWindow(start=_iso(start), end=_iso(end), session_count=sessions)


def _sample_hash(frame: pd.DataFrame) -> str:
    keys = frozenset(
        (_utc(decision), str(instrument))
        for decision, instrument in frame[["decision_at", "instrument_id"]].itertuples(index=False, name=None)
    )
    return _sample_keys_hash(keys)


def _digest_payload(value: Any) -> str:
    return digest(to_jsonable_python(value, fallback=str))


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() > deadline:
        raise AlphaError(AlphaErrorCode.RESOURCE_LIMIT_EXCEEDED, "Walk-forward time budget exceeded")


def _decision_sessions(features: pd.DataFrame, window: WalkForwardDevelopmentWindow) -> tuple[DecisionSession, ...]:
    decisions = sorted({_utc(value) for value in features.index.get_level_values("decision_at")})
    if not decisions:
        raise AlphaError(AlphaErrorCode.INSUFFICIENT_HISTORY, "Feature panel has no decision sessions")
    if decisions[0] < window.development_start or decisions[-1] >= window.development_end:
        raise AlphaError(AlphaErrorCode.DATASET_MISMATCH, "Decision session lies outside the development window")
    by_date: dict[str, list[datetime]] = {}
    for decision in decisions:
        by_date.setdefault(decision.date().isoformat(), []).append(decision)
    ambiguous = {date: values for date, values in by_date.items() if len(values) != 1}
    if ambiguous:
        raise AlphaError(
            AlphaErrorCode.DATASET_MISMATCH,
            "Each decision session must have exactly one decision timestamp",
            {"ambiguous_sessions": sorted(ambiguous)[:32]},
        )
    return tuple(DecisionSession(date, values[0]) for date, values in sorted(by_date.items()))


def plan_development_walk_forward(
    request: WalkForwardAlphaRequest,
    *,
    feature_frame: pd.DataFrame,
    feature_manifest: DatasetManifest,
) -> WalkForwardPlan:
    """Create a deterministic session-based plan without consulting label outcomes."""
    config = request.walk_forward_config
    development = request.development_window
    sessions = _decision_sessions(feature_frame, development)
    compiled = _validate_factor_manifest_source(request.baseline_comparison, feature_manifest)
    expected_schema = set(compiled.spec.outputs)
    actual_schema = set(map(str, feature_frame.columns))
    if actual_schema != expected_schema:
        raise AlphaError(
            AlphaErrorCode.DATASET_MISMATCH,
            "Feature panel columns do not match the validated FactorSpec outputs",
            {"missing_outputs": sorted(expected_schema - actual_schema),
             "unbound_outputs": sorted(actual_schema - expected_schema)},
        )
    label_definition = {
        **request.label_spec.model_dump(mode="json"),
        "label_source_dataset_id": request.stock_dataset_id,
    }
    label_definition_hash = digest(label_definition)
    protocol_hash = digest({
        "protocol_version": PROTOCOL_VERSION,
        "research_protocol_hash": request.research_protocol_hash,
        "feature_set_id": request.feature_set_id,
        "stock_dataset_id": request.stock_dataset_id,
        "factor_spec_hash": compiled.spec_hash,
        "feature_schema_hash": digest(sorted(actual_schema)),
        "label_definition_hash": label_definition_hash,
        "development_window": development.model_dump(mode="json"),
        "candidate_keys": list(CANDIDATE_KEYS),
        "pair_directions": [list(pair) for pair in PAIR_DIRECTIONS],
        "baseline_comparison": request.baseline_comparison.model_dump(mode="json"),
        "lightgbm_hyperparameters": request.lightgbm_hyperparameters,
        "random_seed": request.random_seed,
        "walk_forward_config": config.model_dump(mode="json"),
    })
    folds: list[PlannedFold] = []
    exclusions: list[dict] = []
    outer_start_index = config.train_sessions
    while outer_start_index + config.validation_sessions <= len(sessions):
        if len(folds) >= config.max_folds:
            raise AlphaError(
                AlphaErrorCode.RESOURCE_LIMIT_EXCEEDED,
                "Walk-forward plan exceeds max_folds",
                {"max_folds": config.max_folds, "complete_fold_count_exceeds_limit": True},
            )
        train_start_index = 0 if config.mode == "expanding" else outer_start_index - config.train_sessions
        train_end_index = outer_start_index
        maturity_buffer_sessions = max(1, request.label_spec.horizon_days)
        inner_tuning_start_index = outer_start_index - maturity_buffer_sessions - config.inner_validation_sessions
        inner_tuning_end_index = outer_start_index - maturity_buffer_sessions
        if inner_tuning_start_index <= train_start_index:
            raise AlphaError(
                AlphaErrorCode.INSUFFICIENT_HISTORY,
                "train_sessions cannot accommodate inner tuning plus the label-maturity buffer",
                {
                    "train_sessions": train_end_index - train_start_index,
                    "inner_validation_sessions": config.inner_validation_sessions,
                    "label_maturity_buffer_sessions": maturity_buffer_sessions,
                },
            )
        outer_end_index = outer_start_index + config.validation_sessions
        train_start = sessions[train_start_index].decision_at
        train_end = sessions[train_end_index].decision_at
        inner_tuning_start = sessions[inner_tuning_start_index].decision_at
        inner_tuning_end = sessions[inner_tuning_end_index].decision_at
        outer_start = sessions[outer_start_index].decision_at
        outer_end = sessions[outer_end_index].decision_at if outer_end_index < len(sessions) else development.development_end
        knowledge_cutoff = outer_start - timedelta(days=config.embargo_days)
        session_identity = [sessions[index].session_date for index in range(train_start_index, outer_end_index)]
        fold_id = digest({
            "protocol": protocol_hash,
            "mode": config.mode,
            "train": [_iso(train_start), _iso(train_end)],
            "inner_fit": [_iso(train_start), _iso(inner_tuning_start)],
            "inner_tuning": [_iso(inner_tuning_start), _iso(inner_tuning_end)],
            "outer_validation": [_iso(outer_start), _iso(outer_end)],
            "knowledge_cutoff": _iso(knowledge_cutoff),
            "embargo_days": config.embargo_days,
            "sessions": session_identity,
            "factor_spec_hash": compiled.spec_hash,
            "feature_schema": sorted(actual_schema),
            "label_definition_hash": label_definition_hash,
        })
        folds.append(PlannedFold(
            fold_id=fold_id,
            mode=config.mode,
            train_start=train_start,
            train_end=train_end,
            inner_fit_start=train_start,
            inner_tuning_start=inner_tuning_start,
            inner_tuning_end=inner_tuning_end,
            outer_validation_start=outer_start,
            outer_validation_end=outer_end,
            knowledge_cutoff=knowledge_cutoff,
            train_sessions=train_end_index - train_start_index,
            inner_fit_sessions=inner_tuning_start_index - train_start_index,
            inner_tuning_sessions=inner_tuning_end_index - inner_tuning_start_index,
            validation_sessions=outer_end_index - outer_start_index,
        ))
        outer_start_index += config.step_sessions
    if outer_start_index < len(sessions):
        exclusions.append({
            "reason": "INCOMPLETE_VALIDATION_WINDOW",
            "remaining_session_count": len(sessions) - outer_start_index,
            "required_validation_sessions": config.validation_sessions,
        })
    if len(folds) > config.max_folds:
        raise AlphaError(AlphaErrorCode.RESOURCE_LIMIT_EXCEEDED, "Walk-forward plan exceeds max_folds")
    fold_plan_hash = digest({
        "walk_forward_protocol_hash": protocol_hash,
        "fold_ids": [fold.fold_id for fold in folds],
        "sessions": [session.session_date for session in sessions],
        "exclusions": exclusions,
    })
    return WalkForwardPlan(
        walk_forward_protocol_hash=protocol_hash,
        label_definition_hash=label_definition_hash,
        fold_plan_hash=fold_plan_hash,
        sessions=sessions,
        folds=tuple(sorted(folds, key=lambda fold: fold.outer_validation_start)),
        exclusions=exclusions,
    )


def _label_frame(
    label_rows: Iterable[LabelRow],
    request: WalkForwardAlphaRequest,
) -> pd.DataFrame:
    rows = []
    for row in label_rows:
        if row.label_name != request.label_spec.label_name:
            continue
        if request.label_spec.label_version and row.label_version != request.label_spec.label_version:
            continue
        if not (request.development_window.development_start <= row.feature_decision_at < request.development_window.development_end):
            continue
        rows.append(row.model_dump(mode="python"))
    if not rows:
        raise AlphaError(AlphaErrorCode.DATA_UNAVAILABLE, "No labels match the Walk-forward label specification")
    frame = pd.DataFrame(rows)
    for column in ("feature_decision_at", "entry_at", "exit_at", "label_available_at"):
        frame[column] = pd.to_datetime(frame[column], utc=True)
    frame["decision_at"] = frame["feature_decision_at"]
    duplicate = frame.duplicated(["decision_at", "instrument_id"], keep=False)
    if duplicate.any():
        raise AlphaError(
            AlphaErrorCode.DATASET_MISMATCH,
            "Walk-forward label source contains duplicate sample identities",
            {"duplicate_count": int(duplicate.sum())},
        )
    return frame.sort_values(["decision_at", "instrument_id"], kind="stable").reset_index(drop=True)


def _feature_rows(features: pd.DataFrame) -> pd.DataFrame:
    frame = features.reset_index()
    frame["decision_at"] = pd.to_datetime(frame["decision_at"], utc=True)
    frame["instrument_id"] = frame["instrument_id"].astype(str)
    duplicate = frame.duplicated(["decision_at", "instrument_id"], keep=False)
    if duplicate.any():
        raise AlphaError(
            AlphaErrorCode.DATASET_MISMATCH,
            "Walk-forward feature panel contains duplicate sample identities",
            {"duplicate_count": int(duplicate.sum())},
        )
    return frame


def _subset(frame: pd.DataFrame, start: datetime, end: datetime) -> pd.DataFrame:
    return frame[(frame["decision_at"] >= pd.Timestamp(start)) & (frame["decision_at"] < pd.Timestamp(end))].copy()


def _feature_keys(frame: pd.DataFrame) -> set[tuple[datetime, str]]:
    return {
        (_utc(decision), str(instrument))
        for decision, instrument in frame[["decision_at", "instrument_id"]].itertuples(index=False, name=None)
    }


def _finite_label_mask(frame: pd.DataFrame) -> pd.Series:
    values = pd.to_numeric(frame["label_value"], errors="coerce")
    return values.notna() & np.isfinite(values.to_numpy(dtype=float, na_value=np.nan))


def _join_window(
    feature_window: pd.DataFrame,
    label_window: pd.DataFrame,
    *,
    label_available_before: datetime | None = None,
    exit_before: datetime | None = None,
) -> pd.DataFrame:
    labels = label_window[_finite_label_mask(label_window)].copy()
    if label_available_before is not None:
        labels = labels[labels["label_available_at"] < pd.Timestamp(label_available_before)]
    if exit_before is not None:
        labels = labels[labels["exit_at"] < pd.Timestamp(exit_before)]
    if labels.empty or feature_window.empty:
        return feature_window.iloc[0:0].merge(labels, how="inner", on=["decision_at", "instrument_id"])
    joined = feature_window.merge(
        labels[["decision_at", "instrument_id", "label_value", "exit_at", "label_available_at"]],
        on=["decision_at", "instrument_id"],
        how="inner",
        validate="one_to_one",
    )
    return joined[np.isfinite(joined["label_value"].to_numpy(dtype=float))].copy()


def _train_diagnostics(
    features: pd.DataFrame,
    labels: pd.DataFrame,
    *,
    train_start: datetime,
    train_end: datetime,
    outer_validation_start: datetime,
    knowledge_cutoff: datetime,
    eligible: pd.DataFrame,
) -> dict[str, int | float]:
    feature_keys = _feature_keys(_subset(features, train_start, train_end))
    labels_window = _subset(labels, train_start, train_end)
    finite = labels_window[_finite_label_mask(labels_window)].copy()
    finite_keys = {
        (_utc(decision), str(instrument))
        for decision, instrument in finite[["decision_at", "instrument_id"]].itertuples(index=False, name=None)
    }
    event_overlap = int((
        (finite["exit_at"] >= pd.Timestamp(outer_validation_start))
        | (finite["label_available_at"] >= pd.Timestamp(outer_validation_start))
    ).sum()) if not finite.empty else 0
    not_event_overlap = finite[
        (finite["exit_at"] < pd.Timestamp(outer_validation_start))
        & (finite["label_available_at"] < pd.Timestamp(outer_validation_start))
    ] if not finite.empty else finite
    embargo = int((
        (not_event_overlap["exit_at"] >= pd.Timestamp(knowledge_cutoff))
        | (not_event_overlap["label_available_at"] >= pd.Timestamp(knowledge_cutoff))
    ).sum()) if not not_event_overlap.empty else 0
    unmatured = int((finite["label_available_at"] >= pd.Timestamp(knowledge_cutoff)).sum()) if not finite.empty else 0
    eligible_keys = _feature_keys(eligible)
    return {
        "raw_train_labels": len(labels_window),
        "eligible_train_labels": len(eligible),
        "purged_event_overlap_count": event_overlap,
        "unmatured_label_count": unmatured,
        "embargo_exclusion_count": embargo,
        "null_label_count": int(len(labels_window) - len(finite)),
        "missing_feature_count": len(finite_keys - feature_keys),
        "eligible_train_decision_count": int(eligible["decision_at"].nunique()) if not eligible.empty else 0,
        "raw_train_feature_count": len(feature_keys),
        "eligible_train_label_keys_without_feature_count": len(eligible_keys - feature_keys),
    }


def _validation_diagnostics(
    features: pd.DataFrame,
    labels: pd.DataFrame,
    *,
    validation_start: datetime,
    validation_end: datetime,
    eligible: pd.DataFrame,
) -> dict[str, int | float]:
    feature_window = _subset(features, validation_start, validation_end)
    labels_window = _subset(labels, validation_start, validation_end)
    finite = labels_window[_finite_label_mask(labels_window)].copy()
    unmatched = _feature_keys(finite) - _feature_keys(feature_window)
    return {
        "raw_outer_validation_features": len(feature_window),
        "eligible_outer_validation_count": len(eligible),
        "scored_outer_validation_count": 0,
        "outer_validation_label_window_count": len(labels_window),
        "null_outer_validation_label_count": int(len(labels_window) - len(finite)),
        "unmatured_outer_validation_label_count": int(
            (finite["label_available_at"] >= pd.Timestamp(validation_end)).sum()
        ) if not finite.empty else 0,
        "missing_feature_count": len(unmatched),
    }


def _compact_metric_summary(value: dict) -> dict:
    return {
        key: item for key, item in value.items()
        if key not in {"rank_ic", "rank_ic_by_decision"}
    }


def _metric_bundle(
    targets: np.ndarray,
    predictions: np.ndarray,
    decisions: list,
    expected_decisions: list,
) -> dict:
    result = metrics(targets, predictions, decisions, expected_decisions=expected_decisions)
    result.update(quantile_returns(targets, predictions, decisions))
    return result


def _daily_ic_records(
    targets: np.ndarray,
    predictions: np.ndarray,
    decisions: list,
    expected_decisions: list,
) -> list[dict]:
    return rank_ic_by_decision(
        targets,
        predictions,
        decisions,
        expected_decisions=expected_decisions,
    )


def _coverage_records(
    decisions: list,
    predictions: np.ndarray,
    expected_decisions: list,
) -> list[dict]:
    groups: dict[Any, list[int]] = {}
    for index, decision in enumerate(decisions):
        groups.setdefault(decision, []).append(index)
    records = []
    for decision in sorted(expected_decisions):
        indexes = groups.get(decision, [])
        total = len(indexes)
        valid = int(np.isfinite(predictions[indexes]).sum()) if indexes else 0
        records.append({
            "decision_at": pd.Timestamp(decision).isoformat(),
            "total_count": total,
            "valid_prediction_count": valid,
            "coverage": valid / total if total else 0.0,
        })
    return records


def _paired_records(
    expected_decisions: list,
    candidate_records: dict[str, list[dict]],
    left: str,
    right: str,
) -> list[dict]:
    left_by_date = {row["decision_at"]: row for row in candidate_records[left]}
    right_by_date = {row["decision_at"]: row for row in candidate_records[right]}
    records = []
    for decision in sorted({pd.Timestamp(value).isoformat() for value in expected_decisions}):
        left_value = left_by_date.get(decision, {}).get("rank_ic")
        right_value = right_by_date.get(decision, {}).get("rank_ic")
        estimable = left_value is not None and right_value is not None
        records.append({
            "decision_at": decision,
            "left_rank_ic": left_value,
            "right_rank_ic": right_value,
            "delta_rank_ic": float(left_value - right_value) if estimable else None,
            "estimable": estimable,
            "reason": None if estimable else "PAIR_HAS_NULL_IC",
        })
    return records


def _paired_summary(records: list[dict]) -> dict:
    values = [row["delta_rank_ic"] for row in records if row["delta_rank_ic"] is not None]
    mean = float(np.mean(values)) if values else None
    std = float(np.std(values, ddof=1)) if len(values) >= 2 else None
    return {
        "mean_delta_rank_ic": mean,
        "delta_rank_ic_std": std,
        "positive_date_fraction": (
            sum(value > 0 for value in values) / len(values) if values else None
        ),
        "valid_date_count": len(values),
        "invalid_date_count": len(records) - len(values),
        "worst_delta_rank_ic": min(values) if values else None,
    }


def _bootstrap_seed(random_seed: int, protocol_hash: str) -> int:
    offset = int(protocol_hash.removeprefix("sha256:")[:16], 16)
    return int((random_seed & 0xFFFFFFFF) ^ (offset & 0xFFFFFFFF))


def _moving_block_indices(
    count: int,
    *,
    block_sessions: int,
    replicates: int,
    seed: int,
) -> list[list[int]]:
    if count == 0 or block_sessions < 1 or block_sessions > count:
        return []
    blocks_needed = math.ceil(count / block_sessions)
    start_max = count - block_sessions
    rng = np.random.default_rng(seed)
    sampled_indices: list[list[int]] = []
    for _ in range(replicates):
        sampled: list[int] = []
        for _ in range(blocks_needed):
            start = int(rng.integers(0, start_max + 1))
            sampled.extend(range(start, start + block_sessions))
        sampled_indices.append(sampled[:count])
    return sampled_indices


def _moving_block_draws(
    values: list[float | None],
    *,
    block_sessions: int,
    replicates: int,
    seed: int,
    sample_indices: list[list[int]] | None = None,
) -> list[float | None]:
    indices = sample_indices if sample_indices is not None else _moving_block_indices(
        len(values), block_sessions=block_sessions, replicates=replicates, seed=seed,
    )
    draws: list[float | None] = []
    for indexes in indices:
        finite = [values[index] for index in indexes if values[index] is not None and np.isfinite(values[index])]
        draws.append(float(np.mean(finite)) if finite else None)
    return draws


def _bootstrap_series(
    values: list[float | None],
    *,
    config: BootstrapInferenceConfig,
    seed: int,
    with_pvalue: bool = False,
    sample_indices: list[list[int]] | None = None,
) -> dict:
    finite = [float(value) for value in values if value is not None and np.isfinite(value)]
    point = float(np.mean(finite)) if finite else None
    base = {
        "point_estimate": point,
        "confidence_level": float(config.confidence_level),
        "bootstrap_method": "moving_block_bootstrap_v1",
        "bootstrap_block_sessions": config.bootstrap_block_sessions,
        "bootstrap_replicates_requested": config.bootstrap_replicates,
        "bootstrap_seed": seed,
        "bootstrap_block_selection_basis": "preregistered_request_no_validation_optimization",
        "valid_date_count": len(finite),
        "invalid_date_count": len(values) - len(finite),
    }
    if point is None:
        return {**base, "lower": None, "upper": None, "bootstrap_effective_replicate_count": 0,
                "p_value": None, "reason": "NO_VALID_DATES"}
    if len(values) < 2:
        return {**base, "lower": None, "upper": None, "bootstrap_effective_replicate_count": 0,
                "p_value": None, "reason": "INSUFFICIENT_DATE_HISTORY"}
    if config.bootstrap_block_sessions > len(values):
        raise AlphaError(
            AlphaErrorCode.DATASET_MISMATCH,
            "bootstrap_block_sessions exceeds the valid decision-session history",
            {"bootstrap_block_sessions": config.bootstrap_block_sessions, "session_count": len(values)},
        )
    draws = _moving_block_draws(
        values,
        block_sessions=config.bootstrap_block_sessions,
        replicates=config.bootstrap_replicates,
        seed=seed,
        sample_indices=sample_indices,
    )
    valid = [value for value in draws if value is not None]
    if len(valid) < 2:
        return {
            **base,
            "lower": None,
            "upper": None,
            "bootstrap_effective_replicate_count": len(valid),
            "p_value": None,
            "reason": "INSUFFICIENT_VALID_BOOTSTRAP_REPLICATES",
        }
    if max(valid) - min(valid) <= 1e-15:
        return {
            **base,
            "lower": None,
            "upper": None,
            "bootstrap_effective_replicate_count": len(valid),
            "p_value": None,
            "bootstrap_informative": False,
            "reason": "DEGENERATE_BOOTSTRAP_DISTRIBUTION",
        }
    alpha = (1.0 - float(config.confidence_level)) / 2.0
    lower = float(np.quantile(valid, alpha))
    upper = float(np.quantile(valid, 1.0 - alpha))
    p_value = None
    p_formula = None
    if with_pvalue:
        centered = [abs(value - point) for value in valid]
        exceedances = sum(value >= abs(point) for value in centered)
        p_value = float((1 + exceedances) / (len(valid) + 1))
        p_formula = "(1 + centered-null bootstrap exceedances) / (B_effective + 1)"
    return {
        **base,
        "lower": lower,
        "upper": upper,
        "bootstrap_effective_replicate_count": len(valid),
        "bootstrap_informative": True,
        "p_value": p_value,
        "p_value_formula": p_formula,
        "null_centering": "sampled_statistic_minus_observed" if with_pvalue else None,
    }


def holm_adjust(records: list[dict]) -> list[dict]:
    """Holm step-down, without shrinking the preregistered family for null tests."""
    family_size = len(records)
    estimable = sorted(
        [row for row in records if row.get("raw_p_value") is not None],
        key=lambda row: (float(row["raw_p_value"]), row["hypothesis_id"]),
    )
    previous = 0.0
    adjusted_by_id: dict[str, float] = {}
    for index, row in enumerate(estimable):
        multiplier = family_size - index
        value = min(1.0, max(previous, multiplier * float(row["raw_p_value"])))
        previous = value
        adjusted_by_id[row["hypothesis_id"]] = value
    result = []
    for row in records:
        result.append({
            **row,
            "family_size": family_size,
            "holm_adjusted_p_value": adjusted_by_id.get(row["hypothesis_id"]),
            "holm_policy": "fixed family; unestimable hypotheses remain in family",
        })
    return result


@dataclass
class _Budget:
    request: WalkForwardAlphaRequest
    checkpoint: Callable[[float], None] | None
    deadline: float
    physical_rows: int
    partitions: int
    training_samples: int = 0

    def tick(self, progress: float) -> None:
        _check_deadline(self.deadline)
        if self.checkpoint:
            self.checkpoint(progress)

    def consume_training(self, count: int) -> None:
        self.training_samples += int(count)
        if self.training_samples > self.request.resource_budget.max_total_training_samples:
            raise AlphaError(
                AlphaErrorCode.RESOURCE_LIMIT_EXCEEDED,
                "Walk-forward total training sample budget exceeded",
                {"training_samples": self.training_samples,
                 "max_total_training_samples": self.request.resource_budget.max_total_training_samples},
            )
        self.tick(min(0.9, 0.2 + self.training_samples / max(1, self.request.resource_budget.max_total_training_samples) * 0.6))


def _normalize_importance(values: dict[str, float]) -> dict[str, float]:
    total = sum(max(0.0, value) for value in values.values())
    if total <= 0:
        return {name: 0.0 for name in values}
    return {name: max(0.0, value) / total for name, value in values.items()}


def _fit_nested_lightgbm(
    inner_fit: pd.DataFrame,
    inner_tuning: pd.DataFrame,
    outer_train: pd.DataFrame,
    outer_validation: pd.DataFrame,
    schema: list[str],
    request: WalkForwardAlphaRequest,
) -> tuple[np.ndarray, str, int, dict[str, float]]:
    if inner_fit.empty or inner_tuning.empty:
        raise AlphaError(AlphaErrorCode.INSUFFICIENT_HISTORY, "Inner fit and tuning frames must be non-empty")
    if inner_fit["decision_at"].nunique() < 2 or inner_tuning["decision_at"].nunique() < 2:
        raise AlphaError(AlphaErrorCode.INSUFFICIENT_HISTORY, "Inner fit/tuning requires at least two decision dates")
    params, num_boost_round = lightgbm_parameters(
        request.lightgbm_hyperparameters, request.random_seed,
    )
    inner_train_data = lgb.Dataset(
        inner_fit[schema], label=inner_fit["label_value"], feature_name=schema, free_raw_data=False,
    )
    inner_tuning_data = lgb.Dataset(
        inner_tuning[schema], label=inner_tuning["label_value"], feature_name=schema,
        reference=inner_train_data, free_raw_data=False,
    )
    tuning_booster = lgb.train(
        params,
        inner_train_data,
        num_boost_round=num_boost_round,
        valid_sets=[inner_tuning_data],
        callbacks=[
            lgb.log_evaluation(0),
            lgb.early_stopping(max(1, min(20, num_boost_round // 5)), verbose=False),
        ],
    )
    selected_iteration = int(tuning_booster.best_iteration or 0)
    if selected_iteration < 1:
        raise AlphaError(AlphaErrorCode.INSUFFICIENT_HISTORY, "Inner validation selected no LightGBM iteration")
    outer_data = lgb.Dataset(
        outer_train[schema], label=outer_train["label_value"], feature_name=schema, free_raw_data=False,
    )
    final_booster = lgb.train(params, outer_data, num_boost_round=selected_iteration)
    predictions = np.asarray(
        final_booster.predict(outer_validation[schema], num_iteration=selected_iteration), dtype=float,
    )
    if not np.isfinite(predictions).all():
        raise AlphaError(AlphaErrorCode.DATASET_MISMATCH, "LightGBM produced non-finite outer-validation predictions")
    importance_values = final_booster.feature_importance(
        importance_type="gain", iteration=selected_iteration,
    )
    gain = {name: float(value) for name, value in zip(schema, importance_values, strict=True)}
    model_text = final_booster.model_to_string(num_iteration=selected_iteration)
    return predictions, bytes_hash(model_text.encode()), selected_iteration, _normalize_importance(gain)


def _pair_key(left: str, right: str) -> str:
    return f"{left}|{right}"


def _empty_model_hashes(fold_id: str, reason: str) -> dict[str, str]:
    return {key: digest({"fold_id": fold_id, "candidate": key, "status": reason}) for key in CANDIDATE_KEYS}


def _fold_failure(
    planned: PlannedFold,
    request: WalkForwardAlphaRequest,
    error: Exception,
) -> tuple[FoldRecord, list[dict], pd.DataFrame, pd.DataFrame, list]:
    code = getattr(error, "code", None)
    status = "FOLD_INSUFFICIENT_DATA" if code == AlphaErrorCode.INSUFFICIENT_HISTORY else "FOLD_INVALID"
    empty = digest([])
    payload = {
        "fold_id": planned.fold_id,
        "mode": planned.mode,
        "train_window": _window(planned.train_start, planned.train_end, planned.train_sessions),
        "inner_fit_window": _window(planned.inner_fit_start, planned.inner_tuning_start, planned.inner_fit_sessions),
        "inner_tuning_window": _window(planned.inner_tuning_start, planned.inner_tuning_end, planned.inner_tuning_sessions),
        "outer_validation_window": _window(planned.outer_validation_start, planned.outer_validation_end, planned.validation_sessions),
        "knowledge_cutoff": _iso(planned.knowledge_cutoff),
        "purge_policy": {
            "outer_train": "exit_at<knowledge_cutoff and label_available_at<knowledge_cutoff",
            "inner_fit": "exit_at<inner_tuning_start and label_available_at<inner_tuning_start",
            "outer_validation": "decision inside validation and label_available_at<validation_end",
            "strict_inequality": True,
            "embargo_calendar_days": request.walk_forward_config.embargo_days,
        },
        "train_identity_hash": empty,
        "inner_tuning_identity_hash": empty,
        "validation_identity_hash": empty,
        "common_cohort_identity_hash": empty,
        "model_hashes": _empty_model_hashes(planned.fold_id, status),
        "selected_iteration": None,
        "candidate_metrics": {},
        "paired_differences": {},
        "coverage": {},
        "exclusions": {
            "fold_error_code": getattr(code, "value", "FOLD_EXECUTION_INVALID"),
            "fold_error_message": str(getattr(error, "message", error))[:512],
        },
        "feature_importance": {"status": "UNAVAILABLE", "reason": "FOLD_NOT_COMPLETED"},
        "fold_status": status,
    }
    record = FoldRecord.model_validate({**payload, "fold_hash": _digest_payload(payload)})
    diagnostic = {
        "record_type": "fold_status",
        "fold_id": planned.fold_id,
        "fold_status": status,
        "reason": payload["exclusions"]["fold_error_code"],
        "message": payload["exclusions"]["fold_error_message"],
    }
    return record, [diagnostic], pd.DataFrame(), pd.DataFrame(), []


def _execute_fold(
    planned: PlannedFold,
    *,
    features: pd.DataFrame,
    labels: pd.DataFrame,
    request: WalkForwardAlphaRequest,
    budget: _Budget,
) -> tuple[FoldRecord, list[dict], pd.DataFrame, pd.DataFrame, list]:
    budget.tick(0.2)
    feature_train = _subset(features, planned.train_start, planned.train_end)
    label_train = _subset(labels, planned.train_start, planned.train_end)
    eligible_train = _join_window(
        feature_train,
        label_train,
        label_available_before=planned.knowledge_cutoff,
        exit_before=planned.knowledge_cutoff,
    )
    train_counts = _train_diagnostics(
        features,
        labels,
        train_start=planned.train_start,
        train_end=planned.train_end,
        outer_validation_start=planned.outer_validation_start,
        knowledge_cutoff=planned.knowledge_cutoff,
        eligible=eligible_train,
    )
    inner_fit = _subset(eligible_train, planned.inner_fit_start, planned.inner_tuning_start)
    inner_fit = inner_fit[
        (inner_fit["exit_at"] < pd.Timestamp(planned.inner_tuning_start))
        & (inner_fit["label_available_at"] < pd.Timestamp(planned.inner_tuning_start))
    ].copy()
    inner_tuning = _subset(eligible_train, planned.inner_tuning_start, planned.inner_tuning_end)
    feature_validation = _subset(features, planned.outer_validation_start, planned.outer_validation_end)
    label_validation = _subset(labels, planned.outer_validation_start, planned.outer_validation_end)
    eligible_validation = _join_window(
        feature_validation,
        label_validation,
        label_available_before=planned.outer_validation_end,
        exit_before=planned.outer_validation_end,
    )
    validation_counts = _validation_diagnostics(
        features,
        labels,
        validation_start=planned.outer_validation_start,
        validation_end=planned.outer_validation_end,
        eligible=eligible_validation,
    )
    budget.consume_training(len(eligible_train))
    expected_decisions = sorted(feature_validation["decision_at"].unique().tolist())
    if eligible_train.empty:
        raise AlphaError(AlphaErrorCode.INSUFFICIENT_HISTORY, "No eligible matured outer-train labels")
    if inner_fit.empty or inner_tuning.empty:
        raise AlphaError(AlphaErrorCode.INSUFFICIENT_HISTORY, "No eligible matured inner fit/tuning labels")
    if eligible_validation.empty or not expected_decisions:
        raise AlphaError(AlphaErrorCode.INSUFFICIENT_HISTORY, "No eligible matured outer-validation labels")
    schema = list(features.columns.difference({"decision_at", "instrument_id", "feature_available_at"}))
    schema = sorted(name for name in schema if not str(name).startswith("label_"))
    predictions, model_hashes, selected_iteration, importance = _fit_models(
        planned, request, eligible_train, inner_fit, inner_tuning, eligible_validation, schema,
    )
    candidate_records, paired, diagnostics, common_count, candidate_frame, common_frame = _score_fold(
        planned,
        eligible_validation,
        predictions,
        expected_decisions,
        importance,
    )
    coverage = {
        "validation_sample_count": len(eligible_validation),
        "validation_decision_count": len(expected_decisions),
        "common_cohort_sample_count": int(common_count),
        "common_cohort_coverage": common_count / len(eligible_validation) if len(eligible_validation) else 0.0,
    }
    validation_counts["scored_outer_validation_count"] = int(common_count)
    exclusions = {**train_counts, **validation_counts}
    for key, values in predictions.items():
        exclusions[f"{key}_missing_prediction_count"] = int((~np.isfinite(values)).sum())
    payload = {
        "fold_id": planned.fold_id,
        "mode": planned.mode,
        "train_window": _window(planned.train_start, planned.train_end, planned.train_sessions),
        "inner_fit_window": _window(planned.inner_fit_start, planned.inner_tuning_start, planned.inner_fit_sessions),
        "inner_tuning_window": _window(planned.inner_tuning_start, planned.inner_tuning_end, planned.inner_tuning_sessions),
        "outer_validation_window": _window(planned.outer_validation_start, planned.outer_validation_end, planned.validation_sessions),
        "knowledge_cutoff": _iso(planned.knowledge_cutoff),
        "purge_policy": {
            "outer_train": "exit_at<knowledge_cutoff and label_available_at<knowledge_cutoff",
            "inner_fit": "exit_at<inner_tuning_start and label_available_at<inner_tuning_start",
            "outer_validation": "decision inside validation and label_available_at<validation_end",
            "strict_inequality": True,
            "embargo_calendar_days": request.walk_forward_config.embargo_days,
        },
        "train_identity_hash": _sample_hash(eligible_train),
        "inner_tuning_identity_hash": _sample_hash(inner_tuning),
        "validation_identity_hash": _sample_hash(eligible_validation),
        "common_cohort_identity_hash": _sample_hash(common_frame),
        "model_hashes": model_hashes,
        "selected_iteration": selected_iteration,
        "candidate_metrics": candidate_records,
        "paired_differences": paired,
        "coverage": coverage,
        "exclusions": exclusions,
        "feature_importance": {
            "status": "AVAILABLE",
            "lightgbm_v1": importance,
            "importance_type": "gain",
            "iteration": selected_iteration,
        },
        "fold_status": "FOLD_COMPLETED",
    }
    record = FoldRecord.model_validate({**payload, "fold_hash": _digest_payload(payload)})
    return record, diagnostics, candidate_frame, common_frame, expected_decisions


def _fit_models(
    planned: PlannedFold,
    request: WalkForwardAlphaRequest,
    outer_train: pd.DataFrame,
    inner_fit: pd.DataFrame,
    inner_tuning: pd.DataFrame,
    outer_validation: pd.DataFrame,
    schema: list[str],
) -> tuple[dict[str, np.ndarray], dict[str, str], int | None, dict[str, float]]:
    factors = fixed_factor_scores(outer_validation, request.baseline_comparison)
    _, ridge_predictions, ridge_hash = fit_train_only_ridge(
        outer_train, outer_validation, tuple(schema), float(request.baseline_comparison.ridge_alpha),
    )
    lgb_predictions, lgb_hash, iteration, importance = _fit_nested_lightgbm(
        inner_fit, inner_tuning, outer_train, outer_validation, schema, request,
    )
    model_hashes = {
        "short_reversal_5": digest({
            "fold_id": planned.fold_id,
            "factor_spec_hash": digest(request.baseline_comparison.factor_spec.model_dump(mode="json")),
            "node": request.baseline_comparison.reversal_output,
            "direction": "-pct_change",
            "train_identity_hash": _sample_hash(outer_train),
        }),
        "rsi_5": digest({
            "fold_id": planned.fold_id,
            "factor_spec_hash": digest(request.baseline_comparison.factor_spec.model_dump(mode="json")),
            "node": request.baseline_comparison.rsi_output,
            "direction": "50-rsi",
            "train_identity_hash": _sample_hash(outer_train),
        }),
        "ridge_v1": digest({
            "fold_id": planned.fold_id,
            "fitted_model_hash": ridge_hash,
            "train_identity_hash": _sample_hash(outer_train),
            "alpha": float(request.baseline_comparison.ridge_alpha),
        }),
        "lightgbm_v1": digest({
            "fold_id": planned.fold_id,
            "model_hash": lgb_hash,
            "selected_iteration": iteration,
            "train_identity_hash": _sample_hash(outer_train),
            "inner_fit_identity_hash": _sample_hash(inner_fit),
            "inner_tuning_identity_hash": _sample_hash(inner_tuning),
        }),
    }
    return {
        "short_reversal_5": factors["short_reversal_5"],
        "rsi_5": factors["rsi_5"],
        "ridge_v1": ridge_predictions,
        "lightgbm_v1": lgb_predictions,
    }, model_hashes, iteration, importance


def _score_fold(
    planned: PlannedFold,
    validation: pd.DataFrame,
    predictions: dict[str, np.ndarray],
    expected_decisions: list,
    importance: dict[str, float],
) -> tuple[dict, dict, list[dict], int, pd.DataFrame, pd.DataFrame]:
    targets = validation["label_value"].to_numpy(dtype=float)
    decisions = validation["decision_at"].tolist()
    common_mask = np.logical_and.reduce([np.isfinite(predictions[key]) for key in CANDIDATE_KEYS])
    candidate_frame = validation[["decision_at", "instrument_id", "label_value"]].reset_index(drop=True).copy()
    for candidate in CANDIDATE_KEYS:
        candidate_frame[candidate] = np.asarray(predictions[candidate], dtype=float)
    common = validation.loc[common_mask].reset_index(drop=True)
    common_frame = candidate_frame.loc[common_mask].reset_index(drop=True)
    common_targets = common["label_value"].to_numpy(dtype=float)
    common_decisions = common["decision_at"].tolist()
    common_expected = sorted(expected_decisions)
    candidate_metrics = {}
    daily: dict[str, list[dict]] = {}
    diagnostics: list[dict] = []
    for candidate in CANDIDATE_KEYS:
        independent = _metric_bundle(targets, predictions[candidate], decisions, expected_decisions)
        common_metric = _metric_bundle(common_targets, predictions[candidate][common_mask], common_decisions, common_expected)
        daily[candidate] = _daily_ic_records(
            common_targets, predictions[candidate][common_mask], common_decisions, common_expected,
        )
        valid_predictions = int(np.isfinite(predictions[candidate]).sum())
        coverage_rows = _coverage_records(decisions, predictions[candidate], expected_decisions)
        candidate_metrics[candidate] = {
            "independent": _compact_metric_summary(independent),
            "common_cohort": {
                **_compact_metric_summary(common_metric),
                "rank_ic_by_decision": daily[candidate][:MAX_DECISION_DIAGNOSTICS],
                "omitted_decision_records": max(0, len(daily[candidate]) - MAX_DECISION_DIAGNOSTICS),
            },
            "coverage_by_decision": coverage_rows[:MAX_DECISION_DIAGNOSTICS],
            "omitted_coverage_decisions": max(0, len(coverage_rows) - MAX_DECISION_DIAGNOSTICS),
            "valid_prediction_count": valid_predictions,
            "prediction_coverage": valid_predictions / len(validation) if len(validation) else 0.0,
        }
        for row in coverage_rows:
            diagnostics.append({
                "record_type": "coverage",
                "fold_id": planned.fold_id,
                "candidate": candidate,
                **row,
            })
        for row in daily[candidate]:
            diagnostics.append({
                "record_type": "candidate_ic",
                "scope": "common_cohort",
                "fold_id": planned.fold_id,
                "candidate": candidate,
                **row,
            })
    paired = {}
    for left, right in PAIR_DIRECTIONS:
        records = _paired_records(common_expected, daily, left, right)
        paired[_pair_key(left, right)] = {
            **_paired_summary(records),
            "daily": records[:MAX_DECISION_DIAGNOSTICS],
            "omitted_daily_records": max(0, len(records) - MAX_DECISION_DIAGNOSTICS),
        }
        for row in records:
            diagnostics.append({
                "record_type": "paired_ic",
                "fold_id": planned.fold_id,
                "left_candidate": left,
                "right_candidate": right,
                **row,
            })
    for feature, value in importance.items():
        diagnostics.append({
            "record_type": "feature_importance",
            "fold_id": planned.fold_id,
            "candidate": "lightgbm_v1",
            "feature": feature,
            "normalized_gain": value,
        })
    return candidate_metrics, paired, diagnostics, int(common_mask.sum()), candidate_frame, common_frame


def _software_versions() -> dict[str, str]:
    return {
        "python": __import__("sys").version,
        "lightgbm": version("lightgbm"),
        "scikit_learn": version("scikit-learn"),
        "numpy": version("numpy"),
        "pandas": version("pandas"),
        "scipy": version("scipy"),
    }


def _quality_summary(report) -> dict:
    return {
        "dataset_id": report.dataset_id,
        "dataset_kind": report.dataset_kind,
        "policy_version": report.policy_version,
        "research_readiness": report.research_readiness,
        "report_hash": report.report_hash,
        "dimensions": {name: dimension.status for name, dimension in report.dimensions.items()},
        "finding_codes": sorted({finding.code for finding in report.findings}),
        "blocking_finding_codes": sorted(
            {finding.code for finding in report.findings if finding.severity == "BLOCKING"}
        ),
    }


def _planned_validation_decisions(plan: WalkForwardPlan) -> list[datetime]:
    values: set[datetime] = set()
    for fold in plan.folds:
        values.update(
            session.decision_at
            for session in plan.sessions
            if fold.outer_validation_start <= session.decision_at < fold.outer_validation_end
        )
    return sorted(values)


def _concat_frames(frames: list[pd.DataFrame], *, label: str) -> pd.DataFrame:
    nonempty = [frame for frame in frames if not frame.empty]
    if not nonempty:
        return pd.DataFrame()
    result = pd.concat(nonempty, ignore_index=True)
    duplicate = result.duplicated(["decision_at", "instrument_id"], keep=False)
    if duplicate.any():
        raise AlphaError(
            AlphaErrorCode.DATASET_MISMATCH,
            f"{label} contains duplicate OOF sample identities across folds",
            {"duplicate_count": int(duplicate.sum())},
        )
    return result


def _pooled_metrics(
    independent: pd.DataFrame,
    common: pd.DataFrame,
    expected_decisions: list[datetime],
    request: WalkForwardAlphaRequest,
    protocol_hash: str,
) -> tuple[dict, dict, list[dict], dict, list[dict]]:
    independent = independent.sort_values(["decision_at", "instrument_id"], kind="stable").reset_index(drop=True) if not independent.empty else independent
    common = common.sort_values(["decision_at", "instrument_id"], kind="stable").reset_index(drop=True) if not common.empty else common
    independence_metrics = {}
    common_metrics = {}
    candidate_daily: dict[str, list[dict]] = {}
    pair_daily: dict[str, list[dict]] = {}
    for candidate in CANDIDATE_KEYS:
        independent_value = _metric_bundle(
            independent["label_value"].to_numpy(dtype=float) if not independent.empty else np.array([]),
            independent[candidate].to_numpy(dtype=float) if not independent.empty else np.array([]),
            independent["decision_at"].tolist() if not independent.empty else [],
            expected_decisions,
        ) if not independent.empty else metrics(np.array([]), np.array([]), [], expected_decisions=expected_decisions)
        common_value = _metric_bundle(
            common["label_value"].to_numpy(dtype=float) if not common.empty else np.array([]),
            common[candidate].to_numpy(dtype=float) if not common.empty else np.array([]),
            common["decision_at"].tolist() if not common.empty else [],
            expected_decisions,
        ) if not common.empty else metrics(np.array([]), np.array([]), [], expected_decisions=expected_decisions)
        independence_metrics[candidate] = {
            **_compact_metric_summary(independent_value),
            "valid_prediction_count": int(np.isfinite(independent[candidate]).sum()) if candidate in independent else 0,
            "prediction_coverage": (
                float(np.isfinite(independent[candidate]).mean()) if not independent.empty else 0.0
            ),
        }
        common_metrics[candidate] = _compact_metric_summary(common_value)
        candidate_daily[candidate] = _daily_ic_records(
            common["label_value"].to_numpy(dtype=float) if not common.empty else np.array([]),
            common[candidate].to_numpy(dtype=float) if not common.empty else np.array([]),
            common["decision_at"].tolist() if not common.empty else [],
            expected_decisions,
        )
    for left, right in PAIR_DIRECTIONS:
        records = _paired_records(expected_decisions, candidate_daily, left, right)
        pair_daily[_pair_key(left, right)] = records
    seed = _bootstrap_seed(request.random_seed, protocol_hash)
    inference = request.walk_forward_config.inference
    sample_indices = _moving_block_indices(
        len(expected_decisions),
        block_sessions=inference.bootstrap_block_sessions,
        replicates=inference.bootstrap_replicates,
        seed=seed,
    ) if len(expected_decisions) >= 2 else []
    candidate_uncertainty = {}
    for candidate in CANDIDATE_KEYS:
        values = [row["rank_ic"] for row in candidate_daily[candidate]]
        candidate_uncertainty[candidate] = _bootstrap_series(
            values, config=inference, seed=seed, sample_indices=sample_indices,
        )
    pair_uncertainty = {}
    for left, right in PAIR_DIRECTIONS:
        key = _pair_key(left, right)
        values = [row["delta_rank_ic"] for row in pair_daily[key]]
        pair_uncertainty[key] = _bootstrap_series(
            values, config=inference, seed=seed, with_pvalue=True, sample_indices=sample_indices,
        )
    total_opportunities = len(independent) if not independent.empty else 0
    common_summary = {
        "sample_identity": "(decision_at, instrument_id)",
        "expected_decision_count": len(expected_decisions),
        "validation_opportunity_count": total_opportunities,
        "common_cohort_sample_count": len(common),
        "common_cohort_coverage": len(common) / total_opportunities if total_opportunities else 0.0,
        "rule": "finite prediction for all four candidates on the same decision and instrument",
        "duplicate_oof_sample_count": 0,
    }
    return (
        {"independent": independence_metrics, "common_cohort": common_metrics},
        common_summary,
        candidate_daily,
        {
            "candidate_rank_ic": candidate_uncertainty,
            "paired_mean_delta_rank_ic": pair_uncertainty,
            "seed": seed,
            "shared_row_resampling": True,
            "config": inference.model_dump(mode="json"),
        },
        pair_daily,
    )


def _median_or_none(values: list[float]) -> float | None:
    return float(np.median(values)) if values else None


def _std_or_none(values: list[float]) -> float | None:
    return float(np.std(values, ddof=1)) if len(values) >= 2 else None


def _time_decay(values: list[tuple[str, float]]) -> dict:
    if not values:
        return {
            "rule": "fixed_chronological_thirds", "early": None, "middle": None, "late": None,
            "late_minus_early": None, "recent_fold_rank_ic": None, "fold_series": [],
        }
    groups = np.array_split(np.arange(len(values)), 3)
    means = []
    for indexes in groups:
        group_values = [values[int(index)][1] for index in indexes]
        means.append(float(np.mean(group_values)) if group_values else None)
    return {
        "rule": "fixed_chronological_thirds",
        "early": means[0],
        "middle": means[1],
        "late": means[2],
        "late_minus_early": (
            means[2] - means[0] if means[0] is not None and means[2] is not None else None
        ),
        "recent_fold_rank_ic": values[-1][1],
        "fold_series": [{"fold_id": fold_id, "rank_ic_mean": value} for fold_id, value in values],
    }


def _importance_stability(folds: list[FoldRecord]) -> dict:
    available = [
        fold for fold in folds
        if fold.fold_status == "FOLD_COMPLETED"
        and fold.feature_importance.get("status") == "AVAILABLE"
        and fold.feature_importance.get("lightgbm_v1")
    ]
    correlations = []
    details = []
    for left, right in pairwise(available):
        left_values = left.feature_importance["lightgbm_v1"]
        right_values = right.feature_importance["lightgbm_v1"]
        names = sorted(set(left_values) & set(right_values))
        if len(names) < 2:
            details.append({
                "left_fold_id": left.fold_id, "right_fold_id": right.fold_id,
                "spearman": None, "reason": "INSUFFICIENT_SHARED_FEATURES",
            })
            continue
        correlation = spearmanr([left_values[name] for name in names], [right_values[name] for name in names]).statistic
        value = float(correlation) if correlation is not None and np.isfinite(correlation) else None
        if value is not None:
            correlations.append(value)
        details.append({
            "left_fold_id": left.fold_id,
            "right_fold_id": right.fold_id,
            "spearman": value,
            "reason": None if value is not None else "CONSTANT_OR_NONFINITE_IMPORTANCE",
        })
    status = (
        "INSUFFICIENT_FOLD_EVIDENCE" if len(available) < 2
        else "STABLE_ACROSS_OBSERVED_FOLDS"
        if correlations and min(correlations) >= 0.70
        else "UNSTABLE_ACROSS_OBSERVED_FOLDS"
    )
    return {
        "status": status,
        "available_fold_count": len(available),
        "minimum_adjacent_spearman": min(correlations) if correlations else None,
        "adjacent_spearman": details,
        "importance_type": "gain",
        "normalization": "non-negative_gain_share_per_fold",
    }


def _stability(folds: list[FoldRecord], pair_daily: dict[str, list[dict]]) -> dict:
    candidates = {}
    for candidate in CANDIDATE_KEYS:
        series = []
        invalid = []
        for fold in folds:
            metric = fold.candidate_metrics.get(candidate, {}).get("common_cohort", {}) if fold.candidate_metrics else {}
            value = metric.get("rank_ic_mean")
            if fold.fold_status == "FOLD_COMPLETED" and value is not None:
                series.append((fold.fold_id, float(value)))
            else:
                invalid.append({"fold_id": fold.fold_id, "fold_status": fold.fold_status, "reason": "INVALID_OR_NULL_FOLD_IC"})
        values = [value for _, value in series]
        positive_fraction = sum(value > 0 for value in values) / len(values) if values else None
        std = _std_or_none(values)
        if len(values) < 2:
            conclusion = "INSUFFICIENT_FOLD_EVIDENCE"
        elif positive_fraction is not None and positive_fraction >= STABILITY_MIN_POSITIVE_FRACTION and std is not None and std <= STABILITY_MAX_FOLD_STD:
            conclusion = "STABLE_ACROSS_OBSERVED_FOLDS"
        else:
            conclusion = "UNSTABLE_ACROSS_OBSERVED_FOLDS"
        candidates[candidate] = {
            "fold_rank_ic_mean": float(np.mean(values)) if values else None,
            "fold_rank_ic_median": _median_or_none(values),
            "fold_rank_ic_std": std,
            "positive_fold_fraction": positive_fraction,
            "worst_fold_rank_ic": min(values) if values else None,
            "valid_fold_count": len(values),
            "invalid_fold_count": len(invalid),
            "fold_ic_series": [{"fold_id": fold_id, "rank_ic_mean": value} for fold_id, value in series],
            "invalid_folds": invalid,
            "time_decay": _time_decay(series),
            "descriptive_conclusion": conclusion,
        }
    pair_stability = {}
    for left, right in PAIR_DIRECTIONS:
        key = _pair_key(left, right)
        fold_values = []
        for fold in folds:
            value = fold.paired_differences.get(key, {}).get("mean_delta_rank_ic")
            if fold.fold_status == "FOLD_COMPLETED" and value is not None:
                fold_values.append((fold.fold_id, float(value)))
        daily_values = [row["delta_rank_ic"] for row in pair_daily[key] if row["delta_rank_ic"] is not None]
        values = [value for _, value in fold_values]
        pair_stability[key] = {
            "mean_delta_ic": float(np.mean(daily_values)) if daily_values else None,
            "positive_date_fraction": (
                sum(value > 0 for value in daily_values) / len(daily_values) if daily_values else None
            ),
            "positive_fold_fraction": (
                sum(value > 0 for value in values) / len(values) if values else None
            ),
            "worst_fold_delta_ic": min(values) if values else None,
            "fold_to_fold_std": _std_or_none(values),
            "fold_series": [{"fold_id": fold_id, "mean_delta_rank_ic": value} for fold_id, value in fold_values],
        }
    return {
        "policy": {
            "time_decay_split": "fixed_chronological_thirds",
            "stability_min_positive_fraction": STABILITY_MIN_POSITIVE_FRACTION,
            "stability_max_fold_std": STABILITY_MAX_FOLD_STD,
        },
        "candidates": candidates,
        "paired_comparisons": pair_stability,
        "feature_importance": _importance_stability(folds),
        "interpretation": "Development-period fold diagnostics only; not a trading or independent-OOS certification.",
    }


def _preflight(request: WalkForwardAlphaRequest, store: DatasetStore) -> tuple[dict[str, DatasetManifest], int, int]:
    feature_manifest = store.load_manifest(request.feature_set_id)
    _validate_factor_manifest_source(request.baseline_comparison, feature_manifest)
    manifests = _lineage_manifests(
        store,
        [request.feature_set_id, request.stock_dataset_id],
        max_nodes=request.resource_budget.max_lineage_nodes,
    )
    partitions = sum(len(manifest.partition_manifest) for manifest in manifests.values())
    physical_rows = sum(
        partition.row_count for manifest in manifests.values() for partition in manifest.partition_manifest
    )
    if partitions > request.resource_budget.max_partitions:
        raise AlphaError(
            AlphaErrorCode.RESOURCE_LIMIT_EXCEEDED,
            "Walk-forward lineage partition budget exceeded",
            {"partitions": partitions, "max_partitions": request.resource_budget.max_partitions},
        )
    if physical_rows > request.resource_budget.max_total_physical_rows:
        raise AlphaError(
            AlphaErrorCode.RESOURCE_LIMIT_EXCEEDED,
            "Walk-forward physical row budget exceeded",
            {"physical_rows": physical_rows, "max_total_physical_rows": request.resource_budget.max_total_physical_rows},
        )
    cutoff = request.development_window.development_end
    forbidden = [
        {"dataset_id": dataset_id, "end_at": manifest.end_at.isoformat()}
        for dataset_id, manifest in manifests.items()
        if manifest.end_at > cutoff
    ]
    if forbidden:
        raise AlphaError(
            AlphaErrorCode.QUALITY_SCOPE_NOT_ISOLATED,
            "Walk-forward dataset lineage extends beyond development_end",
            {"development_end": cutoff.isoformat(), "forbidden_end_times": forbidden[:32], "forbidden_count": len(forbidden)},
        )
    for dataset_id, manifest in manifests.items():
        _assert_parquet_time_isolated(store, dataset_id, manifest, cutoff)
    return manifests, partitions, physical_rows


def _read_generated_labels(request: WalkForwardAlphaRequest, store: DatasetStore, stock_manifest: DatasetManifest) -> list[LabelRow]:
    bars = store.read_rows(
        request.stock_dataset_id,
        start_at=request.development_window.development_start,
        end_at=request.development_window.development_end,
    )
    return stock_forward_labels(
        bars,
        horizon=request.label_spec.horizon_days,
        calendar=mapped_stock_calendar(stock_manifest),
    )


def _quality_reports(request: WalkForwardAlphaRequest, store: DatasetStore, budget: _Budget) -> tuple[dict, bool]:
    limits = ResearchQualityLimits(
        max_scan_rows=max(1, min(1_000_000, request.resource_budget.max_total_physical_rows // 4)),
        max_parent_depth=32,
        max_partitions=request.resource_budget.max_partitions,
        max_nodes=request.resource_budget.max_lineage_nodes,
        max_findings=128,
    )
    budget.tick(0.15)
    feature = assess_research_quality(request.feature_set_id, store, limits=limits)
    budget.tick(0.18)
    labels = assess_research_quality(request.stock_dataset_id, store, limits=limits)
    blocked = feature.research_readiness == "RESEARCH_BLOCKED" or labels.research_readiness == "RESEARCH_BLOCKED"
    exploratory = feature.research_readiness != "RESEARCH_DATA_VALIDATED" or labels.research_readiness != "RESEARCH_DATA_VALIDATED"
    return {
        "scope": "development_only",
        "physical_time_isolation": "PARQUET_FOOTER_VERIFIED",
        "feature": _quality_summary(feature),
        "label_source": _quality_summary(labels),
    }, blocked or exploratory


def run_development_walk_forward(
    request: WalkForwardAlphaRequest,
    *,
    store: DatasetStore,
    label_rows: list[LabelRow] | None = None,
    checkpoint: Callable[[float], None] | None = None,
) -> WalkForwardReport:
    """Execute the sole leakage-safe development walk-forward implementation."""
    require_walk_forward_execution(request)
    if request.walk_forward_config.inference.bootstrap_replicates > request.resource_budget.max_bootstrap_replicates:
        raise AlphaError(AlphaErrorCode.RESOURCE_LIMIT_EXCEEDED, "Bootstrap replicate budget exceeded")
    deadline = time.monotonic() + min(request.max_seconds, request.resource_budget.max_seconds)
    manifests, partitions, physical_rows = _preflight(request, store)
    budget = _Budget(
        request=request, checkpoint=checkpoint, deadline=deadline,
        physical_rows=physical_rows, partitions=partitions,
    )
    feature_manifest = manifests[request.feature_set_id]
    stock_manifest = manifests[request.stock_dataset_id]
    budget.tick(0.05)
    feature_rows = store.read_rows(
        request.feature_set_id,
        start_at=request.development_window.development_start,
        end_at=request.development_window.development_end,
    )
    feature_frame, _schema = _feature_frame(feature_rows)
    features = _feature_rows(feature_frame)
    plan = plan_development_walk_forward(request, feature_frame=feature_frame, feature_manifest=feature_manifest)
    budget.tick(0.12)
    if label_rows is None:
        label_rows = _read_generated_labels(request, store, stock_manifest)
    labels = _label_frame(label_rows, request)
    data_quality, exploratory = _quality_reports(request, store, budget)
    completed_frames: list[pd.DataFrame] = []
    common_frames: list[pd.DataFrame] = []
    diagnostics: list[dict] = []
    folds: list[FoldRecord] = []
    expected_decisions = _planned_validation_decisions(plan)
    for index, planned in enumerate(plan.folds):
        try:
            record, fold_diagnostics, candidate_frame, common_frame, _ = _execute_fold(
                planned,
                features=features,
                labels=labels,
                request=request,
                budget=budget,
            )
            if not candidate_frame.empty:
                candidate_frame["fold_id"] = record.fold_id
                completed_frames.append(candidate_frame)
            if not common_frame.empty:
                common_frame["fold_id"] = record.fold_id
                common_frames.append(common_frame)
            diagnostics.extend(fold_diagnostics)
            folds.append(record)
        except AlphaError as error:
            if error.code == AlphaErrorCode.RESOURCE_LIMIT_EXCEEDED:
                raise
            record, fold_diagnostics, _, _, _ = _fold_failure(planned, request, error)
            folds.append(record)
            diagnostics.extend(fold_diagnostics)
        except (ValueError, TypeError, RuntimeError, lgb.basic.LightGBMError) as error:
            record, fold_diagnostics, _, _, _ = _fold_failure(planned, request, error)
            folds.append(record)
            diagnostics.extend(fold_diagnostics)
        budget.tick(0.2 + 0.6 * (index + 1) / max(1, len(plan.folds)))
    independent = _concat_frames(completed_frames, label="Independent OOF")
    common = _concat_frames(common_frames, label="Common cohort OOF")
    budget.tick(0.85)
    pooled, common_summary, _candidate_daily, uncertainty, pair_daily = _pooled_metrics(
        independent, common, expected_decisions, request, plan.walk_forward_protocol_hash,
    )
    budget.tick(0.93)
    stability = _stability(folds, pair_daily)
    trial_records = []
    for left, right in PAIR_DIRECTIONS:
        key = _pair_key(left, right)
        result = uncertainty["paired_mean_delta_rank_ic"][key]
        trial_records.append({
            "hypothesis_id": PAIR_IDS[key],
            "left_candidate": left,
            "right_candidate": right,
            "direction": f"{left} - {right}",
            "statistic": "mean_paired_rank_ic_difference",
            "point_estimate": result["point_estimate"],
            "confidence_interval": [result["lower"], result["upper"]],
            "raw_p_value": result.get("p_value"),
            "estimable": result["point_estimate"] is not None,
            "not_estimable_reason": result.get("reason"),
            "bootstrap_effective_replicate_count": result["bootstrap_effective_replicate_count"],
        })
    trials = holm_adjust(trial_records)
    multiple_testing = {
        "method": "holm",
        "family_size": 4,
        "alpha": 1.0 - float(request.walk_forward_config.inference.confidence_level),
        "confidence_level": float(request.walk_forward_config.inference.confidence_level),
        "multiple_testing_scope": "CURRENT_PREREGISTERED_FAMILY_ONLY",
        "global_trial_history_complete": False,
        "hypotheses": trials,
        "unestimable_policy": "retain in fixed family; do not shrink family size",
    }
    paired_comparisons = [
        {
            "hypothesis_id": row["hypothesis_id"],
            **row,
            "confidence_interval": row["confidence_interval"],
            "holm_adjusted_p_value": row["holm_adjusted_p_value"],
        }
        for row in trials
    ]
    completed = [fold for fold in folds if fold.fold_status == "FOLD_COMPLETED"]
    any_metric = any(
        row.get("common_cohort", {}).get("rank_ic_mean") is not None
        for fold in completed for row in fold.candidate_metrics.values()
    )
    if data_quality["feature"]["research_readiness"] == "RESEARCH_BLOCKED" or data_quality["label_source"]["research_readiness"] == "RESEARCH_BLOCKED":
        status = "WF_INVALID"
    elif len(completed) < request.walk_forward_config.min_folds or not any_metric:
        status = "WF_INSUFFICIENT_EVIDENCE"
    elif exploratory:
        status = "WF_EXPLORATORY"
    else:
        status = "WF_COMPLETED"
    software = _software_versions()
    environment_hash = digest(software)
    exclusions = {
        "fold_plan": plan.exclusions,
        "incomplete_fold_count": len(plan.exclusions),
        "invalid_fold_count": sum(fold.fold_status == "FOLD_INVALID" for fold in folds),
        "insufficient_fold_count": sum(fold.fold_status == "FOLD_INSUFFICIENT_DATA" for fold in folds),
        "fold_exclusions": {
            fold.fold_id: fold.exclusions for fold in folds
            if fold.fold_status != "FOLD_COMPLETED"
        },
    }
    reproducibility = {
        "software_versions": software,
        "environment_hash": environment_hash,
        "request": request.model_dump(mode="json", exclude={"idempotency_key"}),
        "walk_forward_protocol_hash": plan.walk_forward_protocol_hash,
        "fold_plan_hash": plan.fold_plan_hash,
        "metric_implementation": "stock_quant.alpha.models.evaluation",
        "rank_ic_definition": "Spearman correlation per decision on the four-candidate common cohort",
        "bootstrap_method": "moving_block_bootstrap_v1",
        "holm_method": "holm_step_down",
        "hash_excludes": ["wall_clock", "uuid", "elapsed_seconds", "temporary_paths"],
        "resource_usage": {
            "physical_rows_examined_by_metadata": budget.physical_rows,
            "partitions_examined_by_metadata": budget.partitions,
            "lineage_nodes": len(manifests),
            "training_samples": budget.training_samples,
            "feature_rows_read": len(feature_rows),
            "label_rows_read_or_supplied": len(label_rows),
            "bootstrap_replicates_requested": request.walk_forward_config.inference.bootstrap_replicates,
            "max_seconds_effective": min(request.max_seconds, request.resource_budget.max_seconds),
        },
        "trial_ledger": trials,
    }
    payload = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "research_protocol_hash": request.research_protocol_hash,
        "walk_forward_protocol_hash": plan.walk_forward_protocol_hash,
        "feature_set_id": request.feature_set_id,
        "stock_dataset_id": request.stock_dataset_id,
        "factor_spec_hash": feature_manifest.source_version,
        "label_definition_hash": plan.label_definition_hash,
        "development_window": request.development_window.model_dump(mode="json"),
        "data_quality": data_quality,
        "fold_plan_hash": plan.fold_plan_hash,
        "folds": [fold.model_dump(mode="json") for fold in folds],
        "pooled_oof_metrics": pooled,
        "common_cohort": common_summary,
        "paired_comparisons": paired_comparisons,
        "uncertainty": uncertainty,
        "multiple_testing": multiple_testing,
        "stability": stability,
        "exclusions": exclusions,
        "trial_ledger": trials,
        "reproducibility": reproducibility,
        "research_status": status,
    }
    budget.tick(0.95)
    return WalkForwardReport.model_validate({**payload, "report_hash": digest(payload)})


def walk_forward_result_sections(report: WalkForwardReport) -> dict[str, Any]:
    """Convert a completed deterministic report into bounded Job result sections."""
    diagnostics: list[dict] = []
    for fold in report.folds:
        diagnostics.append({
            "record_type": "fold_status",
            "fold_id": fold.fold_id,
            "fold_status": fold.fold_status,
            "fold_hash": fold.fold_hash,
            "knowledge_cutoff": fold.knowledge_cutoff,
        })
        for candidate, metric_record in fold.candidate_metrics.items():
            for row in metric_record.get("common_cohort", {}).get("rank_ic_by_decision", []):
                diagnostics.append({
                    "record_type": "candidate_ic",
                    "scope": "common_cohort",
                    "fold_id": fold.fold_id,
                    "candidate": candidate,
                    **row,
                })
            for row in metric_record.get("coverage_by_decision", []):
                diagnostics.append({
                    "record_type": "coverage",
                    "fold_id": fold.fold_id,
                    "candidate": candidate,
                    **row,
                })
        for pair_key, pair_record in fold.paired_differences.items():
            left, right = pair_key.split("|", 1)
            for row in pair_record.get("daily", []):
                diagnostics.append({
                    "record_type": "paired_ic",
                    "fold_id": fold.fold_id,
                    "left_candidate": left,
                    "right_candidate": right,
                    **row,
                })
        for feature, value in fold.feature_importance.get("lightgbm_v1", {}).items():
            diagnostics.append({
                "record_type": "feature_importance",
                "fold_id": fold.fold_id,
                "candidate": "lightgbm_v1",
                "feature": feature,
                "normalized_gain": value,
            })
    summary = {
        "schema_version": report.schema_version,
        "protocol_version": report.protocol_version,
        "research_status": report.research_status,
        "walk_forward_protocol_hash": report.walk_forward_protocol_hash,
        "fold_plan_hash": report.fold_plan_hash,
        "report_hash": report.report_hash,
        "development_window": report.development_window,
        "fold_count": len(report.folds),
        "completed_fold_count": sum(fold.fold_status == "FOLD_COMPLETED" for fold in report.folds),
        "pooled_oof_metrics": report.pooled_oof_metrics,
        "common_cohort": report.common_cohort,
        "multiple_testing": report.multiple_testing,
        "stability": report.stability,
        "research_boundary": (
            "Development-period walk-forward evidence only; this is not a frozen independent Test, "
            "OOS validation, tradable Alpha or production approval."
        ),
    }
    return {
        "summary": summary,
        "walk_forward": [fold.model_dump(mode="json") for fold in report.folds],
        "diagnostics": diagnostics,
        "trials": report.trial_ledger,
        "reproducibility": report.reproducibility,
    }
