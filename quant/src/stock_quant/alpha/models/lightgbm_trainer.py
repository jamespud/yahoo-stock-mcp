"""Deterministic LightGBM training with train-only fitting and optional baseline comparison."""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import version
from typing import Literal

import lightgbm as lgb
from pydantic import Field, StrictFloat, StrictInt, field_validator, model_validator

from ...compiler import digest
from ...contracts import StrictModel
from ..contracts import Hash, ModelResourceBudget
from ..data.catalog import DatasetStore
from ..errors import AlphaError, AlphaErrorCode
from ..research_admission import require_training_execution
from .baseline_comparison import (
    BaselineComparisonConfig,
    BaselineComparisonReport,
    compare_development_models,
    preflight_baseline_comparison,
)
from .development_cohort import (
    DevelopmentCohort,
    _feature_frame,
    _join_labels,
    _subset,
    prepare_development_cohort,
)
from .evaluation import metrics, quantile_returns
from .hyperparameters import INT32_MAX, validate_hyperparameters
from .labels import LabelRow
from .registry import AlphaModelRegistry
from .temporal_split import ModelDevelopmentSplit, TemporalSplit, split_matured_labels

MODEL_TYPE = "lightgbm_regressor"
MODEL_VERSION = "lightgbm-cpu-deterministic-v1"


class TrainAlphaRequest(StrictModel):
    feature_set_id: Hash
    label_source_dataset_id: Hash | None = None
    label_name: str = Field(min_length=1, max_length=128)
    horizon_days: int = Field(default=5, ge=1, le=252, strict=True)
    target_type: str = Field(pattern=r"^(stock_return|option_return|option_iv_change|stock_return_with_option_features)$")
    evaluation_mode: Literal["validation_only", "legacy_inline_test"]
    research_protocol_hash: str | None = None
    split: TemporalSplit | ModelDevelopmentSplit
    hyperparameters: dict[str, StrictInt | StrictFloat] = Field(default_factory=dict)
    random_seed: int = Field(default=0, strict=True, ge=-INT32_MAX - 1, le=INT32_MAX)
    resource_budget: ModelResourceBudget = Field(default_factory=ModelResourceBudget)
    min_rank_ic: float = Field(default=0.03, ge=-1, le=1, strict=True)
    baseline_comparison: BaselineComparisonConfig | None = None

    _validate_hyperparameters = field_validator("hyperparameters")(validate_hyperparameters)

    @model_validator(mode="after")
    def split_mode(self):
        if self.evaluation_mode == "validation_only" and not isinstance(self.split, ModelDevelopmentSplit):
            raise ValueError("validation_only training requires ModelDevelopmentSplit")
        if self.evaluation_mode == "validation_only" and not self.research_protocol_hash:
            raise ValueError("validation_only training requires research_protocol_hash")
        if self.evaluation_mode == "validation_only" and not self.label_source_dataset_id:
            raise ValueError("validation_only training requires label_source_dataset_id")
        if self.evaluation_mode == "legacy_inline_test" and not isinstance(self.split, TemporalSplit):
            raise ValueError("legacy_inline_test training requires TemporalSplit")
        if self.baseline_comparison is not None and self.evaluation_mode != "validation_only":
            raise ValueError("baseline_comparison is only available for validation_only training")
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
    is_alpha_certified: bool = False


@dataclass(frozen=True)
class _TrainingOutcome:
    """Internal training result; external callers continue to receive ModelSetManifest."""

    manifest: ModelSetManifest
    baseline_comparison: BaselineComparisonReport | None = None
    baseline_comparison_elapsed_seconds: float | None = None


def _environment_hash() -> str:
    return digest({
        "python": __import__("sys").version,
        "lightgbm": version("lightgbm"),
        "sklearn": version("scikit-learn"),
        "numpy": version("numpy"),
        "pandas": version("pandas"),
    })


def lightgbm_parameters(hyperparameters: dict, random_seed: int):
    return {
        "objective": "regression",
        "metric": "l2",
        "verbosity": -1,
        "seed": random_seed,
        "deterministic": True,
        "force_col_wise": True,
        "num_threads": 1,
        "feature_fraction": 1.0,
        "bagging_fraction": 1.0,
        "bagging_freq": 0,
        "learning_rate": float(hyperparameters.get("learning_rate", 0.05)),
        "num_leaves": int(hyperparameters.get("num_leaves", 31)),
        "max_depth": int(hyperparameters.get("max_depth", -1)),
        "min_child_samples": int(hyperparameters.get("min_child_samples", 20)),
    }, int(hyperparameters.get("n_estimators", 100))


