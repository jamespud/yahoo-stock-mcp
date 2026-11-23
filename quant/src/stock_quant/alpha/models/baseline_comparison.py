"""Deterministic development-window comparison of factor, Ridge and LightGBM candidates."""
from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from importlib.metadata import version
from typing import Any, Literal

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from pydantic import Field, StrictFloat, model_validator
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from ...compiler import digest
from ...contracts import StrictModel
from ..contracts import DatasetManifest, Hash
from ..data.catalog import DatasetStore
from ..data.research_quality import (
    ResearchQualityLimits,
    ResearchQualityReport,
    assess_research_quality,
)
from ..errors import AlphaError, AlphaErrorCode
from ..factors.ast import FactorNode, FactorSpec
from ..factors.compiler import (
    CompiledFactorSpec,
    FactorCompileError,
    compile_factor_spec,
)
from ..factors.operators import operator_version
from .development_cohort import DevelopmentCohort
from .evaluation import metrics, quantile_returns, rank_ic_by_decision

REPORT_SCHEMA_VERSION = "alpha-baseline-comparison-v1"
COMPARISON_PROTOCOL_VERSION = "alpha-baseline-protocol-v1"
FACTOR_MODEL_VERSION = "factor-baseline-v1"
RIDGE_MODEL_VERSION = "ridge-cholesky-v1"
MAX_DECISION_DIAGNOSTICS = 512
CANDIDATE_KEYS = ("short_reversal_5", "rsi_5", "ridge_v1", "lightgbm_v1")
PAIR_DIRECTIONS = (
    ("lightgbm_v1", "ridge_v1"),
    ("lightgbm_v1", "short_reversal_5"),
    ("lightgbm_v1", "rsi_5"),
    ("ridge_v1", "short_reversal_5"),
)


class BaselineComparisonConfig(StrictModel):
    schema_version: Literal["1.0"]
    factor_spec: FactorSpec
    reversal_output: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    rsi_output: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    ridge_alpha: StrictFloat = Field(default=1.0, gt=0, le=1_000_000)

    @model_validator(mode="after")
    def fixed_source_definition(self):
        if self.reversal_output == self.rsi_output:
            raise ValueError("reversal_output and rsi_output must identify different nodes")
        try:
            compiled = compile_factor_spec(self.factor_spec)
        except FactorCompileError as error:
            raise ValueError(error.diagnostic["message"]) from error
        for output in (self.reversal_output, self.rsi_output):
            if output not in self.factor_spec.outputs:
                raise ValueError(f"Configured factor output {output!r} is not present in factor_spec.outputs")
        _validate_source_node(compiled.node_map[self.reversal_output], "pct_change", "reversal")
        _validate_source_node(compiled.node_map[self.rsi_output], "rsi", "rsi")
        return self


def _validate_source_node(node: FactorNode, operator: str, label: str) -> None:
    if node.operator != operator or node.inputs != ["close"] or node.params != {"period": 5}:
        raise ValueError(
            f"{label} output must be exactly {operator}(close, period=5); "
            f"got {node.operator}({', '.join(node.inputs)}, params={node.params})"
        )


class CohortIdentity(StrictModel):
    sample_identity: Literal["(decision_at, instrument_id)"]
    train_identity_hash: Hash
    validation_identity_hash: Hash
    universe_identity_hash: Hash
    decision_identity_hash: Hash
    instrument_count: int = Field(ge=1)
    train_sample_count: int = Field(ge=1)
    validation_sample_count: int = Field(ge=1)
    raw_validation_candidate_count: int = Field(ge=0)
    matured_validation_label_count: int = Field(ge=0)
    feature_matched_validation_count: int = Field(ge=0)
    common_cohort_sample_count: int = Field(ge=0)
    train_decision_count: int = Field(ge=0)
    validation_decision_count: int = Field(ge=0)


class BaselineCandidateRecord(StrictModel):
    model_key: Literal["short_reversal_5", "rsi_5", "ridge_v1", "lightgbm_v1"]
    model_id: Hash
    model_hash: Hash | None = None
    candidate_definition_hash: Hash
    fitted_model_hash: Hash | None = None
    model_version: str
    fitting_policy: str
    parameter_definition: dict[str, Any]
    feature_schema_hash: Hash
    signal_direction: Literal["HIGHER_SCORE_EXPECTED_HIGHER_RETURN_RANK"]
    factor_node: str | None = None
    factor_spec_hash: Hash | None = None
    train_sample_count: int = Field(ge=0)
    validation_sample_count: int = Field(ge=0)
    common_cohort_sample_count: int = Field(ge=0)
    train_evaluable_sample_count: int = Field(ge=0)
    validation_valid_prediction_count: int = Field(ge=0)
    prediction_coverage: float = Field(ge=0, le=1)
    train_metrics: dict[str, Any]
    validation_metrics: dict[str, Any]
    common_cohort_metrics: dict[str, Any]
    exclusion_reasons: dict[str, int]
    coverage_by_decision: list[dict[str, Any]]
    omitted_coverage_decisions: int = Field(ge=0)

    @model_validator(mode="after")
    def identity_scope(self):
        fitted_required = self.model_key in {"ridge_v1", "lightgbm_v1"}
        if fitted_required != (self.fitted_model_hash is not None):
            raise ValueError("Fitted candidates require a fitted_model_hash; factor-only candidates must not have one")
        if self.fitted_model_hash is not None and self.model_hash != self.fitted_model_hash:
            raise ValueError("model_hash must equal fitted_model_hash for fitted candidates")
        return self


