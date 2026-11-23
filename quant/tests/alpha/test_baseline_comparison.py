from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from pydantic import ValidationError
from sklearn.impute import SimpleImputer
from sklearn.preprocessing import StandardScaler
from stock_quant.alpha.contracts import StockDatasetRequest
from stock_quant.alpha.data.catalog import DatasetStore
from stock_quant.alpha.errors import AlphaError, AlphaErrorCode
from stock_quant.alpha.factors.ast import FactorNode, FactorSpec
from stock_quant.alpha.factors.compiler import compile_factor_spec
from stock_quant.alpha.factors.operators import apply_operator
from stock_quant.alpha.models.baseline_comparison import (
    BaselineComparisonConfig,
    BaselineComparisonReport,
    validate_baseline_factor_source,
)
from stock_quant.alpha.models.development_cohort import prepare_development_cohort
from stock_quant.alpha.models.labels import LabelRow
from stock_quant.alpha.models.lightgbm_trainer import (
    AlphaModelRegistry,
    ModelResourceBudget,
    TrainAlphaRequest,
    _train_alpha_outcome,
)
from stock_quant.alpha.models.temporal_split import ModelDevelopmentSplit
from stock_quant.jobs import JobStore, execute_job

START = datetime(2024, 1, 1, tzinfo=UTC)
DAYS = 90
SYMBOLS = [f"S{index:02d}" for index in range(24)]
TRAIN_END = START + timedelta(days=58)
VALIDATION_START = TRAIN_END
VALIDATION_END = START + timedelta(days=84)


def _factor_spec(*, reversal_period=5, rsi_period=5) -> FactorSpec:
    return FactorSpec(
        schema_version="1.0",
        timeframe="1d",
        nodes=[
            FactorNode(id="reversal_source", operator="pct_change", inputs=["close"], params={"period": reversal_period}),
            FactorNode(id="rsi_source", operator="rsi", inputs=["close"], params={"period": rsi_period}),
            FactorNode(id="linear_signal", operator="rolling_mean", inputs=["close"], params={"period": 5}),
            FactorNode(id="shape_signal", operator="rolling_std", inputs=["close"], params={"period": 5}),
        ],
        outputs=["reversal_source", "rsi_source", "linear_signal", "shape_signal"],
    )


def _baseline_config(*, factor_spec=None, ridge_alpha=1.0) -> BaselineComparisonConfig:
    return BaselineComparisonConfig(
        schema_version="1.0",
        factor_spec=factor_spec or _factor_spec(),
        reversal_output="reversal_source",
        rsi_output="rsi_source",
        ridge_alpha=ridge_alpha,
    )


