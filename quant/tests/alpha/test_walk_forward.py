from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd
import pytest
from stock_quant.alpha.contracts import StockDatasetRequest
from stock_quant.alpha.data.catalog import DatasetStore
from stock_quant.alpha.errors import AlphaError, AlphaErrorCode
from stock_quant.alpha.factors.ast import FactorNode, FactorSpec
from stock_quant.alpha.factors.compiler import compile_factor_spec
from stock_quant.alpha.models.baseline_comparison import BaselineComparisonConfig
from stock_quant.alpha.models.development_cohort import _feature_frame
from stock_quant.alpha.models.labels import stock_forward_labels
from stock_quant.alpha.models.walk_forward import (
    BootstrapInferenceConfig,
    WalkForwardAlphaRequest,
    WalkForwardConfig,
    WalkForwardDevelopmentWindow,
    WalkForwardLabelSpec,
    WalkForwardReport,
    WalkForwardResourceBudget,
    _bootstrap_series,
    _concat_frames,
    _decision_sessions,
    _feature_rows,
    _fit_nested_lightgbm,
    _join_window,
    _label_frame,
    _moving_block_draws,
    _pooled_metrics,
    _preflight,
    _subset,
    holm_adjust,
    plan_development_walk_forward,
    run_development_walk_forward,
)
from stock_quant.alpha.research_admission import (
    ResearchAdmissionError,
    require_walk_forward_submission_raw,
)
from stock_quant.jobs import JobStore, execute_job

START = datetime(2024, 1, 1, tzinfo=UTC)
DEV_END = START + timedelta(days=105)
SYMBOLS = [f"S{index:02d}" for index in range(20)]


def _factor_spec() -> FactorSpec:
    return FactorSpec(
        schema_version="1.0",
        timeframe="1d",
        nodes=[
            FactorNode(id="reversal_source", operator="pct_change", inputs=["close"], params={"period": 5}),
            FactorNode(id="rsi_source", operator="rsi", inputs=["close"], params={"period": 5}),
            FactorNode(id="linear_signal", operator="rolling_mean", inputs=["close"], params={"period": 5}),
            FactorNode(id="shape_signal", operator="rolling_std", inputs=["close"], params={"period": 5}),
        ],
        outputs=["reversal_source", "rsi_source", "linear_signal", "shape_signal"],
    )


def _request(
    *,
    store: DatasetStore,
    feature_set_id: str,
    stock_dataset_id: str,
    mode: str = "expanding",
    train_sessions: int = 30,
    validation_sessions: int = 10,
    inner_validation_sessions: int = 5,
    min_folds: int = 3,
    replicates: int = 25,
) -> WalkForwardAlphaRequest:
    spec = _factor_spec()
    return WalkForwardAlphaRequest(
        evaluation_mode="validation_only",
        research_protocol_hash="sha256:" + "1" * 64,
        feature_set_id=feature_set_id,
        stock_dataset_id=stock_dataset_id,
        label_spec=WalkForwardLabelSpec(
            label_name="stock_forward_return",
            target_type="stock_return",
            stock_dataset_id=stock_dataset_id,
            horizon_days=5,
        ),
        development_window=WalkForwardDevelopmentWindow(
            development_start=START,
            development_end=DEV_END,
        ),
        baseline_comparison=BaselineComparisonConfig(
            schema_version="1.0",
            factor_spec=spec,
            reversal_output="reversal_source",
            rsi_output="rsi_source",
            ridge_alpha=1.0,
        ),
        lightgbm_hyperparameters={"n_estimators": 20, "min_child_samples": 5, "num_leaves": 7},
        random_seed=3,
        walk_forward_config=WalkForwardConfig(
            mode=mode,
            train_sessions=train_sessions,
            validation_sessions=validation_sessions,
            inner_validation_sessions=inner_validation_sessions,
            step_sessions=validation_sessions,
            min_folds=min_folds,
            max_folds=12,
            inference=BootstrapInferenceConfig(
                bootstrap_block_sessions=5,
                bootstrap_replicates=replicates,
            ),
        ),
        resource_budget=WalkForwardResourceBudget(
            max_seconds=60,
            max_total_physical_rows=100_000,
            max_total_training_samples=100_000,
            max_partitions=100,
            max_lineage_nodes=20,
            max_bootstrap_replicates=100,
        ),
        max_seconds=60,
    )