class BaselinePairedComparison(StrictModel):
    model_ids: list[Literal["short_reversal_5", "rsi_5", "ridge_v1", "lightgbm_v1"]] = Field(min_length=2, max_length=2)
    left_model_id: Literal["short_reversal_5", "rsi_5", "ridge_v1", "lightgbm_v1"]
    right_model_id: Literal["short_reversal_5", "rsi_5", "ridge_v1", "lightgbm_v1"]
    common_date_count: int = Field(ge=0)
    common_sample_count: int = Field(ge=0)
    unpaired_date_count: int = Field(ge=0)
    mean_paired_rank_ic_difference: float | None
    positive_date_fraction: float | None
    daily_differences: list[float]
    paired_ic_by_date: list[dict[str, Any]]
    omitted_date_count: int = Field(ge=0)
    definition: str


class BaselineComparisonReport(StrictModel):
    schema_version: Literal["alpha-baseline-comparison-v1"]
    comparison_protocol_hash: Hash
    research_protocol_hash: str = Field(min_length=1, max_length=256)
    feature_set_id: Hash
    label_source_dataset_id: Hash
    label_definition_hash: Hash
    label_definition: dict[str, Any]
    factor_spec_hash: Hash
    development_window: dict[str, Any]
    cohort_identity: CohortIdentity
    data_quality: dict[str, Any]
    common_cohort_coverage_by_decision: list[dict[str, Any]]
    omitted_common_cohort_decisions: int = Field(ge=0)
    metric_interpretation: str = Field(min_length=1, max_length=1024)
    candidates: list[BaselineCandidateRecord] = Field(min_length=4, max_length=4)
    paired_comparisons: list[BaselinePairedComparison] = Field(min_length=4, max_length=4)
    exclusions: dict[str, int]
    reproducibility: dict[str, Any]
    comparison_status: Literal[
        "COMPARISON_COMPLETED", "COMPARISON_INSUFFICIENT_DATA", "COMPARISON_INVALID", "COMPARISON_EXPLORATORY",
    ]
    report_hash: Hash

    @model_validator(mode="after")
    def coherent(self):
        if [candidate.model_key for candidate in self.candidates] != list(CANDIDATE_KEYS):
            raise ValueError("Baseline comparison must contain the fixed four candidates in canonical order")
        payload = self.model_dump(mode="json", exclude={"report_hash"})
        if digest(payload) != self.report_hash:
            raise ValueError("baseline comparison report hash mismatch")
        return self


def _check_deadline(deadline_monotonic: float | None) -> None:
    if deadline_monotonic is not None and time.monotonic() > deadline_monotonic:
        raise AlphaError(AlphaErrorCode.RESOURCE_LIMIT_EXCEEDED, "Baseline comparison time budget exceeded")


def _lineage_manifests(store: DatasetStore, roots: list[str], *, max_nodes: int = 512) -> dict[str, DatasetManifest]:
    pending = list(roots)
    seen: set[str] = set()
    manifests: dict[str, DatasetManifest] = {}
    while pending:
        dataset_id = pending.pop(0)
        if dataset_id in seen:
            continue
        if len(seen) >= max_nodes:
            raise AlphaError(AlphaErrorCode.RESOURCE_LIMIT_EXCEEDED, "Dataset lineage exceeds comparison node budget")
        seen.add(dataset_id)
        manifest = store.load_manifest(dataset_id)
        manifests[dataset_id] = manifest
        pending.extend(parent for parent in manifest.parent_dataset_ids if parent not in seen)
    return dict(sorted(manifests.items()))


_PHYSICAL_TIME_COLUMNS = {
    "stock_bars": ("start_at", "end_at", "available_at"),
    "feature_panel": ("decision_at", "feature_available_at"),
}