def _fixture(
    tmp_path,
    *,
    scenario="linear",
    baseline=True,
    feature_end=VALIDATION_END,
    stock_end=VALIDATION_END,
    constant_rsi_decision: int | None = None,
    missing_reversal_decision: int | None = None,
    all_missing_train_linear: bool = False,
    quality_price_basis: str = "price_basis_unverified",
    extra_feature_rows_after_cutoff: bool = False,
    extra_stock_rows_after_cutoff: bool = False,
    factor_spec: FactorSpec | None = None,
    store_root=None,
):
    store = DatasetStore(store_root or (tmp_path / "alpha"))
    spec = factor_spec or _factor_spec()
    compiled = compile_factor_spec(spec)
    rng = np.random.default_rng(20261123)
    cutoff_days = (VALIDATION_END - START).days
    stock_days = max(cutoff_days, (stock_end - START).days)
    feature_days = (feature_end - START).days
    if extra_stock_rows_after_cutoff:
        stock_days = max(stock_days, cutoff_days + 10)
    if extra_feature_rows_after_cutoff:
        feature_days = max(feature_days, cutoff_days + 10)
    series_days = max(stock_days, feature_days, DAYS)
    bars, features, labels = [], [], []
    series_by_symbol: dict[str, dict[str, np.ndarray]] = {}
    for symbol_index, symbol in enumerate(SYMBOLS):
        base = np.array([
            50 + symbol_index * 0.2 + day * 0.03 + math.sin(day / 5 + symbol_index / 3) * 1.7
            for day in range(series_days)
        ])
        close = base + rng.normal(scale=0.05, size=series_days)
        series = {
            "reversal_source": apply_operator("pct_change", [__import__("pandas").Series(close)], {"period": 5}).to_numpy(copy=True),
            "rsi_source": apply_operator("rsi", [__import__("pandas").Series(close)], {"period": 5}).to_numpy(copy=True),
            "linear_signal": apply_operator("rolling_mean", [__import__("pandas").Series(close)], {"period": 5}).to_numpy(copy=True),
            "shape_signal": apply_operator("rolling_std", [__import__("pandas").Series(close)], {"period": 5}).to_numpy(copy=True),
        }
        target_series = {name: values.copy() for name, values in series.items()}
        if constant_rsi_decision is not None:
            series["rsi_source"][constant_rsi_decision] = 50.0
        if missing_reversal_decision is not None:
            series["reversal_source"][missing_reversal_decision] = np.nan
        if all_missing_train_linear:
            series["linear_signal"][:(TRAIN_END - START).days] = np.nan
        series_by_symbol[symbol] = series
        for day in range(series_days):
            decision = START + timedelta(days=day, hours=21, minutes=1)
            bar_end = decision - timedelta(minutes=1)
            if day >= stock_days:
                continue
            bars.append({
                "instrument_id": symbol,
                "start_at": decision - timedelta(hours=7),
                "end_at": bar_end,
                "available_at": decision,
                "open": float(close[day]), "high": float(close[day] + 1),
                "low": float(close[day] - 1), "close": float(close[day]),
                "volume": 1000 + day, "adjustment": "raw", "source": "synthetic",
                "source_version": "synthetic-v1", "currency": "USD",
            })
        for day in range(series_days):
            decision = START + timedelta(days=day, hours=21, minutes=1)
            if day < feature_days:
                for output in spec.outputs:
                    value = series[output][day]
                    features.append({
                        "decision_at": decision,
                        "instrument_id": symbol,
                        "factor_name": output,
                        "factor_value": None if not np.isfinite(value) else float(value),
                        "feature_available_at": decision,
                        "eligible": bool(np.isfinite(value)),
                        "quality_flags": [] if np.isfinite(value) else ["MISSING_VALUE"],
                        "factor_version": "synthetic-factor-v1",
                    })
            if day < feature_days and day < DAYS - 5:
                reversal = target_series["reversal_source"][day]
                linear = target_series["linear_signal"][day]
                shape = target_series["shape_signal"][day]
                noise = rng.normal(scale=0.01)
                if scenario == "linear":
                    target = -0.7 * reversal + 0.01 * linear + noise
                elif scenario == "nonlinear":
                    target = math.copysign(math.sqrt(abs(reversal)), reversal) * 1.5 + 0.02 * (linear * shape) + noise
                elif scenario == "no_signal":
                    target = noise
                else:
                    raise AssertionError(scenario)
                if np.isfinite(target):
                    labels.append(LabelRow(
                        feature_decision_at=decision,
                        entry_at=decision + timedelta(days=1),
                        exit_at=decision + timedelta(days=5),
                        label_available_at=decision + timedelta(days=5),
                        label_value=float(target),
                        label_quality=[],
                        instrument_id=symbol,
                        label_name="stock_forward_return",
                    ))
    stock = store.publish_stock(
        StockDatasetRequest(
            source="synthetic", symbols=SYMBOLS, start=START, end=stock_end,
            price_basis=quality_price_basis, calendar_version="fixture",
        ),
        bars,
        calendar_version="fixture",
        source_version="synthetic-v1",
        data_quality={},
    )
    feature_set = store.publish_feature_panel(
        features,
        parent_dataset_id=stock.dataset_id,
        factor_spec_hash=compiled.spec_hash,
        instrument_ids=SYMBOLS,
        start_at=START,
        end_at=feature_end,
        calendar_version="fixture",
        price_basis="price_basis_unverified",
    )
    split = ModelDevelopmentSplit(
        train_start=START,
        train_end=TRAIN_END,
        validation_start=VALIDATION_START,
        validation_end=VALIDATION_END,
        embargo_days=0,
    )
    request = TrainAlphaRequest(
        feature_set_id=feature_set.dataset_id,
        label_source_dataset_id=stock.dataset_id,
        label_name="stock_forward_return",
        target_type="stock_return",
        evaluation_mode="validation_only",
        research_protocol_hash="sha256:" + "4" * 64,
        split=split,
        hyperparameters={"learning_rate": 0.08, "num_leaves": 15, "min_child_samples": 5, "n_estimators": 40},
        random_seed=11,
        resource_budget=ModelResourceBudget(max_seconds=60, max_rows=50_000),
        min_rank_ic=-1,
        baseline_comparison=_baseline_config(factor_spec=spec) if baseline else None,
    )
    return {
        "store": store,
        "stock": stock,
        "feature_set": feature_set,
        "split": split,
        "request": request,
        "labels": labels,
        "spec": spec,
        "compiled": compiled,
    }