def _fixture(
    tmp_path,
    *,
    store_root: Path | None = None,
    scenario: str = "stable",
    boundary_feature: bool = False,
    parent_beyond_end: bool = False,
    **request_overrides,
):
    store = DatasetStore(store_root or (tmp_path / "quant.alpha"))
    spec = _factor_spec()
    compiled = compile_factor_spec(spec)
    rng = np.random.default_rng(20261123)
    prices_by_symbol: dict[str, list[float]] = {}
    for instrument_index, symbol in enumerate(SYMBOLS):
        prices = [100 + instrument_index]
        for _ in range(104):
            prices.append(prices[-1] * float(np.exp(rng.normal(scale=0.02))))
        prices_by_symbol[symbol] = prices
    bars: list[dict] = []
    features: list[dict] = []
    for day_index in range(105):
        decision = START + timedelta(days=day_index, hours=21, minutes=1)
        for symbol in SYMBOLS:
            close = prices_by_symbol[symbol][day_index]
            bars.append({
                "instrument_id": symbol,
                "start_at": decision - timedelta(hours=7),
                "end_at": decision - timedelta(minutes=1),
                "available_at": decision,
                "open": close, "high": close * 1.01, "low": close * 0.99, "close": close,
                "volume": 1000, "adjustment": "raw", "source": "synthetic",
                "source_version": "synthetic-v1", "currency": "USD",
            })
            if day_index >= 100:
                continue
            x = rng.normal(size=4)
            if day_index + 6 < 105:
                future_return = prices_by_symbol[symbol][day_index + 6] / prices_by_symbol[symbol][day_index + 1] - 1
                if scenario == "stable":
                    x[0] = -25.0 * future_return + rng.normal(scale=0.05)
                elif scenario == "drift":
                    direction = -1.0 if day_index >= 60 else 1.0
                    x[0] = direction * (-25.0 * future_return) + rng.normal(scale=0.05)
                elif scenario != "no_signal":
                    raise AssertionError(scenario)
            for name, value in zip(spec.outputs, x, strict=True):
                features.append({
                    "decision_at": decision,
                    "instrument_id": symbol,
                    "factor_name": name,
                    "factor_value": float(value),
                    "feature_available_at": decision,
                    "eligible": True,
                    "quality_flags": [],
                    "factor_version": "synthetic-factor-v1",
                })
    labels = stock_forward_labels(bars, horizon=5)
    if boundary_feature:
        decision = DEV_END
        for symbol in SYMBOLS:
            for name in spec.outputs:
                features.append({
                    "decision_at": decision,
                    "instrument_id": symbol,
                    "factor_name": name,
                    "factor_value": 0.0,
                    "feature_available_at": decision,
                    "eligible": True,
                    "quality_flags": [],
                    "factor_version": "boundary-fixture-v1",
                })
    stock_end = DEV_END + (timedelta(days=1) if parent_beyond_end else timedelta(0))
    stock = store.publish_stock(
        StockDatasetRequest(
            source="synthetic", symbols=SYMBOLS, start=START, end=stock_end,
            price_basis="price_basis_unverified", calendar_version="fixture",
        ),
        bars,
        calendar_version="fixture",
        source_version="synthetic-v1",
        data_quality={},
    )
    feature_end = DEV_END if boundary_feature else START + timedelta(days=100)
    feature = store.publish_feature_panel(
        features,
        parent_dataset_id=stock.dataset_id,
        factor_spec_hash=compiled.spec_hash,
        instrument_ids=SYMBOLS,
        start_at=START,
        end_at=feature_end,
        calendar_version="fixture",
        price_basis="price_basis_unverified",
    )
    request = _request(
        store=store,
        feature_set_id=feature.dataset_id,
        stock_dataset_id=stock.dataset_id,
        **request_overrides,
    )
    return store, request, labels, feature, stock


def _plan(store, request, feature):
    frame, _ = _feature_frame(store.read_rows(feature.dataset_id))
    return plan_development_walk_forward(
        request,
        feature_frame=frame,
        feature_manifest=store.load_manifest(feature.dataset_id),
    )