def _as_utc_datetime(value) -> datetime:
    if isinstance(value, pd.Timestamp):
        value = value.to_pydatetime()
    if isinstance(value, np.datetime64):
        value = pd.Timestamp(value).to_pydatetime()
    if not isinstance(value, datetime):
        raise TypeError(f"Unsupported Parquet timestamp statistic: {type(value).__name__}")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _assert_parquet_time_isolated(
    store: DatasetStore,
    dataset_id: str,
    manifest: DatasetManifest,
    cutoff: datetime,
) -> None:
    """Prove physical row isolation from Parquet footer statistics before any scan."""
    columns = _PHYSICAL_TIME_COLUMNS.get(manifest.dataset_kind)
    if columns is None:
        raise AlphaError(
            AlphaErrorCode.QUALITY_SCOPE_NOT_ISOLATED,
            "Physical time-range proof is unavailable for this dataset kind",
            {"dataset_id": dataset_id, "dataset_kind": manifest.dataset_kind, "validation_end": cutoff.isoformat()},
        )
    root = store.datasets / dataset_id.removeprefix("sha256:")
    for partition in manifest.partition_manifest:
        if partition.row_count == 0:
            continue
        path = root / partition.logical_path
        try:
            metadata = pq.ParquetFile(path).metadata
        except Exception as error:
            raise AlphaError(
                AlphaErrorCode.QUALITY_SCOPE_NOT_ISOLATED,
                "Parquet metadata is unreadable; physical time isolation is unproven",
                {"dataset_id": dataset_id, "partition": partition.logical_path, "reason": type(error).__name__},
            ) from error
        if metadata.num_rows != partition.row_count:
            raise AlphaError(
                AlphaErrorCode.QUALITY_SCOPE_NOT_ISOLATED,
                "Parquet row count does not match the manifest; physical time isolation is unproven",
                {
                    "dataset_id": dataset_id,
                    "partition": partition.logical_path,
                    "manifest_rows": partition.row_count,
                    "parquet_rows": metadata.num_rows,
                },
            )
        names = list(metadata.schema.names)
        for column in columns:
            if column not in names:
                raise AlphaError(
                    AlphaErrorCode.QUALITY_SCOPE_NOT_ISOLATED,
                    "Required time column is absent; physical time isolation is unproven",
                    {"dataset_id": dataset_id, "partition": partition.logical_path, "column": column},
                )
            column_index = names.index(column)
            if metadata.num_row_groups == 0:
                raise AlphaError(
                    AlphaErrorCode.QUALITY_SCOPE_NOT_ISOLATED,
                    "Parquet partition contains no row-group metadata",
                    {"dataset_id": dataset_id, "partition": partition.logical_path, "column": column},
                )
            for row_group_index in range(metadata.num_row_groups):
                statistics = metadata.row_group(row_group_index).column(column_index).statistics
                if statistics is None or not statistics.has_min_max:
                    raise AlphaError(
                        AlphaErrorCode.QUALITY_SCOPE_NOT_ISOLATED,
                        "Parquet time statistics are missing; physical time isolation is unproven",
                        {
                            "dataset_id": dataset_id,
                            "partition": partition.logical_path,
                            "row_group": row_group_index,
                            "column": column,
                        },
                    )
                try:
                    observed_max = _as_utc_datetime(statistics.max)
                    observed_min = _as_utc_datetime(statistics.min)
                except (TypeError, ValueError) as error:
                    raise AlphaError(
                        AlphaErrorCode.QUALITY_SCOPE_NOT_ISOLATED,
                        "Parquet time statistics cannot be interpreted; physical time isolation is unproven",
                        {
                            "dataset_id": dataset_id,
                            "partition": partition.logical_path,
                            "row_group": row_group_index,
                            "column": column,
                        },
                    ) from error
                if observed_max > cutoff:
                    raise AlphaError(
                        AlphaErrorCode.QUALITY_SCOPE_NOT_ISOLATED,
                        "Parquet partition contains rows after the Validation cutoff",
                        {
                            "dataset_id": dataset_id,
                            "partition": partition.logical_path,
                            "row_group": row_group_index,
                            "column": column,
                            "observed_min": observed_min.isoformat(),
                            "observed_max": observed_max.isoformat(),
                            "validation_end": cutoff.isoformat(),
                        },
                    )


def _quality_reports(
    store: DatasetStore,
    cohort: DevelopmentCohort,
    *,
    cutoff,
    max_scan_rows: int,
) -> tuple[ResearchQualityReport, ResearchQualityReport]:
    forbidden = []
    manifests = _lineage_manifests(store, [cohort.feature_set_id, cohort.label_source_dataset_id])
    for dataset_id, manifest in manifests.items():
        if manifest.end_at > cutoff:
            forbidden.append({"dataset_id": dataset_id, "end_at": manifest.end_at.isoformat()})
    if forbidden:
        raise AlphaError(
            AlphaErrorCode.QUALITY_SCOPE_NOT_ISOLATED,
            "Research quality cannot scan dataset lineage beyond the Validation cutoff",
            {"validation_end": cutoff.isoformat(), "forbidden_end_times": forbidden[:32], "forbidden_count": len(forbidden)},
        )
    for dataset_id, manifest in manifests.items():
        _assert_parquet_time_isolated(store, dataset_id, manifest, cutoff)
    limits = ResearchQualityLimits(
        max_scan_rows=max_scan_rows,
        max_parent_depth=32,
        max_nodes=512,
        max_findings=128,
    )
    feature_quality = assess_research_quality(cohort.feature_set_id, store, limits=limits)
    label_quality = assess_research_quality(cohort.label_source_dataset_id, store, limits=limits)
    return feature_quality, label_quality


def _quality_summary(report: ResearchQualityReport) -> dict[str, Any]:
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