def _outcome(fixture, tmp_path):
    return _train_alpha_outcome(
        fixture["request"],
        store=fixture["store"],
        registry=AlphaModelRegistry(tmp_path / "models"),
        label_rows=fixture["labels"],
    )


@pytest.mark.parametrize("scenario", ["linear", "nonlinear", "no_signal"])
def test_comparison_runs_all_synthetic_scenarios_without_certification(tmp_path, scenario):
    fixture = _fixture(tmp_path, scenario=scenario)
    result = _outcome(fixture, tmp_path)
    report = result.baseline_comparison
    assert report is not None
    assert [candidate.model_key for candidate in report.candidates] == [
        "short_reversal_5", "rsi_5", "ridge_v1", "lightgbm_v1",
    ]
    assert report.comparison_status == "COMPARISON_EXPLORATORY"
    assert report.feature_set_id == fixture["feature_set"].dataset_id
    assert report.label_source_dataset_id == fixture["stock"].dataset_id
    assert report.data_quality["physical_time_isolation"] == "PARQUET_FOOTER_VERIFIED"
    assert result.manifest.metrics.keys() == {"train", "validation"}
    assert result.manifest.test_window is None
    common_counts = {candidate.common_cohort_sample_count for candidate in report.candidates}
    assert len(common_counts) == 1
    dumped = report.model_dump(mode="json")
    assert "ALPHA_CONFIRMED" not in str(dumped)
    assert "PRODUCTION_READY" not in str(dumped)
    assert "STATISTICALLY_SIGNIFICANT" not in str(dumped)
    assert "TRADING_VALIDATED" not in str(dumped)
    if report.comparison_status != "COMPARISON_INSUFFICIENT_DATA":
        assert all(candidate.common_cohort_metrics["sample_count"] == next(iter(common_counts)) for candidate in report.candidates)
    if scenario == "linear":
        ridge = next(candidate for candidate in report.candidates if candidate.model_key == "ridge_v1")
        assert ridge.common_cohort_metrics["rank_ic_mean"] > 0


def test_comparison_report_and_lightgbm_identity_are_deterministic(tmp_path):
    fixture = _fixture(tmp_path)
    first = _outcome(fixture, tmp_path)
    second = _outcome(fixture, tmp_path)
    assert first.manifest.model_id == second.manifest.model_id
    assert first.manifest.model_hash == second.manifest.model_hash
    assert first.baseline_comparison == second.baseline_comparison
    assert first.baseline_comparison.report_hash == second.baseline_comparison.report_hash


def test_baseline_comparison_does_not_change_lightgbm_identity(tmp_path):
    without = _fixture(tmp_path, baseline=False)
    first = _train_alpha_outcome(
        without["request"], store=without["store"],
        registry=AlphaModelRegistry(tmp_path / "models"), label_rows=without["labels"],
    )
    with_baseline = _fixture(tmp_path, baseline=True)
    second = _train_alpha_outcome(
        with_baseline["request"], store=with_baseline["store"],
        registry=AlphaModelRegistry(tmp_path / "models"), label_rows=with_baseline["labels"],
    )
    assert first.manifest.model_id == second.manifest.model_id
    assert first.manifest.model_hash == second.manifest.model_hash


