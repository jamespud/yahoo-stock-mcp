"""Deterministic LightGBM training with train-only fitting and frozen test evaluation."""
from __future__ import annotations

import math
import time
from datetime import date
from importlib.metadata import version
from typing import Callable, Literal

import lightgbm as lgb
import numpy as np
import pandas as pd
from pydantic import Field, StrictFloat, StrictInt, field_validator, model_validator

from ...compiler import digest
from ...contracts import StrictModel
from ..contracts import Hash, ModelResourceBudget
from ..data.catalog import DatasetStore
from ..data.session_coverage import mapped_stock_calendar
from ..errors import AlphaError, AlphaErrorCode
from .evaluation import metrics, quantile_returns
from .diagnostic_protocol import (
    ProtocolMode,
    RESERVED_TEST_END,
    RESERVED_TEST_START,
    evaluate_frozen_validation_gate,
    protocol_claim,
    validate_frozen_label_contract,
    validate_frozen_membership,
    validate_frozen_training_binding,
)
from .hyperparameters import INT32_MAX, validate_hyperparameters
from .labels import LabelRow
from .registry import AlphaModelRegistry
from .signal_diagnostics import (
    DirectSignalSpec,
    evaluate_direct_signal,
    evaluate_weekly_signal,
    expected_session_dates,
    signal_spec_hash,
    weekly_last_session_dates,
)
from .temporal_split import ModelDevelopmentSplit, TemporalSplit, split_matured_development, split_matured_labels

MODEL_TYPE = "lightgbm_regressor"
MODEL_VERSION = "lightgbm-cpu-deterministic-v1"
SELECTION_DIAGNOSTICS_VERSION = "validation-selection-v1"
BASELINE_COMPARISON_VERSION = "validation-baseline-comparison-v1"


class TrainAlphaRequest(StrictModel):
    feature_set_id: Hash
    label_source_dataset_id: Hash | None = None
    label_name: str = Field(min_length=1, max_length=128)
    horizon_days: int = Field(default=5, ge=1, le=252, strict=True)
    target_type: str = Field(pattern=r"^(stock_return|option_return|option_iv_change|stock_return_with_option_features)$")
    evaluation_mode: str = Field(default="legacy_inline_test", pattern=r"^(validation_only|legacy_inline_test)$")
    research_protocol_hash: str | None = None
    protocol_mode: ProtocolMode = "synthetic_contract"
    candidate_id: str | None = Field(default=None, pattern=r"^H0[1-6]$")
    split: TemporalSplit | ModelDevelopmentSplit
    hyperparameters: dict[str, StrictInt | StrictFloat] = Field(default_factory=dict)
    random_seed: int = Field(default=0, strict=True, ge=-INT32_MAX - 1, le=INT32_MAX)
    resource_budget: ModelResourceBudget = Field(default_factory=ModelResourceBudget)
    min_rank_ic: float = Field(default=0.03, ge=-1, le=1, strict=True)
    selection_metric: Literal["l2", "daily_rank_ic", "weekly_rank_ic"] = "l2"
    baseline_signal: DirectSignalSpec | None = None

    _validate_hyperparameters = field_validator("hyperparameters")(validate_hyperparameters)

    @model_validator(mode="after")
    def split_mode(self):
        if self.evaluation_mode == "validation_only" and not isinstance(self.split, ModelDevelopmentSplit):
            raise ValueError("validation_only training requires ModelDevelopmentSplit")
        if self.evaluation_mode == "validation_only" and not self.research_protocol_hash:
            raise ValueError("validation_only training requires research_protocol_hash")
        if self.evaluation_mode == "validation_only" and not self.label_source_dataset_id:
            raise ValueError("validation_only training requires label_source_dataset_id")
        if (
            self.protocol_mode == "frozen_v1_3"
            and
            self.evaluation_mode == "validation_only"
            and self.split.train_start < RESERVED_TEST_END
            and self.split.validation_end > RESERVED_TEST_START
        ):
            raise ValueError("validation_only split must not enter reserved Test interval at or after 2012-01-01T00:00:00Z")
        if self.evaluation_mode == "legacy_inline_test" and not isinstance(self.split, TemporalSplit):
            raise ValueError("legacy_inline_test training requires TemporalSplit")
        if self.evaluation_mode == "legacy_inline_test" and self.selection_metric != "l2":
            raise ValueError("selection metrics are validation-only")
        if self.evaluation_mode == "legacy_inline_test" and self.baseline_signal is not None:
            raise ValueError("baseline_signal is validation-only")
        if self.protocol_mode == "frozen_v1_3":
            if self.evaluation_mode != "validation_only":
                raise ValueError("frozen_v1_3 requires validation_only evaluation")
            validate_frozen_training_binding(
                protocol_hash_value=self.research_protocol_hash or "",
                split=self.split,
                candidate_id=self.candidate_id,
                hyperparameters=self.hyperparameters,
                random_seed=self.random_seed,
                resource_budget=self.resource_budget,
                baseline_signal=self.baseline_signal,
                min_rank_ic=self.min_rank_ic,
            )
        return self