def validate_baseline_factor_source(
    config: BaselineComparisonConfig,
    cohort: DevelopmentCohort,
    *,
    feature_manifest: DatasetManifest | None = None,
) -> CompiledFactorSpec:
    manifest = feature_manifest or cohort.feature_manifest
    if len(manifest.parent_dataset_ids) != 1:
        raise AlphaError(
            AlphaErrorCode.DATASET_MISMATCH,
            "Baseline comparison rejects composite or unbound feature panels",
            {"parent_dataset_ids": manifest.parent_dataset_ids},
        )
    try:
        compiled = compile_factor_spec(config.factor_spec)
    except FactorCompileError as error:
        raise AlphaError(AlphaErrorCode.DATASET_MISMATCH, "FactorSpec compilation failed", error.diagnostic) from error
    if manifest.source_version != compiled.spec_hash:
        raise AlphaError(
            AlphaErrorCode.DATASET_MISMATCH,
            "FactorSpec hash does not match the feature panel source version",
            {"factor_spec_hash": compiled.spec_hash, "feature_source_version": manifest.source_version},
        )
    expected_schema = set(compiled.spec.outputs)
    actual_schema = set(cohort.feature_schema)
    if actual_schema != expected_schema:
        raise AlphaError(
            AlphaErrorCode.DATASET_MISMATCH,
            "Feature panel columns do not exactly match the validated FactorSpec outputs",
            {
                "missing_outputs": sorted(expected_schema - actual_schema),
                "unbound_outputs": sorted(actual_schema - expected_schema),
            },
        )
    manifest_factor_hash = manifest.universe_definition.get("factor_spec_hash")
    if manifest_factor_hash not in (None, compiled.spec_hash):
        raise AlphaError(
            AlphaErrorCode.DATASET_MISMATCH,
            "Feature panel universe metadata FactorSpec hash is inconsistent",
            {"factor_spec_hash": compiled.spec_hash, "manifest_factor_spec_hash": manifest_factor_hash},
        )
    _validate_source_node(compiled.node_map[config.reversal_output], "pct_change", "reversal")
    _validate_source_node(compiled.node_map[config.rsi_output], "rsi", "rsi")
    return compiled


def _numeric_matrix(frame: pd.DataFrame, schema: tuple[str, ...], label: str) -> np.ndarray:
    try:
        matrix = frame[list(schema)].to_numpy(dtype=float, na_value=np.nan)
    except (TypeError, ValueError) as error:
        raise AlphaError(AlphaErrorCode.DATASET_MISMATCH, f"{label} feature matrix is not numeric") from error
    if np.isinf(matrix).any():
        raise AlphaError(AlphaErrorCode.DATASET_MISMATCH, f"{label} feature matrix contains non-finite values")
    return matrix


def _fixed_factor_scores(
    cohort: DevelopmentCohort,
    config: BaselineComparisonConfig,
) -> dict[str, np.ndarray]:
    frame = cohort.validation
    reversal = frame[config.reversal_output].to_numpy(dtype=float, na_value=np.nan)
    rsi = frame[config.rsi_output].to_numpy(dtype=float, na_value=np.nan)
    if np.isinf(reversal).any() or np.isinf(rsi).any():
        raise AlphaError(AlphaErrorCode.DATASET_MISMATCH, "Baseline factor source contains non-finite values")
    return {
        "short_reversal_5": -reversal,
        "rsi_5": 50.0 - rsi,
    }


def _finite_float_list(values) -> list[float]:
    flattened = np.asarray(values, dtype=float).reshape(-1)
    if not np.isfinite(flattened).all():
        raise AlphaError(AlphaErrorCode.DATASET_MISMATCH, "Ridge fitted state contains non-finite values")
    return [float(value) for value in flattened]


def _ridge_predictions(
    cohort: DevelopmentCohort,
    alpha: float,
) -> tuple[np.ndarray, np.ndarray, str]:
    schema = cohort.feature_schema
    train_x = _numeric_matrix(cohort.train, schema, "Train")
    validation_x = _numeric_matrix(cohort.validation, schema, "Validation")
    all_missing = [name for name, values in zip(schema, train_x.T, strict=True) if not np.isfinite(values).any()]
    if all_missing:
        raise AlphaError(
            AlphaErrorCode.DATASET_MISMATCH,
            "Ridge requires every feature to have at least one finite Train value",
            {"all_missing_features": all_missing},
        )
    train_y = cohort.train["label_value"].to_numpy(dtype=float)
    pipeline = Pipeline([
        ("imputer", SimpleImputer(strategy="median")),
        ("scaler", StandardScaler()),
        ("model", Ridge(alpha=float(alpha), solver="cholesky")),
    ])
    pipeline.fit(train_x, train_y)
    train_predictions = np.asarray(pipeline.predict(train_x), dtype=float)
    predictions = np.asarray(pipeline.predict(validation_x), dtype=float)
    if not np.isfinite(train_predictions).all() or not np.isfinite(predictions).all():
        raise AlphaError(AlphaErrorCode.DATASET_MISMATCH, "Ridge produced non-finite predictions")
    imputer = pipeline.named_steps["imputer"]
    scaler = pipeline.named_steps["scaler"]
    model = pipeline.named_steps["model"]
    fitted_hash = digest({
        "alpha": float(alpha),
        "solver": "cholesky",
        "feature_schema": list(schema),
        "imputer_statistics": _finite_float_list(imputer.statistics_),
        "scaler_mean": _finite_float_list(scaler.mean_),
        "scaler_scale": _finite_float_list(scaler.scale_),
        "scaler_samples_seen": _finite_float_list(scaler.n_samples_seen_),
        "ridge_coef": _finite_float_list(model.coef_),
        "ridge_intercept": _finite_float_list(model.intercept_),
        "ridge_n_features_in": int(model.n_features_in_),
    })
    return train_predictions, predictions, fitted_hash


def _metrics(y_true: np.ndarray, y_pred: np.ndarray, decisions: list, expected: list) -> dict:
    result = metrics(y_true, y_pred, decisions, expected_decisions=expected)
    result.update(quantile_returns(y_true, y_pred, decisions))
    return result