def _run(store, request):
    return run_development_walk_forward(request, store=store)


def test_expanding_and_rolling_plans_are_deterministic_and_non_overlapping(tmp_path):
    store, request, _, feature, _ = _fixture(tmp_path)
    expanding = _plan(store, request, feature)
    repeated = _plan(store, request, feature)
    assert expanding == repeated
    assert len(expanding.folds) == 7
    assert all(fold.mode == "expanding" for fold in expanding.folds)
    assert [fold.train_start for fold in expanding.folds] == [expanding.folds[0].train_start] * len(expanding.folds)
    for left, right in zip(expanding.folds, expanding.folds[1:], strict=False):
        assert left.outer_validation_end <= right.outer_validation_start

    rolling_request = request.model_copy(update={
        "walk_forward_config": request.walk_forward_config.model_copy(update={"mode": "rolling"}),
    })
    rolling = _plan(store, rolling_request, feature)
    assert all(fold.mode == "rolling" for fold in rolling.folds)
    assert rolling.folds[1].train_start > rolling.folds[0].train_start
    assert all(
        (fold.train_end - fold.train_start) >= timedelta(days=29)
        for fold in rolling.folds
    )


def test_duplicate_decision_session_timestamp_is_rejected():
    index = pd.MultiIndex.from_tuples([
        (pd.Timestamp("2024-01-02T21:01:00Z"), "S00"),
        (pd.Timestamp("2024-01-02T22:01:00Z"), "S01"),
    ], names=["decision_at", "instrument_id"])
    frame = pd.DataFrame({"f1": [1.0, 2.0]}, index=index)
    with pytest.raises(AlphaError, match="exactly one decision timestamp"):
        _decision_sessions(
            frame,
            WalkForwardDevelopmentWindow(
                development_start=START,
                development_end=DEV_END,
            ),
        )


def test_incomplete_tail_is_explicit_and_fold_count_is_not_truncated(tmp_path):
    store, request, _, feature, _ = _fixture(tmp_path, train_sessions=31)
    plan = _plan(store, request, feature)
    assert len(plan.folds) == 6
    assert plan.exclusions == [{
        "reason": "INCOMPLETE_VALIDATION_WINDOW",
        "remaining_session_count": 9,
        "required_validation_sessions": 10,
    }]
    too_many = request.model_copy(update={
        "walk_forward_config": request.walk_forward_config.model_copy(update={"max_folds": 3}),
    })
    with pytest.raises(AlphaError) as error:
        _plan(store, too_many, feature)
    assert error.value.code == AlphaErrorCode.RESOURCE_LIMIT_EXCEEDED


def test_fewer_than_minimum_complete_folds_reports_insufficient_evidence(tmp_path):
    store, request, _labels, _, _ = _fixture(tmp_path, train_sessions=75)
    report = _run(store, request)
    assert len(report.folds) == 2
    assert report.research_status == "WF_INSUFFICIENT_EVIDENCE"


def test_label_purge_uses_event_times_and_strict_cutoff(tmp_path):
    store, request, labels, feature, _ = _fixture(tmp_path)
    plan = _plan(store, request, feature)
    first = plan.folds[0]
    frame, _ = _feature_frame(store.read_rows(feature.dataset_id))
    features = _feature_rows(frame)
    label_frame = _label_frame(labels, request)
    eligible = _join_window(
        _subset(features, first.train_start, first.train_end),
        _subset(label_frame, first.train_start, first.train_end),
        label_available_before=first.knowledge_cutoff,
        exit_before=first.knowledge_cutoff,
    )
    assert not eligible.empty
    assert (eligible["exit_at"] < pd.Timestamp(first.knowledge_cutoff)).all()
    assert (eligible["label_available_at"] < pd.Timestamp(first.knowledge_cutoff)).all()
    boundary = labels[0].model_copy(update={
        "entry_at": first.knowledge_cutoff - timedelta(days=2),
        "exit_at": first.knowledge_cutoff - timedelta(microseconds=1),
        "label_available_at": first.knowledge_cutoff,
    })
    boundary_frame = _label_frame([boundary], request)
    strict = _join_window(
        features[features["instrument_id"] == boundary.instrument_id],
        boundary_frame,
        label_available_before=first.knowledge_cutoff,
        exit_before=first.knowledge_cutoff,
    )
    assert strict.empty