class ModelSetManifest(StrictModel):
    schema_version: str = "1.0"
    model_id: Hash
    model_set_id: Hash
    model_type: str
    model_version: str
    feature_set_id: Hash
    label_definition_hash: Hash
    label_version: str
    feature_schema: list[str]
    selected_iteration: int
    model_hash: Hash
    environment_hash: Hash
    training_window: dict
    validation_window: dict
    test_window: dict | None = None
    metrics: dict
    validation_gate_passed: bool
    evaluation_mode: str = "legacy_inline_test"
    research_protocol_hash: str | None = None
    protocol_mode: ProtocolMode = "synthetic_contract"
    protocol_claim: dict | None = None
    frozen_validation_gate: dict | None = None
    is_alpha_certified: bool = False
    selection_diagnostics: dict | None = None
    baseline_comparison: dict | None = None


def _environment_hash() -> str:
    return digest({
        "python": __import__("sys").version,
        "lightgbm": version("lightgbm"),
        "sklearn": version("scikit-learn"),
        "numpy": version("numpy"),
        "pandas": version("pandas"),
    })


def _feature_frame(rows: list[dict]) -> tuple[pd.DataFrame, list[str]]:
    frame = pd.DataFrame(rows)
    if frame.empty:
        raise AlphaError(AlphaErrorCode.DATA_UNAVAILABLE, "Feature set has no rows")
    decisions = pd.to_datetime(frame["decision_at"], utc=True)
    available = pd.to_datetime(frame["feature_available_at"], utc=True)
    if (available > decisions).any():
        raise AlphaError(AlphaErrorCode.DATASET_MISMATCH, "Feature contains values available after its decision time")
    pivot = frame.pivot_table(index=["decision_at", "instrument_id"], columns="factor_name", values="factor_value", aggfunc="first")
    schema = sorted(pivot.columns)
    pivot = pivot[schema]
    pivot.index = pivot.index.set_levels(pd.to_datetime(pivot.index.levels[0], utc=True), level=0)
    return pivot, list(schema)


def _join_labels(features: pd.DataFrame, schema: list[str], labels: list[LabelRow]) -> pd.DataFrame:
    label_frame = pd.DataFrame([row.model_dump(mode="python") for row in labels])
    label_frame["decision_at"] = pd.to_datetime(label_frame["feature_decision_at"], utc=True)
    label_frame["label_available_at"] = pd.to_datetime(label_frame["label_available_at"], utc=True)
    joined = features.reset_index().merge(
        label_frame[["decision_at", "instrument_id", "label_value", "label_available_at"]],
        on=["decision_at", "instrument_id"], how="inner", validate="one_to_one",
    )
    joined = joined[joined["label_value"].notna()].copy()
    return joined


def _subset(joined: pd.DataFrame, start, end) -> pd.DataFrame:
    return joined[(joined["decision_at"] >= pd.Timestamp(start)) & (joined["decision_at"] < pd.Timestamp(end)) & (joined["label_available_at"] <= pd.Timestamp(end))].copy()


def _params(request: TrainAlphaRequest):
    return {
        "objective": "regression",
        "metric": "l2",
        "verbosity": -1,
        "seed": request.random_seed,
        "deterministic": True,
        "force_col_wise": True,
        "num_threads": 1,
        "feature_fraction": 1.0,
        "bagging_fraction": 1.0,
        "bagging_freq": 0,
        "learning_rate": float(request.hyperparameters.get("learning_rate", 0.05)),
        "num_leaves": int(request.hyperparameters.get("num_leaves", 31)),
        "max_depth": int(request.hyperparameters.get("max_depth", -1)),
        "min_child_samples": int(request.hyperparameters.get("min_child_samples", 20)),
    }, int(request.hyperparameters.get("n_estimators", 100))


def _deadline_callback(deadline: float):
    def enforce(_env):
        if time.monotonic() > deadline:
            raise AlphaError(AlphaErrorCode.RESOURCE_LIMIT_EXCEEDED, "Training time budget exceeded")
    return enforce


def _validation_only(request: TrainAlphaRequest) -> bool:
    return request.evaluation_mode == "validation_only"


def _candidate_metric_key(selection_metric: str) -> str:
    if selection_metric == "l2":
        return "l2_rmse"
    if selection_metric in {"daily_rank_ic", "weekly_rank_ic"}:
        return selection_metric
    raise ValueError(f"Unsupported validation selection metric: {selection_metric}")


def select_candidate_iteration(candidates: list[dict], selection_metric: str) -> int:
    """Select one finite candidate, preserving the first exact tie."""
    metric_key = _candidate_metric_key(selection_metric)
    best_iteration = None
    best_value = None
    for candidate in candidates:
        value = candidate.get(metric_key)
        if value is None or not math.isfinite(float(value)):
            continue
        numeric = float(value)
        if best_value is None or (numeric < best_value if selection_metric == "l2" else numeric > best_value):
            best_iteration = int(candidate["iteration"])
            best_value = numeric
    if best_iteration is None:
        raise ValueError(f"no finite {selection_metric} candidate iteration")
    return best_iteration


def _decision_dates(frame: pd.DataFrame) -> list[date]:
    return [pd.Timestamp(value).date() for value in frame["decision_at"]]