def test_ridge_imputer_and_scaler_fit_only_shared_train_samples(tmp_path, monkeypatch):
    fixture = _fixture(tmp_path)
    imputer_rows, scaler_rows = [], []
    original_imputer_fit = SimpleImputer.fit
    original_scaler_fit = StandardScaler.fit
    def imputer_fit(self, X, y=None):
        imputer_rows.append(len(X))
        return original_imputer_fit(self, X, y)
    def scaler_fit(self, X, y=None, sample_weight=None):
        scaler_rows.append(len(X))
        return original_scaler_fit(self, X, y, sample_weight=sample_weight)
    monkeypatch.setattr(SimpleImputer, "fit", imputer_fit)
    monkeypatch.setattr(StandardScaler, "fit", scaler_fit)
    result = _outcome(fixture, tmp_path)
    ridge = next(candidate for candidate in result.baseline_comparison.candidates if candidate.model_key == "ridge_v1")
    assert imputer_rows == [ridge.train_sample_count]
    assert scaler_rows == [ridge.train_sample_count]
    assert ridge.train_sample_count == len(prepare_development_cohort(
        fixture["request"], store=fixture["store"], label_rows=fixture["labels"],
    ).train)


def test_factor_direction_is_fixed_before_validation_labels(tmp_path):
    fixture = _fixture(tmp_path)
    cohort = prepare_development_cohort(
        fixture["request"], store=fixture["store"], label_rows=fixture["labels"],
    )
    from stock_quant.alpha.models.baseline_comparison import _fixed_factor_scores
    first = _fixed_factor_scores(cohort, fixture["request"].baseline_comparison)
    changed_labels = [
        row.model_copy(update={"label_value": -(row.label_value or 0.0)})
        if row.feature_decision_at >= VALIDATION_START else row
        for row in fixture["labels"]
    ]
    changed_cohort = prepare_development_cohort(
        fixture["request"], store=fixture["store"], label_rows=changed_labels,
    )
    second = _fixed_factor_scores(changed_cohort, fixture["request"].baseline_comparison)
    assert np.allclose(first["short_reversal_5"], second["short_reversal_5"], equal_nan=True)
    assert np.allclose(first["rsi_5"], second["rsi_5"], equal_nan=True)


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        (_factor_spec(rsi_period=14), "rsi"),
        (_factor_spec(reversal_period=10), "pct_change"),
    ],
)
def test_nonstandard_factor_periods_are_rejected(spec, message):
    with pytest.raises(ValidationError, match=message):
        _baseline_config(factor_spec=spec)


def test_missing_configured_factor_output_is_rejected():
    spec = FactorSpec(
        schema_version="1.0", timeframe="1d",
        nodes=[FactorNode(id="reversal_source", operator="pct_change", inputs=["close"], params={"period": 5})],
        outputs=["reversal_source"],
    )
    with pytest.raises(ValidationError, match="rsi_source"):
        _baseline_config(factor_spec=spec)


def test_factor_spec_hash_mismatch_is_rejected(tmp_path):
    fixture = _fixture(tmp_path, baseline=False)
    cohort = prepare_development_cohort(
        fixture["request"].model_copy(update={
            "baseline_comparison": _baseline_config(),
        }),
        store=fixture["store"],
        label_rows=fixture["labels"],
    )
    altered = fixture["feature_set"].model_copy(update={"source_version": "sha256:" + "f" * 64})
    with pytest.raises(AlphaError, match="FactorSpec hash"):
        validate_baseline_factor_source(_baseline_config(), cohort, feature_manifest=altered)


def test_composite_or_unbound_feature_panel_is_rejected(tmp_path):
    fixture = _fixture(tmp_path)
    cohort = prepare_development_cohort(
        fixture["request"], store=fixture["store"], label_rows=fixture["labels"],
    )
    composite = fixture["feature_set"].model_copy(update={
        "source_version": "sha256:" + "e" * 64,
        "parent_dataset_ids": [fixture["stock"].dataset_id, "sha256:" + "d" * 64],
    })
    with pytest.raises(AlphaError, match="combined|composite|FactorSpec hash"):
        validate_baseline_factor_source(_baseline_config(), cohort, feature_manifest=composite)