def _params(request: TrainAlphaRequest):
    return lightgbm_parameters(request.hyperparameters, request.random_seed)


def _model_manifest(
    request: TrainAlphaRequest,
    cohort: DevelopmentCohort,
    *,
    trained: dict,
    selected_iteration: int,
    feature_schema: list[str],
    environment_hash: str,
    train_metrics: dict,
    validation_metrics: dict,
    validation_gate: bool,
) -> ModelSetManifest:
    return ModelSetManifest(
        model_id=trained["model_id"], model_set_id=trained["model_set_id"], model_type=MODEL_TYPE,
        model_version=MODEL_VERSION, feature_set_id=request.feature_set_id,
        label_definition_hash=cohort.label_definition_hash,
        label_version=cohort.label_spec["label_version"],
        feature_schema=feature_schema, selected_iteration=int(selected_iteration), model_hash=trained["model_hash"],
        environment_hash=environment_hash,
        training_window={"start": request.split.train_start.isoformat(), "end": request.split.train_end.isoformat()},
        validation_window={"start": request.split.validation_start.isoformat(), "end": request.split.validation_end.isoformat()},
        metrics={"train": train_metrics, "validation": validation_metrics},
        validation_gate_passed=validation_gate, evaluation_mode=request.evaluation_mode,
        research_protocol_hash=request.research_protocol_hash, is_alpha_certified=False,
    )