def _rank_ic_on_dates(
    frame: pd.DataFrame,
    predictions: np.ndarray,
    expected_sessions: list[date],
) -> dict:
    expected = sorted({pd.Timestamp(value).date() for value in expected_sessions})
    if not expected:
        return metrics(
            frame["label_value"].to_numpy(dtype=float),
            predictions,
            [],
            expected_decisions=[],
        )
    session_values = np.asarray(_decision_dates(frame), dtype=object)
    expected_set = set(expected)
    mask = np.fromiter((value in expected_set for value in session_values), dtype=bool, count=len(session_values))
    if not mask.any():
        return metrics(
            np.asarray([], dtype=float),
            np.asarray([], dtype=float),
            [],
            expected_decisions=expected,
        )
    return metrics(
        frame.loc[mask, "label_value"].to_numpy(dtype=float),
        predictions[mask],
        [value for value, keep in zip(session_values, mask, strict=True) if keep],
        expected_decisions=expected,
    )


def _ic_reasons(summary: dict, frequency: str) -> list[str]:
    if summary.get("rank_ic_mean") is not None:
        return []
    reasons = {
        reason
        for record in summary.get("rank_ic_by_decision", [])
        for reason in record.get("exclusion_reasons", [])
        if reason
    }
    return [f"NO_FINITE_{frequency.upper()}_RANK_IC", *sorted(reasons)]


def _candidate_record(
    iteration: int,
    validation: pd.DataFrame,
    predictions: np.ndarray,
    daily_sessions: list[date],
    weekly_sessions: list[date],
) -> dict:
    targets = validation["label_value"].to_numpy(dtype=float)
    matched = np.isfinite(targets) & np.isfinite(predictions)
    rmse = (
        float(np.sqrt(np.mean((targets[matched] - predictions[matched]) ** 2)))
        if matched.any()
        else None
    )
    daily = _rank_ic_on_dates(validation, predictions, daily_sessions)
    weekly = _rank_ic_on_dates(validation, predictions, weekly_sessions)
    return {
        "iteration": iteration,
        "l2": rmse,
        "l2_rmse": rmse,
        "daily_rank_ic": daily["rank_ic_mean"],
        "weekly_rank_ic": weekly["rank_ic_mean"],
        "daily_sample_count": daily["sample_count"],
        "weekly_sample_count": weekly["sample_count"],
        "daily_decision_count": daily["expected_ic_date_count"],
        "weekly_decision_count": weekly["expected_ic_date_count"],
        "daily_valid_decision_count": daily["valid_ic_date_count"],
        "weekly_valid_decision_count": weekly["valid_ic_date_count"],
        "defined": {
            "l2": rmse is not None,
            "l2_rmse": rmse is not None,
            "daily_rank_ic": daily["rank_ic_mean"] is not None,
            "weekly_rank_ic": weekly["rank_ic_mean"] is not None,
        },
        "undefined_reasons": {
            "l2": [] if rmse is not None else ["NO_FINITE_VALIDATION_SAMPLES"],
            "l2_rmse": [] if rmse is not None else ["NO_FINITE_VALIDATION_SAMPLES"],
            "daily_rank_ic": _ic_reasons(daily, "daily"),
            "weekly_rank_ic": _ic_reasons(weekly, "weekly"),
        },
    }


def _selected_prediction_hash(frame: pd.DataFrame, predictions: np.ndarray) -> str:
    rows = sorted(
        (
            {
                "decision_at": pd.Timestamp(decision).isoformat(),
                "instrument_id": str(instrument_id).upper(),
                "prediction": float(prediction),
            }
            for decision, instrument_id, prediction in zip(
                frame["decision_at"], frame["instrument_id"], predictions, strict=True,
            )
        ),
        key=lambda row: (row["decision_at"], row["instrument_id"]),
    )
    return digest(rows)


def _direction(delta: float | None) -> str:
    if delta is None:
        return "undefined"
    if delta > 0:
        return "positive"
    if delta < 0:
        return "negative"
    return "zero"


def _baseline_values(
    feature_rows: list[dict],
    spec: DirectSignalSpec,
    instruments: set[str],
    sessions: set[date],
) -> dict[tuple[date, str], float]:
    values: dict[tuple[date, str], float] = {}
    for row in feature_rows:
        if row.get("factor_name") != spec.factor_name or not row.get("eligible", True):
            continue
        instrument = str(row["instrument_id"]).upper()
        decision_at = pd.Timestamp(row["decision_at"]).to_pydatetime()
        session = decision_at.date()
        if instrument not in instruments or session not in sessions:
            continue
        if pd.Timestamp(row["feature_available_at"]).to_pydatetime() > decision_at:
            raise AlphaError(AlphaErrorCode.DATASET_MISMATCH, "Baseline feature is available after its decision time")
        value = row.get("factor_value")
        if value is None or not np.isfinite(float(value)):
            continue
        values[(session, instrument)] = float(value) * spec.score_multiplier
    return values