def test_quality_scope_beyond_validation_cutoff_fails_closed(tmp_path):
    fixture = _fixture(tmp_path, feature_end=VALIDATION_END + timedelta(days=30))
    with pytest.raises(AlphaError) as info:
        _outcome(fixture, tmp_path)
    assert info.value.code == AlphaErrorCode.QUALITY_SCOPE_NOT_ISOLATED


def test_missing_factor_values_reduce_individual_and_common_coverage(tmp_path):
    fixture = _fixture(tmp_path, missing_reversal_decision=70)
    report = _outcome(fixture, tmp_path).baseline_comparison
    reversal = next(candidate for candidate in report.candidates if candidate.model_key == "short_reversal_5")
    lightgbm = next(candidate for candidate in report.candidates if candidate.model_key == "lightgbm_v1")
    assert reversal.prediction_coverage < 1.0
    assert reversal.exclusion_reasons["MISSING_FACTOR_VALUE"] == len(SYMBOLS)
    assert lightgbm.prediction_coverage == 1.0
    assert report.cohort_identity.common_cohort_sample_count < report.cohort_identity.feature_matched_validation_count


def test_constant_factor_date_is_invalid_ic_not_zero(tmp_path):
    fixture = _fixture(tmp_path, constant_rsi_decision=70)
    report = _outcome(fixture, tmp_path).baseline_comparison
    rsi = next(candidate for candidate in report.candidates if candidate.model_key == "rsi_5")
    records = rsi.validation_metrics["rank_ic_by_decision"]
    assert any(record["rank_ic"] is None for record in records)
    assert any("CONSTANT_PREDICTION" in record["exclusion_reasons"] for record in records)
    assert rsi.validation_metrics["invalid_ic_date_count"] >= 1


def test_paired_comparisons_use_intersection_of_valid_ic_dates(tmp_path):
    fixture = _fixture(tmp_path, constant_rsi_decision=70)
    report = _outcome(fixture, tmp_path).baseline_comparison
    pairs = {(pair.left_model_id, pair.right_model_id): pair for pair in report.paired_comparisons}
    lightgbm_ridge = pairs[("lightgbm_v1", "ridge_v1")]
    lightgbm_rsi = pairs[("lightgbm_v1", "rsi_5")]
    assert lightgbm_rsi.common_date_count < lightgbm_ridge.common_date_count
    for pair in report.paired_comparisons:
        assert pair.definition.startswith("paired Spearman RankIC")
        assert pair.common_sample_count == report.cohort_identity.common_cohort_sample_count
        if pair.common_date_count == len(pair.daily_differences):
            assert pair.mean_paired_rank_ic_difference == pytest.approx(sum(pair.daily_differences) / pair.common_date_count)


def test_report_hash_rejects_mutation(tmp_path):
    report = _outcome(_fixture(tmp_path), tmp_path).baseline_comparison
    payload = report.model_dump(mode="json")
    payload["comparison_status"] = "COMPARISON_COMPLETED"
    with pytest.raises(ValidationError, match="report hash"):
        BaselineComparisonReport.model_validate(payload)


def test_default_training_path_is_unchanged_without_comparison(tmp_path):
    fixture = _fixture(tmp_path, baseline=False)
    result = _train_alpha_outcome(
        fixture["request"], store=fixture["store"],
        registry=AlphaModelRegistry(tmp_path / "models"), label_rows=fixture["labels"],
    )
    assert result.baseline_comparison is None
    assert result.manifest.metrics.keys() == {"train", "validation"}
    assert not hasattr(result.manifest, "baseline_comparison")