def test_nested_early_stopping_never_receives_outer_validation(tmp_path, monkeypatch):
    store, request, _labels, _, _ = _fixture(tmp_path)
    calls = []
    original_train = lgb.train

    def spy(params, train_set, *args, **kwargs):
        calls.append({
            "train_rows": len(train_set.get_label()),
            "valid_sets": [len(item.get_label()) for item in kwargs.get("valid_sets", [])],
            "num_boost_round": kwargs.get("num_boost_round"),
        })
        return original_train(params, train_set, *args, **kwargs)

    monkeypatch.setattr(lgb, "train", spy)
    report = _run(store, request)
    tuning_calls = [call for call in calls if call["valid_sets"]]
    final_calls = [call for call in calls if not call["valid_sets"]]
    assert len(tuning_calls) == len(report.folds)
    assert len(final_calls) == len(report.folds)
    assert all(call["valid_sets"] for call in tuning_calls)
    assert all(call["num_boost_round"] == 20 for call in tuning_calls)
    assert all(fold.selected_iteration is not None for fold in report.folds)
    assert all(fold.selected_iteration <= 20 for fold in report.folds)


def test_outer_validation_labels_do_not_change_nested_model_identity():
    from types import SimpleNamespace

    rng = np.random.default_rng(44)
    schema = ["f1", "f2", "f3", "f4"]

    def frame(days: int, start_day: int) -> pd.DataFrame:
        rows = []
        for day in range(start_day, start_day + days):
            decision = START + timedelta(days=day, hours=21, minutes=1)
            for instrument in range(10):
                values = rng.normal(size=4)
                rows.append({
                    "decision_at": decision,
                    "instrument_id": f"S{instrument:02d}",
                    "label_value": float(0.4 * values[0] - 0.2 * values[1]),
                    **{name: float(value) for name, value in zip(schema, values, strict=True)},
                })
        return pd.DataFrame(rows)

    inner_fit = frame(12, 0)
    inner_tuning = frame(5, 12)
    outer_train = frame(20, 17)
    outer_validation = frame(6, 37)
    request = SimpleNamespace(
        lightgbm_hyperparameters={"n_estimators": 20, "min_child_samples": 2, "num_leaves": 7},
        random_seed=3,
    )
    first = _fit_nested_lightgbm(inner_fit, inner_tuning, outer_train, outer_validation, schema, request)
    changed_validation = outer_validation.copy()
    changed_validation["label_value"] = -changed_validation["label_value"]
    second = _fit_nested_lightgbm(inner_fit, inner_tuning, outer_train, changed_validation, schema, request)
    assert first[1:] == second[1:]
    assert np.allclose(first[0], second[0])


def test_ridge_preprocessing_is_refit_for_every_fold(tmp_path, monkeypatch):
    import stock_quant.alpha.models.walk_forward as module

    store, request, _labels, _, _ = _fixture(tmp_path)
    observed = []
    original = module.fit_train_only_ridge

    def spy(train, validation, schema, alpha):
        observed.append((len(train), len(validation)))
        return original(train, validation, schema, alpha)

    monkeypatch.setattr(module, "fit_train_only_ridge", spy)
    report = _run(store, request)
    assert len(observed) == len(report.folds)
    assert len({train_count for train_count, _ in observed}) > 1
    ridge_hashes = [fold.model_hashes["ridge_v1"] for fold in report.folds]
    assert len(set(ridge_hashes)) == len(ridge_hashes)