def _common_rank_ic(
    validation: pd.DataFrame,
    predictions: np.ndarray,
    baseline_values: dict[tuple[date, str], float],
    expected_sessions: list[date],
) -> dict:
    targets, model_values, baseline = [], [], []
    decisions = []
    expected = set(expected_sessions)
    for index, (_, row) in enumerate(validation.iterrows()):
        session = pd.Timestamp(row["decision_at"]).date()
        instrument = str(row["instrument_id"]).upper()
        model_value = float(predictions[index])
        target = float(row["label_value"])
        baseline_value = baseline_values.get((session, instrument))
        if (
            session not in expected
            or baseline_value is None
            or not all(np.isfinite(value) for value in (model_value, target, baseline_value))
        ):
            continue
        decisions.append(session)
        targets.append(target)
        model_values.append(model_value)
        baseline.append(float(baseline_value))
    model_metrics = metrics(
        np.asarray(targets, dtype=float),
        np.asarray(model_values, dtype=float),
        decisions,
        expected_decisions=expected_sessions,
    )
    baseline_metrics = metrics(
        np.asarray(targets, dtype=float),
        np.asarray(baseline, dtype=float),
        decisions,
        expected_decisions=expected_sessions,
    )
    return {"model": model_metrics, "baseline": baseline_metrics}


def _comparison_bucket(
    *,
    model_summary: dict,
    baseline_rank_ic_mean: float | None,
    baseline_sample_count: int,
    common: dict,
    expected_session_count: int,
    expected_sample_count: int,
) -> dict:
    model_mean = common["model"]["rank_ic_mean"]
    baseline_mean = common["baseline"]["rank_ic_mean"]
    delta = model_mean - baseline_mean if model_mean is not None and baseline_mean is not None else None
    return {
        "model_rank_ic_mean": model_summary.get("rank_ic_mean"),
        "baseline_rank_ic_mean": baseline_rank_ic_mean,
        "model_rank_ic_mean_on_common": model_mean,
        "baseline_rank_ic_mean_on_common": baseline_mean,
        "delta": delta,
        "direction": _direction(delta),
        "common_sample_count": common["model"]["sample_count"],
        "common_decision_count": common["model"]["valid_ic_date_count"],
        "model_sample_count": model_summary.get("sample_count"),
        "baseline_sample_count": baseline_sample_count,
        "expected_sample_count": expected_sample_count,
        "expected_decision_count": expected_session_count,
        "model_coverage": (
            model_summary.get("sample_count", 0) / expected_sample_count
            if expected_sample_count else 0.0
        ),
        "baseline_coverage": (
            baseline_sample_count / expected_sample_count
            if expected_sample_count else 0.0
        ),
    }


def _baseline_comparison(
    *,
    spec: DirectSignalSpec,
    feature_rows: list[dict],
    label_rows: list[LabelRow],
    validation: pd.DataFrame,
    selected_predictions: np.ndarray,
    selected_daily: dict,
    selected_weekly: dict,
    instruments: list[str],
    daily_sessions: list[date],
    weekly_sessions: list[date],
    validation_grid_source: str,
) -> dict:
    baseline_daily = evaluate_direct_signal(
        feature_rows,
        label_rows,
        expected_instruments=instruments,
        expected_sessions=daily_sessions,
        spec=spec,
    )
    baseline_weekly = evaluate_weekly_signal(
        feature_rows,
        label_rows,
        expected_instruments=instruments,
        expected_sessions=daily_sessions,
        spec=spec,
    )
    instrument_set = {str(value).upper() for value in instruments}
    daily_values = _baseline_values(feature_rows, spec, instrument_set, set(daily_sessions))
    weekly_values = _baseline_values(feature_rows, spec, instrument_set, set(weekly_sessions))
    daily_common = _common_rank_ic(validation, selected_predictions, daily_values, daily_sessions)
    weekly_common = _common_rank_ic(validation, selected_predictions, weekly_values, weekly_sessions)
    expected = len(instrument_set)
    coverage_definition = (
        "Expected instruments x mapped validation sessions; common samples are an intersection "
        "and do not replace either signal's fixed coverage denominator."
        if validation_grid_source == "mapped_validation_sessions"
        else "Expected instruments x validation_rows_fallback sessions; common samples are an intersection "
        "and do not replace either signal's fixed coverage denominator."
    )
    return {
        "schema_version": BASELINE_COMPARISON_VERSION,
        "comparison": "model_minus_baseline_on_common_samples",
        "baseline_signal": spec.model_dump(mode="json"),
        "baseline_signal_hash": signal_spec_hash(spec),
        "same_validation_opportunity_grid": True,
        "validation_grid_source": validation_grid_source,
        "coverage_definition": coverage_definition,
        "daily": _comparison_bucket(
            model_summary=selected_daily,
            baseline_rank_ic_mean=baseline_daily["summary"]["rank_ic_mean"],
            baseline_sample_count=baseline_daily["coverage"]["valid_matched_opportunities"],
            common=daily_common,
            expected_session_count=len(daily_sessions),
            expected_sample_count=expected * len(daily_sessions),
        ),
        "weekly": _comparison_bucket(
            model_summary=selected_weekly,
            baseline_rank_ic_mean=baseline_weekly["summary"]["weekly_rank_ic_mean"],
            baseline_sample_count=baseline_weekly["summary"]["weekly_coverage"]["valid_matched_opportunities"],
            common=weekly_common,
            expected_session_count=len(weekly_sessions),
            expected_sample_count=expected * len(weekly_sessions),
        ),
    }