def _compact_metrics(value: dict) -> dict:
    records = list(value.get("rank_ic_by_decision", []))
    retained = records[:MAX_DECISION_DIAGNOSTICS]
    reasons: dict[str, int] = {}
    for record in records:
        for reason in record.get("exclusion_reasons", []):
            reasons[reason] = reasons.get(reason, 0) + 1
    compact = {
        key: item for key, item in value.items()
        if key not in {"rank_ic", "rank_ic_by_decision"}
    }
    compact["rank_ic_by_decision"] = retained
    compact["omitted_decision_records"] = len(records) - len(retained)
    compact["invalid_ic_reason_counts"] = dict(sorted(reasons.items()))
    return compact


def _common_cohort_coverage(frame: pd.DataFrame, common_mask: np.ndarray) -> tuple[list[dict], int]:
    records = []
    for decision, indexes in frame.groupby("decision_at", sort=True).groups.items():
        positions = np.asarray(indexes)
        matched = len(positions)
        common_count = int(common_mask[positions].sum())
        records.append({
            "decision_at": pd.Timestamp(decision).isoformat(),
            "feature_matched_count": matched,
            "common_cohort_count": common_count,
            "common_cohort_coverage": common_count / matched if matched else 0.0,
        })
    return records[:MAX_DECISION_DIAGNOSTICS], max(0, len(records) - MAX_DECISION_DIAGNOSTICS)


def _coverage_by_decision(frame: pd.DataFrame, predictions: np.ndarray) -> tuple[list[dict], int]:
    records = []
    for decision, indexes in frame.groupby("decision_at", sort=True).groups.items():
        positions = np.asarray(indexes)
        total = len(positions)
        valid = int(np.isfinite(predictions[positions]).sum())
        records.append({
            "decision_at": pd.Timestamp(decision).isoformat(),
            "total_count": total,
            "valid_prediction_count": valid,
            "coverage": valid / total if total else 0.0,
        })
    return records[:MAX_DECISION_DIAGNOSTICS], max(0, len(records) - MAX_DECISION_DIAGNOSTICS)


def _candidate_definition_identity(
    model_key: str,
    version_value: str,
    parameters: dict,
    schema_hash: str,
) -> str:
    return digest({
        "model_key": model_key,
        "model_version": version_value,
        "parameters": parameters,
        "feature_schema_hash": schema_hash,
        "deployable": False,
    })


def _fitted_result_identity(
    cohort: DevelopmentCohort,
    candidate_definition_hash: str,
    fitted_model_hash: str | None,
) -> str:
    return digest({
        "candidate_definition_hash": candidate_definition_hash,
        "feature_set_id": cohort.feature_set_id,
        "label_source_dataset_id": cohort.label_source_dataset_id,
        "label_definition_hash": cohort.label_definition_hash,
        "train_identity_hash": cohort.train_identity_hash,
        "fitted_model_hash": fitted_model_hash,
    })


def _software_versions() -> dict[str, str]:
    return {
        "python": __import__("sys").version,
        "lightgbm": version("lightgbm"),
        "scikit_learn": version("scikit-learn"),
        "numpy": version("numpy"),
        "pandas": version("pandas"),
    }


def _paired_records(
    predictions: dict[str, np.ndarray],
    common: pd.DataFrame,
) -> list[BaselinePairedComparison]:
    expected = [] if common.empty else sorted(
        pd.Timestamp(value).isoformat() for value in common["decision_at"].unique().tolist()
    )
    decisions = [] if common.empty else [pd.Timestamp(value).isoformat() for value in common["decision_at"].tolist()]
    by_model = {
        key: {
            record["decision_at"]: record["rank_ic"]
            for record in rank_ic_by_decision(
                common["label_value"].to_numpy(dtype=float),
                prediction,
                decisions,
                expected_decisions=expected,
            )
        }
        for key, prediction in predictions.items()
    }
    result = []
    for left, right in PAIR_DIRECTIONS:
        paired = []
        for decision in expected:
            left_ic = by_model[left].get(decision)
            right_ic = by_model[right].get(decision)
            if left_ic is not None and right_ic is not None:
                paired.append({
                    "decision_at": pd.Timestamp(decision).isoformat(),
                    "left_rank_ic": float(left_ic),
                    "right_rank_ic": float(right_ic),
                    "difference": float(left_ic - right_ic),
                })
        differences = [item["difference"] for item in paired]
        retained = paired[:MAX_DECISION_DIAGNOSTICS]
        retained_differences = differences[:MAX_DECISION_DIAGNOSTICS]
        result.append(BaselinePairedComparison(
            model_ids=[left, right],
            left_model_id=left,
            right_model_id=right,
            common_date_count=len(differences),
            common_sample_count=len(common),
            unpaired_date_count=len(expected) - len(differences),
            mean_paired_rank_ic_difference=(sum(differences) / len(differences)) if differences else None,
            positive_date_fraction=(sum(value > 0 for value in differences) / len(differences)) if differences else None,
            daily_differences=retained_differences,
            paired_ic_by_date=retained,
            omitted_date_count=len(differences) - len(retained),
            definition=(
                "paired Spearman RankIC on identical common-cohort samples; "
                f"difference={left} - {right}; only dates with finite IC for both are paired"
            ),
        ))
    return result