def test_oof_common_cohort_metrics_bootstrap_and_holm_are_bounded_and_deterministic(tmp_path):
    store, request, _labels, _, _ = _fixture(tmp_path)
    first = _run(store, request)
    second = _run(store, request)
    idempotent_copy = request.model_copy(update={"idempotency_key": "caller-retry-key"})
    third = _run(store, idempotent_copy)
    assert first.report_hash == second.report_hash == third.report_hash
    assert first == second == third
    assert first.common_cohort["duplicate_oof_sample_count"] == 0
    assert first.common_cohort["common_cohort_sample_count"] > 0
    assert first.common_cohort["planned_session_count"] == 70
    assert first.common_cohort["valid_label_session_count"] <= first.common_cohort["planned_session_count"]
    assert first.common_cohort["valid_ic_session_count"] <= first.common_cohort["valid_label_session_count"]
    assert first.common_cohort["planned_validation_opportunity_count"] > 0
    assert first.common_cohort["eligible_label_opportunity_count"] > 0
    assert first.common_cohort["conditional_label_coverage"] <= 1
    assert first.common_cohort["conditional_model_coverage"] <= 1
    assert first.common_cohort["end_to_end_common_coverage"] <= 1
    common_samples = {
        candidate: first.pooled_oof_metrics["common_cohort"][candidate]["sample_count"]
        for candidate in ("short_reversal_5", "rsi_5", "ridge_v1", "lightgbm_v1")
    }
    assert len(set(common_samples.values())) == 1
    assert all(value is not None for value in common_samples.values())
    assert first.uncertainty["shared_row_resampling"] is True
    assert {
        row["bootstrap_seed"] for row in first.uncertainty["candidate_rank_ic"].values()
    } == {first.uncertainty["seed"]}
    assert len(first.multiple_testing["hypotheses"]) == 4
    assert first.multiple_testing["family_size"] == 4
    assert first.multiple_testing["multiple_testing_scope"] == "CURRENT_PREREGISTERED_FAMILY_ONLY"
    assert first.multiple_testing["global_trial_history_complete"] is False
    for row in first.multiple_testing["hypotheses"]:
        if row["raw_p_value"] is not None:
            assert row["holm_adjusted_p_value"] >= row["raw_p_value"]
            assert row["confidence_interval"][0] <= row["confidence_interval"][1]
    assert "ALPHA_CONFIRMED" not in str(first.model_dump(mode="json"))
    assert "PRODUCTION_READY" not in str(first.model_dump(mode="json"))


def test_pooled_coverage_separates_planned_label_and_model_denominators(tmp_path):
    _store, request, _, _, _ = _fixture(tmp_path)
    decision = pd.Timestamp("2024-02-01T21:01:00Z")
    frame = pd.DataFrame({
        "decision_at": [decision, decision],
        "instrument_id": ["S00", "S01"],
        "label_value": [0.1, -0.2],
        "short_reversal_5": [0.1, -0.1],
        "rsi_5": [0.2, -0.2],
        "ridge_v1": [0.3, -0.3],
        "lightgbm_v1": [0.4, -0.4],
    })
    _, summary, _, _, _ = _pooled_metrics(
        frame,
        frame,
        [decision.to_pydatetime()],
        {decision: 4},
        request,
        "sha256:" + "0" * 64,
    )
    assert summary["planned_session_count"] == 1
    assert summary["valid_label_session_count"] == 1
    assert summary["valid_ic_session_count"] == 1
    assert summary["planned_validation_opportunity_count"] == 4
    assert summary["eligible_label_opportunity_count"] == 2
    assert summary["conditional_label_coverage"] == pytest.approx(0.5)
    assert summary["conditional_model_coverage"] == pytest.approx(1.0)
    assert summary["end_to_end_common_coverage"] == pytest.approx(0.5)
    assert summary["common_cohort_coverage"] == pytest.approx(0.5)


def test_duplicate_oof_sample_identity_fails_instead_of_double_counting():
    frame = pd.DataFrame({
        "decision_at": [pd.Timestamp("2024-01-01T21:01:00Z")],
        "instrument_id": ["S00"],
        "label_value": [0.1],
        "short_reversal_5": [0.1],
        "rsi_5": [0.2],
        "ridge_v1": [0.3],
        "lightgbm_v1": [0.4],
        "fold_id": ["sha256:" + "1" * 64],
    })
    with pytest.raises(AlphaError, match="duplicate OOF sample"):
        _concat_frames([frame, frame], label="Common cohort OOF")