def train_validation_only(
    request: TrainAlphaRequest,
    *,
    store: DatasetStore,
    registry: AlphaModelRegistry,
    label_rows: list[LabelRow],
    checkpoint: Callable[[float], None] | None = None,
    validation_sessions: list[date] | None = None,
) -> ModelSetManifest:
    """Strict Validation-only training path; no Test feature, label, prediction, or metric is created."""
    if not isinstance(request.split, ModelDevelopmentSplit):
        raise AlphaError(AlphaErrorCode.DATASET_MISMATCH, "Validation-only training requires ModelDevelopmentSplit")
    feature_manifest = store.load_manifest(request.feature_set_id)
    stock_manifest = store.load_manifest(request.label_source_dataset_id)
    if request.protocol_mode == "frozen_v1_3" and ((
        feature_manifest.start_at < RESERVED_TEST_END and feature_manifest.end_at > RESERVED_TEST_START
    ) or (
        stock_manifest.start_at < RESERVED_TEST_END and stock_manifest.end_at > RESERVED_TEST_START
    )):
        raise AlphaError(
            AlphaErrorCode.DATASET_MISMATCH,
            "Validation-only dataset physical coverage enters reserved Test",
            {
                "feature_end": feature_manifest.end_at.isoformat(),
                "stock_end": stock_manifest.end_at.isoformat(),
            },
        )
    calendar = None
    if request.protocol_mode == "frozen_v1_3":
        try:
            validate_frozen_membership(feature_manifest.instrument_ids)
            validate_frozen_membership(stock_manifest.instrument_ids)
        except ValueError as error:
            raise AlphaError(AlphaErrorCode.DATASET_MISMATCH, str(error)) from error
        if stock_manifest.dataset_id not in feature_manifest.parent_dataset_ids:
            raise AlphaError(AlphaErrorCode.DATASET_MISMATCH, "Feature set is not derived from the frozen stock dataset")
        calendar = mapped_stock_calendar(stock_manifest)
        if calendar is None:
            raise AlphaError(AlphaErrorCode.DATA_UNAVAILABLE, "frozen_v1_3 requires a mapped calendar")
        validate_frozen_label_contract(label_rows)
        mapped_validation_sessions = expected_session_dates(
            calendar,
            request.split.validation_start.date(),
            request.split.validation_end.date(),
        )
        if validation_sessions is None or set(validation_sessions) != set(mapped_validation_sessions):
            raise AlphaError(
                AlphaErrorCode.DATASET_MISMATCH,
                "frozen_v1_3 requires the exact mapped Validation session grid",
            )
        validation_sessions = mapped_validation_sessions
    label_rows = [row for row in label_rows if row.label_name == request.label_name]
    if not label_rows:
        raise AlphaError(AlphaErrorCode.DATA_UNAVAILABLE, "No labels match the requested label_name")
    began = time.monotonic()
    cutoff = request.split.validation_end
    feature_rows = store.read_rows(request.feature_set_id, start_at=request.split.train_start, end_at=cutoff)
    features, schema = _feature_frame(feature_rows)
    if request.protocol_mode == "frozen_v1_3":
        from .diagnostic_protocol import candidate_definition
        expected_schema = sorted(candidate_definition(request.candidate_id)["factor_outputs"])
        if schema != expected_schema:
            raise AlphaError(
                AlphaErrorCode.DATASET_MISMATCH,
                "frozen_v1_3 feature schema does not match the approved candidate",
                {"expected": expected_schema, "actual": schema},
            )
    joined = _join_labels(features, schema, label_rows)
    if len(joined) > request.resource_budget.max_rows:
        raise AlphaError(AlphaErrorCode.RESOURCE_LIMIT_EXCEEDED, "Training rows exceed budget")
    matured = split_matured_development(label_rows, request.split)
    def allowed(frame, rows):
        keys = {(row.feature_decision_at, row.instrument_id) for row in rows}
        return frame[frame.apply(lambda item: (item["decision_at"].to_pydatetime(), item["instrument_id"]) in keys, axis=1)]
    train = allowed(_subset(joined, request.split.train_start, request.split.train_end), matured["train"])
    validation = allowed(_subset(joined, request.split.validation_start, request.split.validation_end), matured["validation"])
    if train.empty or validation.empty:
        raise AlphaError(AlphaErrorCode.INSUFFICIENT_HISTORY, "Train and validation require matured labels")
    if time.monotonic() - began > request.resource_budget.max_seconds:
        raise AlphaError(AlphaErrorCode.RESOURCE_LIMIT_EXCEEDED, "Training time budget exceeded")
    if validation_sessions is None:
        daily_sessions = sorted(set(_decision_dates(validation)))
        validation_grid_source = "validation_rows_fallback"
    else:
        daily_sessions = sorted({pd.Timestamp(value).date() for value in validation_sessions})
        validation_grid_source = "mapped_validation_sessions"
    if not daily_sessions:
        raise AlphaError(AlphaErrorCode.INSUFFICIENT_HISTORY, "Validation grid has no scheduled sessions")
    weekly_sessions = weekly_last_session_dates(daily_sessions)
    params, num_boost_round = _params(request)
    train_data = lgb.Dataset(train[schema], label=train["label_value"], feature_name=schema, free_raw_data=False)
    booster = lgb.train(
        params,
        train_data,
        num_boost_round=num_boost_round,
        callbacks=[lgb.log_evaluation(0), _deadline_callback(began + request.resource_budget.max_seconds)],
    )
    candidates = []
    for iteration in range(1, num_boost_round + 1):
        iteration_predictions = booster.predict(validation[schema], num_iteration=iteration)
        candidates.append(_candidate_record(
            iteration, validation, iteration_predictions, daily_sessions, weekly_sessions,
        ))
        if time.monotonic() - began > request.resource_budget.max_seconds:
            raise AlphaError(AlphaErrorCode.RESOURCE_LIMIT_EXCEEDED, "Training time budget exceeded")
    expected_iterations = list(range(1, num_boost_round + 1))
    if [record["iteration"] for record in candidates] != expected_iterations:
        raise AlphaError(AlphaErrorCode.DATASET_MISMATCH, "Candidate iterations are not contiguous on one tree path")
    try:
        selected_iteration = select_candidate_iteration(candidates, request.selection_metric)
    except ValueError as error:
        raise AlphaError(AlphaErrorCode.INSUFFICIENT_HISTORY, str(error)) from error
    if time.monotonic() - began > request.resource_budget.max_seconds:
        raise AlphaError(AlphaErrorCode.RESOURCE_LIMIT_EXCEEDED, "Training time budget exceeded")
    predictions = booster.predict(validation[schema], num_iteration=selected_iteration)
    train_predictions = booster.predict(train[schema], num_iteration=selected_iteration)
    train_metrics = metrics(train["label_value"].to_numpy(), train_predictions, train["decision_at"].tolist())
    validation_metrics = metrics(validation["label_value"].to_numpy(), predictions, validation["decision_at"].tolist())
    selected_record = candidates[selected_iteration - 1]
    selected_daily = _rank_ic_on_dates(validation, predictions, daily_sessions)
    selected_weekly = _rank_ic_on_dates(validation, predictions, weekly_sessions)
    metric_key = _candidate_metric_key(request.selection_metric)
    l2_evaluated = sum(record["defined"]["l2_rmse"] for record in candidates)
    daily_evaluated = sum(record["defined"]["daily_rank_ic"] for record in candidates)
    weekly_evaluated = sum(record["defined"]["weekly_rank_ic"] for record in candidates)
    evaluated_counts = {
        "l2": l2_evaluated,
        "l2_rmse": l2_evaluated,
        "daily_rank_ic": daily_evaluated,
        "weekly_rank_ic": weekly_evaluated,
    }
    undefined_counts = {
        "l2": len(candidates) - l2_evaluated,
        "l2_rmse": len(candidates) - l2_evaluated,
        "daily_rank_ic": len(candidates) - daily_evaluated,
        "weekly_rank_ic": len(candidates) - weekly_evaluated,
    }
    selection_diagnostics = {
        "schema_version": SELECTION_DIAGNOSTICS_VERSION,
        "selection_metric": request.selection_metric,
        "selection_rule": (
            "minimum_validation_l2_rmse"
            if request.selection_metric == "l2"
            else f"maximum_validation_{request.selection_metric}"
        ),
        "tie_rule": "first_iteration_on_exact_tie",
        "tree_path": "single_booster_all_candidate_iterations",
        "candidate_iteration_start": 1,
        "candidate_iteration_end": num_boost_round,
        "candidate_iteration_count": len(candidates),
        "candidate_iterations_contiguous": True,
        "selected_iteration": selected_iteration,
        "selected_value": selected_record[metric_key],
        "evaluated_counts": evaluated_counts,
        "undefined_counts": undefined_counts,
        "selected_validation_prediction_hash": _selected_prediction_hash(validation, predictions),
        "validation_grid": {
            "source": validation_grid_source,
            "daily_decision_dates": [session.isoformat() for session in daily_sessions],
            "weekly_decision_dates": [session.isoformat() for session in weekly_sessions],
        },
        "candidates": candidates,
    }
    feature_manifest = store.load_manifest(request.feature_set_id)
    expected_instruments = feature_manifest.instrument_ids or sorted(validation["instrument_id"].unique())
    baseline_comparison = None
    if request.baseline_signal is not None:
        baseline_comparison = _baseline_comparison(
            spec=request.baseline_signal,
            feature_rows=feature_rows,
            label_rows=matured["validation"],
            validation=validation,
            selected_predictions=predictions,
            selected_daily=selected_daily,
            selected_weekly=selected_weekly,
            instruments=expected_instruments,
            daily_sessions=daily_sessions,
            weekly_sessions=weekly_sessions,
            validation_grid_source=validation_grid_source,
        )
    importance = booster.feature_importance(importance_type="gain", iteration=selected_iteration)
    environment_hash = _environment_hash()
    model_text = booster.model_to_string(num_iteration=selected_iteration)
    if request.protocol_mode == "frozen_v1_3":
        frozen_gate = evaluate_frozen_validation_gate(
            [
                {
                    "decision_at": pd.Timestamp(row["decision_at"]).to_pydatetime(),
                    "instrument_id": row["instrument_id"],
                    "label_value": float(row["label_value"]),
                    "prediction": float(prediction),
                }
                for (_, row), prediction in zip(validation.iterrows(), predictions, strict=True)
            ],
            expected_instruments=stock_manifest.instrument_ids,
            expected_sessions=daily_sessions,
        )
        if not frozen_gate["passed"]:
            raise AlphaError(
                AlphaErrorCode.INSUFFICIENT_HISTORY,
                "Frozen v1.3 Validation gate failed",
                frozen_gate,
            )
        validation_gate = True
    else:
        frozen_gate = None
        validation_gate = (
            validation_metrics.get("rank_ic_mean") is not None
            and validation_metrics["rank_ic_mean"] >= request.min_rank_ic
            and validation_metrics["prediction_coverage"] >= 0.8
        )
    label_versions = sorted({row.label_version for row in label_rows})
    label_spec = {
        "label_name": request.label_name,
        "target_type": request.target_type,
        "horizon_days": request.horizon_days,
        "label_source_dataset_id": request.label_source_dataset_id,
        "label_version": label_versions[0] if len(label_versions) == 1 else "mixed",
    }
    label_hash = digest({
        "label_spec": label_spec,
        "split": request.split.model_dump(mode="json"),
        "research_protocol_hash": request.research_protocol_hash,
    })
    trained = registry.save(model_text=model_text, metadata={
        "model_type": MODEL_TYPE, "model_version": MODEL_VERSION, "feature_set_id": request.feature_set_id,
        "label_definition_hash": label_hash,
        "label_spec": label_spec,
        "label_source_dataset_id": request.label_source_dataset_id,
        "factor_spec_hash": feature_manifest.source_version,
        "calendar_version": feature_manifest.calendar_version,
        "training_window": {"start": request.split.train_start.isoformat(), "end": request.split.train_end.isoformat()},
        "validation_window": {"validation_start": request.split.validation_start.isoformat(), "validation_end": request.split.validation_end.isoformat()},
        "evaluation_mode": request.evaluation_mode,
        "research_protocol_hash": request.research_protocol_hash,
        "protocol_mode": request.protocol_mode,
        "candidate_id": request.candidate_id,
        "protocol_claim": protocol_claim(request.protocol_mode),
        "frozen_validation_gate": frozen_gate,
        "selected_iteration": int(selected_iteration), "hyperparameters": params, "feature_schema": schema,
        "feature_schema_hash": digest(schema), "environment_hash": environment_hash,
        "random_seed": request.random_seed, "feature_importance": dict(zip(schema, map(float, importance), strict=True)),
        "train_metrics": train_metrics, "validation_metrics": validation_metrics,
        "validation_gate_passed": validation_gate,
        "selection_metric": request.selection_metric,
        "selection_diagnostics": selection_diagnostics,
        "baseline_comparison": baseline_comparison,
    })
    if checkpoint:
        checkpoint(1.0)
    return ModelSetManifest(
        model_id=trained["model_id"], model_set_id=trained["model_set_id"], model_type=MODEL_TYPE,
        model_version=MODEL_VERSION, feature_set_id=request.feature_set_id, label_definition_hash=label_hash,
        label_version=label_spec["label_version"],
        feature_schema=schema, selected_iteration=int(selected_iteration), model_hash=trained["model_hash"],
        environment_hash=environment_hash,
        training_window={"start": request.split.train_start.isoformat(), "end": request.split.train_end.isoformat()},
        validation_window={"start": request.split.validation_start.isoformat(), "end": request.split.validation_end.isoformat()},
        metrics={"train": train_metrics, "validation": validation_metrics},
        validation_gate_passed=validation_gate, evaluation_mode=request.evaluation_mode,
        research_protocol_hash=request.research_protocol_hash, is_alpha_certified=False,
        protocol_mode=request.protocol_mode, protocol_claim=protocol_claim(request.protocol_mode),
        frozen_validation_gate=frozen_gate,
        selection_diagnostics=selection_diagnostics,
        baseline_comparison=baseline_comparison,
    )