def test_training_reads_never_extend_past_validation_cutoff(tmp_path, monkeypatch):
    fixture = _fixture(tmp_path)
    observed = []
    original = DatasetStore.read_rows
    def spy(self, dataset_id, columns=None, start_at=None, end_at=None):
        observed.append((dataset_id, start_at, end_at))
        return original(self, dataset_id, columns, start_at, end_at)
    monkeypatch.setattr(DatasetStore, "read_rows", spy)
    _outcome(fixture, tmp_path)
    assert observed
    assert all(end is not None and end <= VALIDATION_END for _, _, end in observed)


def test_all_missing_train_feature_is_a_deterministic_error(tmp_path):
    fixture = _fixture(tmp_path, all_missing_train_linear=True)
    with pytest.raises(AlphaError, match="finite Train value") as info:
        _outcome(fixture, tmp_path)
    assert info.value.code == AlphaErrorCode.DATASET_MISMATCH


def test_blocking_research_quality_marks_comparison_invalid(tmp_path):
    fixture = _fixture(tmp_path, quality_price_basis="split_adjusted")
    report = _outcome(fixture, tmp_path).baseline_comparison
    assert report.comparison_status == "COMPARISON_INVALID"
    assert report.data_quality["feature"]["research_readiness"] == "RESEARCH_BLOCKED"
    assert "PRICE_ROW_BASIS_CONFLICT" in report.data_quality["label_source"]["blocking_finding_codes"]


def test_parent_artifact_beyond_validation_cutoff_fails_quality_scope(tmp_path):
    fixture = _fixture(tmp_path, stock_end=VALIDATION_END + timedelta(days=5))
    with pytest.raises(AlphaError) as info:
        _outcome(fixture, tmp_path)
    assert info.value.code == AlphaErrorCode.QUALITY_SCOPE_NOT_ISOLATED


@pytest.mark.parametrize(
    "fixture_kwargs",
    [
        {"extra_stock_rows_after_cutoff": True},
        {"extra_feature_rows_after_cutoff": True},
    ],
)
def test_physical_rows_after_cutoff_are_rejected_before_quality_scan(tmp_path, monkeypatch, fixture_kwargs):
    fixture = _fixture(tmp_path, **fixture_kwargs)
    from stock_quant.alpha.models import baseline_comparison as module
    def forbidden(*args, **kwargs):
        raise AssertionError("research quality scan must not run before physical scope proof")
    monkeypatch.setattr(module, "assess_research_quality", forbidden)
    with pytest.raises(AlphaError) as info:
        _outcome(fixture, tmp_path)
    assert info.value.code == AlphaErrorCode.QUALITY_SCOPE_NOT_ISOLATED
    assert "Parquet" in info.value.message or "partition" in info.value.message


def test_ridge_fitted_identity_binds_training_labels(tmp_path):
    fixture = _fixture(tmp_path)
    first = _outcome(fixture, tmp_path).baseline_comparison
    changed_labels = [
        row.model_copy(update={"label_value": -(row.label_value or 0.0)})
        if row.feature_decision_at < VALIDATION_START else row
        for row in fixture["labels"]
    ]
    second = _train_alpha_outcome(
        fixture["request"], store=fixture["store"],
        registry=AlphaModelRegistry(tmp_path / "models-changed"), label_rows=changed_labels,
    ).baseline_comparison
    first_ridge = next(candidate for candidate in first.candidates if candidate.model_key == "ridge_v1")
    second_ridge = next(candidate for candidate in second.candidates if candidate.model_key == "ridge_v1")
    assert first_ridge.candidate_definition_hash == second_ridge.candidate_definition_hash
    assert first_ridge.fitted_model_hash != second_ridge.fitted_model_hash
    assert first_ridge.model_id != second_ridge.model_id
    assert first_ridge.model_hash == first_ridge.fitted_model_hash


def test_unbound_extra_feature_column_is_rejected(tmp_path):
    from dataclasses import replace
    fixture = _fixture(tmp_path)
    cohort = prepare_development_cohort(
        fixture["request"], store=fixture["store"], label_rows=fixture["labels"],
    )
    unbound = replace(cohort, feature_schema=(*cohort.feature_schema, "masquerade"))
    with pytest.raises(AlphaError, match="exactly match"):
        validate_baseline_factor_source(_baseline_config(), unbound)