def test_bootstrap_seed_and_fixed_family_holm_are_reproducible():
    values = [0.1, 0.2, None, -0.1, 0.3, 0.05, 0.2, -0.2]
    config = BootstrapInferenceConfig(
        bootstrap_block_sessions=2,
        bootstrap_replicates=17,
        confidence_level=0.95,
    )
    first = _bootstrap_series(values, config=config, seed=4, with_pvalue=True)
    second = _bootstrap_series(values, config=config, seed=4, with_pvalue=True)
    assert first == second
    assert len(_moving_block_draws(values, block_sessions=2, replicates=17, seed=4)) == 17
    degenerate = _bootstrap_series([0.1] * 10, config=config, seed=4, with_pvalue=True)
    assert degenerate["p_value"] is None
    assert degenerate["lower"] is None and degenerate["upper"] is None
    assert degenerate["reason"] == "DEGENERATE_BOOTSTRAP_DISTRIBUTION"
    with pytest.raises(AlphaError, match="bootstrap_block_sessions"):
        _bootstrap_series([0.1, 0.2], config=BootstrapInferenceConfig(bootstrap_block_sessions=3), seed=4)
    records = [
        {"hypothesis_id": "H1", "raw_p_value": 0.03},
        {"hypothesis_id": "H2", "raw_p_value": None},
        {"hypothesis_id": "H3", "raw_p_value": 0.01},
        {"hypothesis_id": "H4", "raw_p_value": 0.04},
    ]
    adjusted = holm_adjust(records)
    assert all(row["family_size"] == 4 for row in adjusted)
    assert next(row for row in adjusted if row["hypothesis_id"] == "H2")["holm_adjusted_p_value"] is None
    assert next(row for row in adjusted if row["hypothesis_id"] == "H3")["holm_adjusted_p_value"] == pytest.approx(0.04)
    assert next(row for row in adjusted if row["hypothesis_id"] == "H1")["holm_adjusted_p_value"] == pytest.approx(0.09)
    assert next(row for row in adjusted if row["hypothesis_id"] == "H4")["holm_adjusted_p_value"] == pytest.approx(0.09)


def test_time_drift_is_visible_in_late_decay_without_automatic_direction_reversal(tmp_path):
    store, request, _labels, _, _ = _fixture(tmp_path, scenario="drift")
    report = _run(store, request)
    reversal = report.stability["candidates"]["short_reversal_5"]
    assert reversal["time_decay"]["early"] is not None
    assert reversal["time_decay"]["late"] is not None
    assert reversal["time_decay"]["late"] < reversal["time_decay"]["early"]
    assert all(
        fold.feature_importance.get("status") in {"AVAILABLE", "UNAVAILABLE"}
        for fold in report.folds
    )
    assert "AUTO_REVERSED" not in str(report.model_dump(mode="json"))


def test_no_signal_scenario_returns_walk_forward_evidence_without_certification(tmp_path):
    store, request, _labels, _, _ = _fixture(tmp_path, scenario="no_signal")
    report = _run(store, request)
    assert report.research_status in {"WF_EXPLORATORY", "WF_INSUFFICIENT_EVIDENCE"}
    assert len(report.folds) >= 3
    assert report.report_hash.startswith("sha256:")
    assert "TRADING_VALIDATED" not in str(report.model_dump(mode="json"))


def test_preflight_rejects_boundary_rows_before_partition_read(tmp_path, monkeypatch):
    store, request, _, _, _ = _fixture(tmp_path, boundary_feature=True)
    from stock_quant.alpha.data import catalog as catalog_module

    reads = []

    def read_spy(*args, **kwargs):
        reads.append((args, kwargs))
        raise AssertionError("partition rows must not be read before physical isolation")

    monkeypatch.setattr(catalog_module, "read_partition", read_spy)
    with pytest.raises(AlphaError) as error:
        _preflight(request, store)
    assert error.value.code == AlphaErrorCode.QUALITY_SCOPE_NOT_ISOLATED
    assert reads == []


def test_preflight_rejects_parent_manifest_beyond_development_end(tmp_path, monkeypatch):
    store, request, _, _, _ = _fixture(tmp_path, parent_beyond_end=True)
    from stock_quant.alpha.data import catalog as catalog_module

    reads = []
    monkeypatch.setattr(catalog_module, "read_partition", lambda *args, **kwargs: reads.append(args))
    with pytest.raises(AlphaError) as error:
        _preflight(request, store)
    assert error.value.code == AlphaErrorCode.QUALITY_SCOPE_NOT_ISOLATED
    assert reads == []