def train_alpha(
    request: TrainAlphaRequest,
    *,
    store: DatasetStore,
    registry: AlphaModelRegistry,
    label_rows: list[LabelRow] | None = None,
    checkpoint: Callable[[float], None] | None = None,
    validation_sessions: list[date] | None = None,
) -> ModelSetManifest:
    if _validation_only(request):
        if label_rows is None:
            raise AlphaError(AlphaErrorCode.DATA_UNAVAILABLE, "Label rows must be supplied or generated by the label job")
        return train_validation_only(
            request,
            store=store,
            registry=registry,
            label_rows=label_rows,
            checkpoint=checkpoint,
            validation_sessions=validation_sessions,
        )
    if label_rows is None:
        raise AlphaError(AlphaErrorCode.DATA_UNAVAILABLE, "Label rows must be supplied or generated by the label job")
    label_rows = [row for row in label_rows if row.label_name == request.label_name]
    if not label_rows:
        raise AlphaError(AlphaErrorCode.DATA_UNAVAILABLE, "No labels match the requested label_name")
    began = time.monotonic()
    features, schema = _feature_frame(store.read_rows(request.feature_set_id))
    joined = _join_labels(features, schema, label_rows)
    if len(joined) > request.resource_budget.max_rows:
        raise AlphaError(AlphaErrorCode.RESOURCE_LIMIT_EXCEEDED, "Training rows exceed budget")
    matured = split_matured_labels(label_rows, request.split)
    def allowed(frame, rows):
        keys = {(row.feature_decision_at, row.instrument_id) for row in rows}
        return frame[frame.apply(lambda item: (item["decision_at"].to_pydatetime(), item["instrument_id"]) in keys, axis=1)]
    train = allowed(_subset(joined, request.split.train_start, request.split.train_end), matured["train"])
    validation = allowed(_subset(joined, request.split.validation_start, request.split.validation_end), matured["validation"])
    test = allowed(_subset(joined, request.split.test_start, request.split.test_end), matured["test"])
    if train.empty or validation.empty or test.empty:
        raise AlphaError(AlphaErrorCode.INSUFFICIENT_HISTORY, "Train, validation and test require matured labels")
    if time.monotonic() - began > request.resource_budget.max_seconds:
        raise AlphaError(AlphaErrorCode.RESOURCE_LIMIT_EXCEEDED, "Training time budget exceeded")
    params, num_boost_round = _params(request)
    train_data = lgb.Dataset(train[schema], label=train["label_value"], feature_name=schema, free_raw_data=False)
    valid_data = lgb.Dataset(validation[schema], label=validation["label_value"], feature_name=schema, reference=train_data, free_raw_data=False)
    booster = lgb.train(params, train_data, num_boost_round=num_boost_round, valid_sets=[valid_data], callbacks=[lgb.log_evaluation(0), lgb.early_stopping(max(1, min(20, num_boost_round // 5)), verbose=False)])
    if time.monotonic() - began > request.resource_budget.max_seconds:
        raise AlphaError(AlphaErrorCode.RESOURCE_LIMIT_EXCEEDED, "Training time budget exceeded")
    selected_iteration = booster.best_iteration or num_boost_round
    train_predictions = booster.predict(train[schema], num_iteration=selected_iteration)
    predictions = booster.predict(validation[schema], num_iteration=selected_iteration)
    test_predictions = booster.predict(test[schema], num_iteration=selected_iteration)
    train_metrics = metrics(train["label_value"].to_numpy(), train_predictions, train["decision_at"].tolist())
    validation_metrics = metrics(validation["label_value"].to_numpy(), predictions, validation["decision_at"].tolist())
    oos_metrics = metrics(test["label_value"].to_numpy(), test_predictions, test["decision_at"].tolist())
    oos_metrics.update(quantile_returns(test["label_value"].to_numpy(), test_predictions, test["decision_at"].tolist()))
    importance = booster.feature_importance(importance_type="gain", iteration=selected_iteration)
    environment_hash = _environment_hash()
    model_text = booster.model_to_string(num_iteration=selected_iteration)
    validation_gate = (
        validation_metrics.get("rank_ic_mean") is not None
        and validation_metrics["rank_ic_mean"] >= request.min_rank_ic
        and validation_metrics["prediction_coverage"] >= 0.8
    )
    label_versions = sorted({row.label_version for row in label_rows})
    label_spec = {
        "label_name": request.label_name,
        "target_type": request.target_type,
        "horizon_days": request.horizon_days,
        "label_source_dataset_id": request.label_source_dataset_id,
        "label_version": label_versions[0] if len(label_versions) == 1 else "mixed",
    }
    label_hash = digest({
        "label_spec": label_spec,
        "split": request.split.model_dump(mode="json"),
        "research_protocol_hash": request.research_protocol_hash,
    })
    feature_manifest = store.load_manifest(request.feature_set_id)
    trained = registry.save(model_text=model_text, metadata={
        "model_type": MODEL_TYPE, "model_version": MODEL_VERSION, "feature_set_id": request.feature_set_id,
        "label_definition_hash": label_hash, "label_spec": label_spec,
        "label_source_dataset_id": request.label_source_dataset_id,
        "factor_spec_hash": feature_manifest.source_version,
        "calendar_version": feature_manifest.calendar_version,
        "training_window": request.split.model_dump(mode="json"),
        "validation_window": request.split.model_dump(mode="json"), "test_window": request.split.model_dump(mode="json"),
        "selected_iteration": int(selected_iteration), "hyperparameters": params, "feature_schema": schema,
        "feature_schema_hash": digest(schema), "environment_hash": environment_hash,
        "random_seed": request.random_seed, "feature_importance": dict(zip(schema, map(float, importance), strict=True)),
        "train_metrics": train_metrics, "validation_metrics": validation_metrics, "oos_metrics": oos_metrics,
        "validation_gate_passed": validation_gate, "evaluation_mode": request.evaluation_mode,
        "research_protocol_hash": request.research_protocol_hash,
    })
    if checkpoint:
        checkpoint(1.0)
    return ModelSetManifest(
        model_id=trained["model_id"], model_set_id=trained["model_set_id"], model_type=MODEL_TYPE,
        model_version=MODEL_VERSION, feature_set_id=request.feature_set_id, label_definition_hash=label_hash,
        label_version=label_spec["label_version"],
        feature_schema=schema, selected_iteration=int(selected_iteration), model_hash=trained["model_hash"],
        environment_hash=environment_hash,
        training_window={"start": request.split.train_start.isoformat(), "end": request.split.train_end.isoformat()},
        validation_window={"start": request.split.validation_start.isoformat(), "end": request.split.validation_end.isoformat()},
        test_window={"start": request.split.test_start.isoformat(), "end": request.split.test_end.isoformat()},
        metrics={"train": train_metrics, "validation": validation_metrics, "oos": oos_metrics},
        validation_gate_passed=validation_gate, evaluation_mode=request.evaluation_mode,
        research_protocol_hash=request.research_protocol_hash, is_alpha_certified=False,
    )