def _train_validation_outcome(
    request: TrainAlphaRequest,
    *,
    store: DatasetStore,
    registry: AlphaModelRegistry,
    label_rows: list[LabelRow],
    checkpoint: Callable[[float], None] | None = None,
) -> _TrainingOutcome:
    """Strict Validation-only training path; no Test feature, label, prediction, or metric is created."""
    require_training_execution(request)
    if not isinstance(request.split, ModelDevelopmentSplit):
        raise AlphaError(AlphaErrorCode.DATASET_MISMATCH, "Validation-only training requires ModelDevelopmentSplit")
    preflight_baseline_comparison(
        feature_set_id=request.feature_set_id,
        label_source_dataset_id=request.label_source_dataset_id,
        split=request.split,
        config=request.baseline_comparison,
        store=store,
    )
    began = time.monotonic()
    deadline = began + request.resource_budget.max_seconds
    cohort = prepare_development_cohort(
        request, store=store, label_rows=label_rows,
        deadline_monotonic=deadline, checkpoint=checkpoint,
    )
    train = cohort.train
    validation = cohort.validation
    schema = list(cohort.feature_schema)
    params, num_boost_round = _params(request)
    train_data = lgb.Dataset(train[schema], label=train["label_value"], feature_name=schema, free_raw_data=False)
    valid_data = lgb.Dataset(validation[schema], label=validation["label_value"], feature_name=schema, reference=train_data, free_raw_data=False)
    booster = lgb.train(params, train_data, num_boost_round=num_boost_round, valid_sets=[valid_data], callbacks=[lgb.log_evaluation(0), lgb.early_stopping(max(1, min(20, num_boost_round // 5)), verbose=False)])
    if time.monotonic() > deadline:
        raise AlphaError(AlphaErrorCode.RESOURCE_LIMIT_EXCEEDED, "Training time budget exceeded")
    selected_iteration = booster.best_iteration or num_boost_round
    train_predictions = booster.predict(train[schema], num_iteration=selected_iteration)
    predictions = booster.predict(validation[schema], num_iteration=selected_iteration)
    train_metrics = metrics(train["label_value"].to_numpy(), train_predictions, train["decision_at"].tolist())
    validation_metrics = metrics(validation["label_value"].to_numpy(), predictions, validation["decision_at"].tolist())
    importance = booster.feature_importance(importance_type="gain", iteration=selected_iteration)
    environment_hash = _environment_hash()
    model_text = booster.model_to_string(num_iteration=selected_iteration)
    validation_gate = (
        validation_metrics.get("rank_ic_mean") is not None
        and validation_metrics["rank_ic_mean"] >= request.min_rank_ic
        and validation_metrics["prediction_coverage"] >= 0.8
    )
    feature_manifest = cohort.feature_manifest
    trained = registry.save(model_text=model_text, metadata={
        "model_type": MODEL_TYPE, "model_version": MODEL_VERSION, "feature_set_id": request.feature_set_id,
        "label_definition_hash": cohort.label_definition_hash,
        "label_spec": cohort.label_spec,
        "label_source_dataset_id": request.label_source_dataset_id,
        "factor_spec_hash": feature_manifest.source_version,
        "calendar_version": feature_manifest.calendar_version,
        "training_window": {"start": request.split.train_start.isoformat(), "end": request.split.train_end.isoformat()},
        "validation_window": {"validation_start": request.split.validation_start.isoformat(), "validation_end": request.split.validation_end.isoformat()},
        "evaluation_mode": request.evaluation_mode,
        "research_protocol_hash": request.research_protocol_hash,
        "selected_iteration": int(selected_iteration), "hyperparameters": params, "feature_schema": schema,
        "feature_schema_hash": digest(schema), "environment_hash": environment_hash,
        "random_seed": request.random_seed, "feature_importance": dict(zip(schema, map(float, importance), strict=True)),
        "train_metrics": train_metrics, "validation_metrics": validation_metrics,
        "validation_gate_passed": validation_gate,
    })
    manifest = _model_manifest(
        request, cohort, trained=trained, selected_iteration=selected_iteration,
        feature_schema=schema, environment_hash=environment_hash,
        train_metrics=train_metrics, validation_metrics=validation_metrics,
        validation_gate=validation_gate,
    )
    if request.baseline_comparison is None:
        if checkpoint:
            checkpoint(1.0)
        return _TrainingOutcome(manifest=manifest)
    comparison_began = time.monotonic()
    report = compare_development_models(
        cohort,
        predictions,
        config=request.baseline_comparison,
        store=store,
        requested_research_protocol_hash=request.research_protocol_hash,
        lightgbm_model_id=manifest.model_id,
        lightgbm_model_hash=manifest.model_hash,
        lightgbm_metrics={
            "train": train_metrics,
            "validation": validation_metrics,
            "model_version": MODEL_VERSION,
            "parameter_definition": {
                "hyperparameters": params,
                "num_boost_round": num_boost_round,
                "selected_iteration": int(selected_iteration),
                "random_seed": request.random_seed,
            },
            "random_seed": request.random_seed,
        },
        deadline_monotonic=deadline,
        checkpoint=checkpoint,
    )
    if checkpoint:
        checkpoint(1.0)
    return _TrainingOutcome(
        manifest=manifest,
        baseline_comparison=report,
        baseline_comparison_elapsed_seconds=round(time.monotonic() - comparison_began, 6),
    )


def train_validation_only(
    request: TrainAlphaRequest,
    *,
    store: DatasetStore,
    registry: AlphaModelRegistry,
    label_rows: list[LabelRow],
    checkpoint: Callable[[float], None] | None = None,
) -> ModelSetManifest:
    """Compatibility wrapper returning the existing ModelSetManifest."""
    return _train_validation_outcome(
        request, store=store, registry=registry, label_rows=label_rows, checkpoint=checkpoint,
    ).manifest


def _train_alpha_outcome(
    request: TrainAlphaRequest,
    *,
    store: DatasetStore,
    registry: AlphaModelRegistry,
    label_rows: list[LabelRow] | None = None,
    checkpoint: Callable[[float], None] | None = None,
) -> _TrainingOutcome:
    """Private Job entry point that may carry the optional comparison report."""
    require_training_execution(request)
    if label_rows is None:
        raise AlphaError(AlphaErrorCode.DATA_UNAVAILABLE, "Label rows must be supplied or generated by the label job")
    return _train_validation_outcome(
        request, store=store, registry=registry, label_rows=label_rows, checkpoint=checkpoint,
    )


def train_alpha(
    request: TrainAlphaRequest,
    *,
    store: DatasetStore,
    registry: AlphaModelRegistry,
    label_rows: list[LabelRow] | None = None,
    checkpoint: Callable[[float], None] | None = None,
) -> ModelSetManifest:
    """Authorized compatibility entry point returning ModelSetManifest."""
    return _train_alpha_outcome(
        request, store=store, registry=registry, label_rows=label_rows, checkpoint=checkpoint,
    ).manifest


def _train_legacy_inline_test(
    request: TrainAlphaRequest,
    *,
    store: DatasetStore,
    registry: AlphaModelRegistry,
    label_rows: list[LabelRow] | None = None,
    checkpoint: Callable[[float], None] | None = None,
) -> ModelSetManifest:
    """Private compatibility test path for already isolated historical fixtures only."""
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