def test_direct_run_admission_rejects_legacy_mode_before_dataset_read(tmp_path, monkeypatch):
    store, request, _labels, _, _ = _fixture(tmp_path)
    invalid = request.model_copy(update={"evaluation_mode": "legacy_inline_test"})
    observed = []
    monkeypatch.setattr(DatasetStore, "load_manifest", lambda self, *args, **kwargs: observed.append(args))
    with pytest.raises(ResearchAdmissionError):
        _run(store, invalid)
    assert observed == []


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"evaluation_mode": None}, "RESEARCH_MODE_REQUIRED"),
        ({"evaluation_mode": "legacy_inline_test"}, "LEGACY_INLINE_TEST_DISABLED"),
        ({"test_start": "2024-01-01T00:00:00Z"}, "TEST_WINDOW_NOT_ALLOWED"),
        ({"research_protocol_hash": None}, "RESEARCH_PROTOCOL_REQUIRED"),
        ({"stock_dataset_id": None, "label_spec": {"stock_dataset_id": None}}, "STOCK_LABEL_DATASET_REQUIRED"),
    ],
)
def test_raw_walk_forward_admission_rejects_unsafe_fields(override, reason):
    spec = _factor_spec().model_dump(mode="json")
    raw = {
        "kind": "walk_forward_alpha",
        "evaluation_mode": "validation_only",
        "research_protocol_hash": "sha256:" + "1" * 64,
        "feature_set_id": "sha256:" + "2" * 64,
        "stock_dataset_id": "sha256:" + "3" * 64,
        "label_spec": {
            "label_name": "stock_forward_return", "target_type": "stock_return",
            "stock_dataset_id": "sha256:" + "3" * 64, "horizon_days": 5,
        },
        "development_window": {
            "development_start": "2024-01-01T00:00:00Z", "development_end": "2024-06-01T00:00:00Z",
        },
        "baseline_comparison": {
            "schema_version": "1.0", "factor_spec": spec,
            "reversal_output": "reversal_source", "rsi_output": "rsi_source", "ridge_alpha": 1.0,
        },
        "walk_forward_config": {
            "mode": "expanding", "train_sessions": 30, "validation_sessions": 10,
            "inner_validation_sessions": 5, "step_sessions": 10,
        },
    }
    raw.update(override)
    with pytest.raises(ResearchAdmissionError) as error:
        require_walk_forward_submission_raw(raw)
    assert error.value.code.value == reason


def test_direct_run_rejects_external_label_rows(tmp_path):
    store, request, labels, _, _ = _fixture(tmp_path)
    with pytest.raises(TypeError):
        run_development_walk_forward(request, store=store, label_rows=labels)


def test_preflight_requires_feature_parent_to_equal_stock_label_dataset(tmp_path, monkeypatch):
    store, request, _, _, _ = _fixture(tmp_path)
    from stock_quant.alpha.data import catalog as catalog_module

    reads = []
    monkeypatch.setattr(catalog_module, "read_partition", lambda *args, **kwargs: reads.append(args))
    bars = []
    for index, symbol in enumerate(SYMBOLS):
        at = START + timedelta(hours=21, minutes=1)
        bars.append({
            "instrument_id": symbol,
            "start_at": at - timedelta(hours=7),
            "end_at": at - timedelta(minutes=1),
            "available_at": at,
            "open": 100 + index, "high": 101 + index, "low": 99 + index, "close": 100 + index,
            "volume": 1000, "adjustment": "raw", "source": "other-stock",
            "source_version": "other-v1", "currency": "USD",
        })
    other_stock = store.publish_stock(
        StockDatasetRequest(
            source="other-stock", symbols=SYMBOLS, start=START, end=DEV_END,
            price_basis="price_basis_unverified", calendar_version="fixture",
        ),
        bars,
        calendar_version="fixture",
        source_version="other-v1",
        data_quality={},
    )
    mismatched = request.model_copy(update={
        "stock_dataset_id": other_stock.dataset_id,
        "label_spec": request.label_spec.model_copy(update={"stock_dataset_id": other_stock.dataset_id}),
    })
    with pytest.raises(AlphaError, match="derived from the requested stock"):
        _preflight(mismatched, store)
    assert reads == []