def test_comparison_does_not_publish_a_dataset_or_test_artifact(tmp_path):
    fixture = _fixture(tmp_path)
    before = sorted(path.name for path in fixture["store"].datasets.iterdir())
    result = _outcome(fixture, tmp_path)
    after = sorted(path.name for path in fixture["store"].datasets.iterdir())
    assert before == after
    assert result.manifest.test_window is None
    assert "oos" not in result.manifest.metrics
    assert result.baseline_comparison is not None


def test_job_adds_report_to_training_metrics_without_new_section(tmp_path):
    fixture = _fixture(tmp_path, store_root=tmp_path / "quant.alpha")
    registry_path = tmp_path / "quant.db"
    with JobStore(registry_path) as jobs:
        submitted = jobs.submit_alpha({
            "kind": "train_alpha_model",
            "feature_set_id": fixture["feature_set"].dataset_id,
            "label_name": "stock_forward_return",
            "target_type": "stock_return",
            "stock_dataset_id": fixture["stock"].dataset_id,
            "horizon_days": 5,
            "evaluation_mode": "validation_only",
            "research_protocol_hash": "sha256:" + "4" * 64,
            "split": {
                "train_start": START.isoformat(), "train_end": TRAIN_END.isoformat(),
                "validation_start": VALIDATION_START.isoformat(), "validation_end": VALIDATION_END.isoformat(),
            },
            "hyperparameters": {"n_estimators": 20, "min_child_samples": 2},
            "random_seed": 7,
            "min_rank_ic": -1,
            "resource_budget": {"max_seconds": 60, "max_rows": 50_000},
            "baseline_comparison": _baseline_config().model_dump(mode="json"),
        })
        assert execute_job(jobs, jobs.claim()) is True
        metrics = jobs.get_result(submitted["job_id"], "training_metrics")["rows"][0]
        assert "baseline_comparison" in metrics
        report = BaselineComparisonReport.model_validate(metrics["baseline_comparison"])
        assert report.report_hash.startswith("sha256:")
        reproducibility = jobs.get_result(submitted["job_id"], "reproducibility")
        assert reproducibility["baseline_comparison"]["report_hash"] == report.report_hash
        assert reproducibility["baseline_comparison"]["train_identity_hash"] == report.cohort_identity.train_identity_hash
        assert reproducibility["baseline_comparison"]["validation_identity_hash"] == report.cohort_identity.validation_identity_hash
        with pytest.raises(ValueError, match="Invalid result section or limit"):
            jobs.get_result(submitted["job_id"], "baseline_comparison")


def test_job_default_training_metrics_have_no_baseline_field(tmp_path):
    fixture = _fixture(tmp_path, baseline=False, store_root=tmp_path / "quant.alpha")
    registry_path = tmp_path / "quant.db"
    with JobStore(registry_path) as jobs:
        submitted = jobs.submit_alpha({
            "kind": "train_alpha_model",
            "feature_set_id": fixture["feature_set"].dataset_id,
            "label_name": "stock_forward_return",
            "target_type": "stock_return",
            "stock_dataset_id": fixture["stock"].dataset_id,
            "horizon_days": 5,
            "evaluation_mode": "validation_only",
            "research_protocol_hash": "sha256:" + "4" * 64,
            "split": {
                "train_start": START.isoformat(), "train_end": TRAIN_END.isoformat(),
                "validation_start": VALIDATION_START.isoformat(), "validation_end": VALIDATION_END.isoformat(),
            },
            "hyperparameters": {"n_estimators": 20, "min_child_samples": 2},
            "random_seed": 7,
            "min_rank_ic": -1,
            "resource_budget": {"max_seconds": 60, "max_rows": 50_000},
        })
        assert execute_job(jobs, jobs.claim()) is True
        metrics = jobs.get_result(submitted["job_id"], "training_metrics")["rows"][0]
        assert set(metrics) == {"train", "validation"}
        reproducibility = jobs.get_result(submitted["job_id"], "reproducibility")
        assert "baseline_comparison" not in reproducibility