def compare_development_models(
    cohort: DevelopmentCohort,
    lightgbm_predictions: np.ndarray,
    *,
    config: BaselineComparisonConfig,
    store: DatasetStore,
    requested_research_protocol_hash: str,
    lightgbm_model_id: str,
    lightgbm_model_hash: str,
    lightgbm_metrics: dict,
    deadline_monotonic: float | None,
    checkpoint: Callable[[float], None] | None = None,
) -> BaselineComparisonReport:
    """Compare the fixed four candidates without publishing any baseline model."""
    compiled = validate_baseline_factor_source(config, cohort)
    _check_deadline(deadline_monotonic)
    feature_quality, label_quality = _quality_reports(
        store,
        cohort,
        cutoff=cohort.split.validation_end,
        max_scan_rows=max(1, cohort.resource_budget.max_rows // 2),
    )
    _check_deadline(deadline_monotonic)
    if checkpoint:
        checkpoint(0.55)

    validation = cohort.validation.reset_index(drop=True)
    train = cohort.train.reset_index(drop=True)
    decisions = sorted(validation["decision_at"].unique().tolist())
    targets = validation["label_value"].to_numpy(dtype=float)
    factor_predictions = _fixed_factor_scores(cohort, config)
    ridge_train_predictions, ridge_predictions, ridge_fitted_hash = _ridge_predictions(cohort, float(config.ridge_alpha))
    lightgbm = np.asarray(lightgbm_predictions, dtype=float)
    if len(lightgbm) != len(validation):
        raise AlphaError(AlphaErrorCode.DATASET_MISMATCH, "LightGBM predictions do not align with the shared Validation cohort")
    predictions = {
        "short_reversal_5": factor_predictions["short_reversal_5"],
        "rsi_5": factor_predictions["rsi_5"],
        "ridge_v1": ridge_predictions,
        "lightgbm_v1": lightgbm,
    }
    common_mask = np.ones(len(validation), dtype=bool)
    for values in predictions.values():
        common_mask &= np.isfinite(values)
    common = validation.loc[common_mask].reset_index(drop=True)
    common_predictions = {key: values[common_mask] for key, values in predictions.items()}
    common_decisions = sorted(common["decision_at"].unique().tolist())
    common_targets = common["label_value"].to_numpy(dtype=float)
    schema_hash = digest(list(cohort.feature_schema))
    software = _software_versions()
    environment_hash = digest(software)
    factor_parameter = {
        "reversal": {"operator": operator_version("pct_change"), "period": 5, "score": "-pct_change(close,5)"},
        "rsi": {"operator": operator_version("rsi"), "period": 5, "score": "50-rsi(close,5)"},
    }
    ridge_parameter = {
        "alpha": float(config.ridge_alpha),
        "solver": "cholesky",
        "imputer": "median",
        "scaler": "standard",
        "fit_scope": "train_only",
    }
    lightgbm_parameter = {
        "model_version": "existing registered LightGBM outcome",
        "validation_early_stopping": "existing behavior",
    }
    split_identity = cohort.label_definition_hash
    comparison_protocol_hash = digest({
        "protocol_version": COMPARISON_PROTOCOL_VERSION,
        "candidate_keys": list(CANDIDATE_KEYS),
        "factor_spec_hash": compiled.spec_hash,
        "factor_nodes": {
            "reversal_output": config.reversal_output,
            "rsi_output": config.rsi_output,
            "reversal_operator": operator_version("pct_change"),
            "rsi_operator": operator_version("rsi"),
        },
        "fixed_directions": {
            "short_reversal_5": "-pct_change",
            "rsi_5": "50-rsi",
        },
        "ridge": ridge_parameter,
        "lightgbm": {
            "model_version": lightgbm_metrics.get("model_version", "lightgbm-cpu-deterministic-v1"),
            "parameters": lightgbm_metrics.get("parameter_definition", lightgbm_parameter),
        },
        "feature_set_id": cohort.feature_set_id,
        "feature_schema_hash": schema_hash,
        "label_source_dataset_id": cohort.label_source_dataset_id,
        "label_definition_hash": split_identity,
        "split": cohort.split.model_dump(mode="json"),
        "common_cohort_rule": "finite prediction in all four candidates on the same (decision_at,instrument_id)",
        "missing_policy": {
            "factor": "missing",
            "ridge": "train_only_median_plus_standard_scaler",
            "lightgbm": "existing model behavior",
        },
        "metric_implementation": "stock_quant.alpha.models.evaluation",
        "report_schema": REPORT_SCHEMA_VERSION,
    })

    candidate_map = {}
    factor_models = {
        "short_reversal_5": {
            "node": config.reversal_output,
            "parameters": factor_parameter["reversal"],
            "fitting_policy": "fixed_direction_no_fit",
        },
        "rsi_5": {
            "node": config.rsi_output,
            "parameters": factor_parameter["rsi"],
            "fitting_policy": "fixed_direction_no_fit",
        },
    }
    for key in ("short_reversal_5", "rsi_5"):
        candidate_definition = {
            "factor_spec_hash": compiled.spec_hash,
            "factor_node": factor_models[key]["node"],
            **factor_models[key]["parameters"],
        }
        candidate_definition_hash = _candidate_definition_identity(
            key, FACTOR_MODEL_VERSION, candidate_definition, schema_hash,
        )
        model_id = _fitted_result_identity(cohort, candidate_definition_hash, None)
        values = predictions[key]
        individual = _metrics(targets, values, validation["decision_at"].tolist(), decisions)
        common_metric = _metrics(common_targets, common_predictions[key], common["decision_at"].tolist(), common_decisions)
        coverage, omitted = _coverage_by_decision(validation, values)
        finite_count = int(np.isfinite(values).sum())
        candidate_map[key] = BaselineCandidateRecord(
            model_key=key,
            model_id=model_id,
            model_hash=None,
            candidate_definition_hash=candidate_definition_hash,
            fitted_model_hash=None,
            model_version=FACTOR_MODEL_VERSION,
            fitting_policy=factor_models[key]["fitting_policy"],
            parameter_definition=factor_models[key]["parameters"],
            feature_schema_hash=schema_hash,
            signal_direction="HIGHER_SCORE_EXPECTED_HIGHER_RETURN_RANK",
            factor_node=factor_models[key]["node"],
            factor_spec_hash=compiled.spec_hash,
            train_sample_count=len(train),
            validation_sample_count=len(validation),
            common_cohort_sample_count=len(common),
            train_evaluable_sample_count=int(np.isfinite(fixed_factor_train_values(train, config, key)).sum()),
            validation_valid_prediction_count=finite_count,
            prediction_coverage=finite_count / len(validation) if len(validation) else 0.0,
            train_metrics=_compact_metrics(
                _metrics(
                    train["label_value"].to_numpy(dtype=float),
                    fixed_factor_train_values(train, config, key),
                    train["decision_at"].tolist(),
                    sorted(train["decision_at"].unique().tolist()),
                )
            ),
            validation_metrics=_compact_metrics(individual),
            common_cohort_metrics=_compact_metrics(common_metric),
            exclusion_reasons={"MISSING_FACTOR_VALUE": len(validation) - finite_count} if finite_count < len(validation) else {},
            coverage_by_decision=coverage,
            omitted_coverage_decisions=omitted,
        )

    ridge_individual = _metrics(targets, ridge_predictions, validation["decision_at"].tolist(), decisions)
    ridge_common = _metrics(common_targets, common_predictions["ridge_v1"], common["decision_at"].tolist(), common_decisions)
    ridge_coverage, ridge_omitted = _coverage_by_decision(validation, ridge_predictions)
    ridge_definition_hash = _candidate_definition_identity(
        "ridge_v1", RIDGE_MODEL_VERSION, ridge_parameter, schema_hash,
    )
    ridge_model_id = _fitted_result_identity(cohort, ridge_definition_hash, ridge_fitted_hash)
    ridge_train = _metrics(
        train["label_value"].to_numpy(dtype=float),
        ridge_train_predictions,
        train["decision_at"].tolist(),
        sorted(train["decision_at"].unique().tolist()),
    )
    candidate_map["ridge_v1"] = BaselineCandidateRecord(
        model_key="ridge_v1",
        model_id=ridge_model_id,
        model_hash=ridge_fitted_hash,
        candidate_definition_hash=ridge_definition_hash,
        fitted_model_hash=ridge_fitted_hash,
        model_version=RIDGE_MODEL_VERSION,
        fitting_policy="train_only_pipeline_fit",
        parameter_definition=ridge_parameter,
        feature_schema_hash=schema_hash,
        signal_direction="HIGHER_SCORE_EXPECTED_HIGHER_RETURN_RANK",
        train_sample_count=len(train),
        validation_sample_count=len(validation),
        common_cohort_sample_count=len(common),
        train_evaluable_sample_count=len(train),
        validation_valid_prediction_count=int(np.isfinite(ridge_predictions).sum()),
        prediction_coverage=float(np.isfinite(ridge_predictions).mean()) if len(ridge_predictions) else 0.0,
        train_metrics=_compact_metrics(ridge_train),
        validation_metrics=_compact_metrics(ridge_individual),
        common_cohort_metrics=_compact_metrics(ridge_common),
        exclusion_reasons={"NONFINITE_MODEL_PREDICTION": int((~np.isfinite(ridge_predictions)).sum())} if not np.isfinite(ridge_predictions).all() else {},
        coverage_by_decision=ridge_coverage,
        omitted_coverage_decisions=ridge_omitted,
    )

    lightgbm_common = _metrics(common_targets, common_predictions["lightgbm_v1"], common["decision_at"].tolist(), common_decisions)
    lgb_coverage, lgb_omitted = _coverage_by_decision(validation, lightgbm)
    lightgbm_definition_hash = _candidate_definition_identity(
        "lightgbm_v1",
        lightgbm_metrics.get("model_version", "lightgbm-cpu-deterministic-v1"),
        lightgbm_metrics.get("parameter_definition", lightgbm_parameter),
        schema_hash,
    )
    candidate_map["lightgbm_v1"] = BaselineCandidateRecord(
        model_key="lightgbm_v1",
        model_id=lightgbm_model_id,
        model_hash=lightgbm_model_hash,
        candidate_definition_hash=lightgbm_definition_hash,
        fitted_model_hash=lightgbm_model_hash,
        model_version=lightgbm_metrics.get("model_version", "lightgbm-cpu-deterministic-v1"),
        fitting_policy="existing_deterministic_lightgbm_fit",
        parameter_definition=lightgbm_metrics.get("parameter_definition", lightgbm_parameter),
        feature_schema_hash=schema_hash,
        signal_direction="HIGHER_SCORE_EXPECTED_HIGHER_RETURN_RANK",
        train_sample_count=len(train),
        validation_sample_count=len(validation),
        common_cohort_sample_count=len(common),
        train_evaluable_sample_count=len(train),
        validation_valid_prediction_count=int(np.isfinite(lightgbm).sum()),
        prediction_coverage=float(np.isfinite(lightgbm).mean()) if len(lightgbm) else 0.0,
        train_metrics=_compact_metrics(lightgbm_metrics["train"]),
        validation_metrics=_compact_metrics(lightgbm_metrics["validation"]),
        common_cohort_metrics=_compact_metrics(lightgbm_common),
        exclusion_reasons={"NONFINITE_MODEL_PREDICTION": int((~np.isfinite(lightgbm)).sum())} if not np.isfinite(lightgbm).all() else {},
        coverage_by_decision=lgb_coverage,
        omitted_coverage_decisions=lgb_omitted,
    )

    if checkpoint:
        checkpoint(0.85)
    paired = _paired_records(common_predictions, common)
    common_coverage, omitted_common_decisions = _common_cohort_coverage(validation, common_mask)
    feature_summary = _quality_summary(feature_quality)
    label_summary = _quality_summary(label_quality)
    quality_reports = (feature_quality, label_quality)
    if any(report.research_readiness == "RESEARCH_BLOCKED" for report in quality_reports):
        comparison_status = "COMPARISON_INVALID"
    elif len(common) == 0 or not any(
        candidate.common_cohort_metrics.get("valid_ic_date_count", 0) > 0
        for candidate in candidate_map.values()
    ):
        comparison_status = "COMPARISON_INSUFFICIENT_DATA"
    elif any(report.research_readiness != "RESEARCH_DATA_VALIDATED" for report in quality_reports):
        comparison_status = "COMPARISON_EXPLORATORY"
    else:
        comparison_status = "COMPARISON_COMPLETED"

    cohort_identity = CohortIdentity(
        sample_identity="(decision_at, instrument_id)",
        train_identity_hash=cohort.train_identity_hash,
        validation_identity_hash=cohort.validation_identity_hash,
        universe_identity_hash=cohort.universe_identity_hash,
        decision_identity_hash=cohort.decision_identity_hash,
        instrument_count=len(set(cohort.feature_manifest.instrument_ids) | set(cohort.label_manifest.instrument_ids)),
        train_sample_count=cohort.counts["train_sample_count"],
        validation_sample_count=cohort.counts["validation_sample_count"],
        raw_validation_candidate_count=cohort.counts["raw_validation_candidate_count"],
        matured_validation_label_count=cohort.counts["matured_validation_label_count"],
        feature_matched_validation_count=cohort.counts["feature_matched_validation_count"],
        common_cohort_sample_count=len(common),
        train_decision_count=cohort.counts["train_decision_count"],
        validation_decision_count=cohort.counts["validation_decision_count"],
    )
    exclusions = {
        **cohort.counts,
        "common_cohort_excluded_count": len(validation) - len(common),
    }
    payload = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "comparison_protocol_hash": comparison_protocol_hash,
        "research_protocol_hash": requested_research_protocol_hash,
        "feature_set_id": cohort.feature_set_id,
        "label_source_dataset_id": cohort.label_source_dataset_id,
        "label_definition_hash": cohort.label_definition_hash,
        "label_definition": {
            **cohort.label_spec,
            "maturity_rule": "split_matured_development",
            "label_available_at_requirement": "interval_end_minus_embargo_or_earlier",
        },
        "factor_spec_hash": compiled.spec_hash,
        "development_window": cohort.split.model_dump(mode="json"),
        "cohort_identity": cohort_identity.model_dump(mode="json"),
        "data_quality": {
            "scope": "development_only",
            "physical_time_isolation": "PARQUET_FOOTER_VERIFIED",
            "quality_scan_row_limit_per_root": max(1, cohort.resource_budget.max_rows // 2),
            "feature": feature_summary,
            "label_source": label_summary,
        },
        "common_cohort_coverage_by_decision": common_coverage,
        "omitted_common_cohort_decisions": omitted_common_decisions,
        "metric_interpretation": (
            "RankIC is comparable on the common cohort. MAE/RMSE for uncalibrated factor scores are "
            "auxiliary diagnostics only. Quantile returns describe rank buckets, not tradable "
            "after-cost strategy returns."
        ),
        "candidates": [candidate_map[key].model_dump(mode="json") for key in CANDIDATE_KEYS],
        "paired_comparisons": [pair.model_dump(mode="json") for pair in paired],
        "exclusions": exclusions,
        "reproducibility": {
            "software_versions": software,
            "environment_hash": environment_hash,
            "random_seed": lightgbm_metrics.get("random_seed"),
            "computation_sample_count": len(train) + len(validation),
            "report_identity_excludes_elapsed_time": True,
        },
        "comparison_status": comparison_status,
    }
    return BaselineComparisonReport.model_validate({**payload, "report_hash": digest(payload)})


def fixed_factor_train_values(train: pd.DataFrame, config: BaselineComparisonConfig, key: str) -> np.ndarray:
    source = train[config.reversal_output if key == "short_reversal_5" else config.rsi_output].to_numpy(
        dtype=float, na_value=np.nan,
    )
    if np.isinf(source).any():
        raise AlphaError(AlphaErrorCode.DATASET_MISMATCH, "Baseline factor source contains non-finite Train values")
    return -source if key == "short_reversal_5" else 50.0 - source