def test_walk_forward_job_submission_rejection_is_audited_without_enqueue(tmp_path):
    registry = tmp_path / "quant.db"
    store_root = registry.with_suffix(".alpha")
    _, request, _, _, _ = _fixture(tmp_path, store_root=store_root)
    raw = request.model_dump(mode="json")
    raw["test_start"] = "2024-05-02T00:00:00Z"
    with JobStore(registry) as jobs:
        with pytest.raises(ValueError, match="TEST_WINDOW_NOT_ALLOWED"):
            jobs.submit_alpha(raw)
        assert jobs.connection.execute("SELECT count(*) FROM jobs").fetchone() == (0,)
        event, = jobs.list_alpha_research_audit()
    assert event["event_type"] == "WALK_FORWARD_SUBMISSION_REJECTED"
    assert event["detail"]["reason"] == "TEST_WINDOW_NOT_ALLOWED"
    assert "test_start" in event["detail"]["fields"]


def test_walk_forward_job_publishes_paginated_sections_and_no_model_artifact(tmp_path):
    registry = tmp_path / "quant.db"
    store_root = registry.with_suffix(".alpha")
    _store, request, _, _, _ = _fixture(tmp_path, store_root=store_root)
    with JobStore(registry) as jobs:
        submission = jobs.submit_alpha(request.model_dump(mode="json"))
        assert execute_job(jobs, jobs.claim()) is True
        job = jobs.get_job(submission["job_id"])
        summary = jobs.get_result(submission["job_id"], "summary")
        fold_page = jobs.get_result(submission["job_id"], "walk_forward", 2)
        trials = jobs.get_result(submission["job_id"], "trials")
        diagnostics = jobs.get_result(submission["job_id"], "diagnostics", 5)
        all_folds = jobs.get_result(submission["job_id"], "walk_forward", 100)
        reproducibility = jobs.get_result(submission["job_id"], "reproducibility")
    assert job["status"] == "completed"
    assert summary["research_status"] in {"WF_EXPLORATORY", "WF_INSUFFICIENT_EVIDENCE", "WF_COMPLETED"}
    assert fold_page["total_count"] >= 3
    assert len(fold_page["rows"]) <= 2
    assert fold_page["next_cursor"]
    assert trials["total_count"] == 4
    assert diagnostics["total_count"] > 0
    assert reproducibility["walk_forward_protocol_hash"].startswith("sha256:")
    reconstruction = reproducibility["report_reconstruction"]["report_without_folds"]
    rebuilt = WalkForwardReport.model_validate({**reconstruction, "folds": all_folds["rows"]})
    assert rebuilt.report_hash == summary["report_hash"]
    assert reproducibility["label_generation"]["source_dataset_id"] == request.stock_dataset_id
    assert reproducibility["label_generation"]["external_label_rows_accepted"] is False
    assert not list(registry.with_suffix(".models").glob("*/metadata.json"))


def test_queued_legacy_walk_forward_worker_request_is_rejected_before_reads(tmp_path, monkeypatch):
    from stock_quant.compiler import digest

    registry = tmp_path / "quant.db"
    raw = {
        "kind": "walk_forward_alpha",
        "evaluation_mode": "legacy_inline_test",
        "research_protocol_hash": "sha256:" + "1" * 64,
        "stock_dataset_id": "sha256:" + "2" * 64,
    }
    with JobStore(registry) as jobs:
        jobs.connection.execute(
            "INSERT INTO jobs(id,status,request,config_hash) VALUES (?, 'queued', ?, ?)",
            ("alpha_legacy_walk_forward", json.dumps(raw), digest(raw)),
        )
        jobs.connection.commit()
        reads = []
        monkeypatch.setattr(DatasetStore, "load_manifest", lambda self, *args, **kwargs: reads.append(args))
        assert execute_job(jobs, jobs.claim()) is True
        job = jobs.get_job("alpha_legacy_walk_forward")
    assert job["status"] == "failed"
    assert job["error_code"] == AlphaErrorCode.LEGACY_INLINE_TEST_DISABLED.value
    assert reads == []


def test_fold_models_never_enter_registry_across_direct_runs(tmp_path):
    store, request, _labels, _, _ = _fixture(tmp_path)
    _run(store, request)
    _run(store, request)
    registry_root = tmp_path / "models"
    assert not registry_root.exists()
