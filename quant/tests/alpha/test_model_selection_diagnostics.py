from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest
from pydantic import ValidationError

from stock_quant.alpha.contracts import StockDatasetRequest
from stock_quant.alpha.data.catalog import DatasetStore
from stock_quant.alpha.models.labels import LabelRow
from stock_quant.alpha.models.lightgbm_trainer import (
    AlphaModelRegistry,
    ModelResourceBudget,
    TrainAlphaRequest,
    select_candidate_iteration,
    train_alpha,
)
from stock_quant.alpha.models.signal_diagnostics import DirectSignalSpec
from stock_quant.alpha.models.temporal_split import ModelDevelopmentSplit, TemporalSplit


def _fixture(tmp_path, *, n_estimators=6, seed=7, constant_features=False):
    store = DatasetStore(tmp_path / "alpha")
    rng = np.random.default_rng(20261121)
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    feature_rows, label_rows = [], []
    for day in range(100):
        decision_at = start + timedelta(days=day, hours=1)
        for instrument in range(20):
            x1, x2, x3, x4 = (0.0, 0.0, 0.0, 0.0) if constant_features else rng.normal(size=4)
            target = 0.8 * x1 + 0.2 * x2 - 0.1 * x3 + 0.05 * rng.normal()
            for name, value in (
                ("f1", x1), ("f2", x2), ("f3", x3), ("f4", x4),
                ("oracle", 0.0 if constant_features else target),
            ):
                feature_rows.append({
                    "decision_at": decision_at,
                    "instrument_id": f"S{instrument:02d}",
                    "factor_name": name,
                    "factor_value": float(value),
                    "feature_available_at": decision_at,
                    "eligible": True,
                    "quality_flags": [],
                    "factor_version": "test-v1",
                })
            label_rows.append(LabelRow(
                feature_decision_at=decision_at,
                entry_at=decision_at + timedelta(days=1),
                exit_at=decision_at + timedelta(days=5),
                label_available_at=decision_at + timedelta(days=5),
                label_value=float(target),
                label_quality=[],
                instrument_id=f"S{instrument:02d}",
                label_name="stock_forward_return",
            ))
    parent = store.publish_stock(
        StockDatasetRequest(
            source="fixture", symbols=["S00"], start=start,
            end=start + timedelta(days=1),
        ),
        [{
            "instrument_id": "S00", "start_at": start,
            "end_at": start + timedelta(hours=6),
            "available_at": start + timedelta(hours=6),
            "open": 100, "high": 101, "low": 99, "close": 100,
            "volume": 1, "adjustment": "raw", "source": "fixture",
            "source_version": "v1", "currency": "USD",
        }],
        calendar_version="fixture", source_version="v1", data_quality={},
    )
    feature_set = store.publish_feature_panel(
        feature_rows,
        parent_dataset_id=parent.dataset_id,
        factor_spec_hash="sha256:" + "b" * 64,
        instrument_ids=[f"S{i:02d}" for i in range(20)],
        start_at=start,
        end_at=start + timedelta(days=101),
        calendar_version="fixture",
        price_basis="price_basis_unverified",
    )
    split = ModelDevelopmentSplit(
        train_start=start,
        train_end=start + timedelta(days=70),
        validation_start=start + timedelta(days=70),
        validation_end=start + timedelta(days=90),
        embargo_days=0,
    )
    request = TrainAlphaRequest(
        feature_set_id=feature_set.dataset_id,
        label_source_dataset_id=parent.dataset_id,
        label_name="stock_forward_return",
        target_type="stock_return",
        evaluation_mode="validation_only",
        research_protocol_hash="sha256:" + "0" * 64,
        split=split,
        hyperparameters={
            "learning_rate": 0.08,
            "num_leaves": 7,
            "min_child_samples": 5,
            "n_estimators": n_estimators,
        },
        random_seed=seed,
        resource_budget=ModelResourceBudget(max_seconds=60, max_rows=10000),
        min_rank_ic=-1,
    )
    return store, feature_set, split, request, label_rows


def _train(tmp_path, request, labels, *, name="models", validation_sessions=None, store=None):
    return train_alpha(
        request,
        store=store or DatasetStore(tmp_path / "alpha"),
        registry=AlphaModelRegistry(tmp_path / name),
        label_rows=labels,
        validation_sessions=validation_sessions,
    )


@pytest.mark.parametrize(
    ("selection_metric", "candidate_metric", "choose_maximum"),
    [
        ("l2", "l2_rmse", False),
        ("daily_rank_ic", "daily_rank_ic", True),
        ("weekly_rank_ic", "weekly_rank_ic", True),
    ],
)
def test_iterations_are_contiguous_and_selection_uses_one_tree_path(
    tmp_path, selection_metric, candidate_metric, choose_maximum,
):
    store, _, _, request, labels = _fixture(tmp_path)
    request = request.model_copy(update={"selection_metric": selection_metric})
    manifest = _train(tmp_path, request, labels, name=f"models-{selection_metric}")
    diagnostics = manifest.selection_diagnostics

    assert diagnostics is not None
    assert diagnostics["schema_version"] == "validation-selection-v1"
    assert diagnostics["tree_path"] == "single_booster_all_candidate_iterations"
    assert diagnostics["tie_rule"] == "first_iteration_on_exact_tie"
    assert diagnostics["candidate_iteration_start"] == 1
    assert diagnostics["candidate_iteration_end"] == 6
    assert diagnostics["candidate_iteration_count"] == 6
    assert [record["iteration"] for record in diagnostics["candidates"]] == list(range(1, 7))

    values = [record[candidate_metric] for record in diagnostics["candidates"]]
    expected = (max if choose_maximum else min)(values)
    expected_iteration = next(
        record["iteration"] for record in diagnostics["candidates"]
        if record[candidate_metric] == expected
    )
    assert diagnostics["selected_iteration"] == expected_iteration
    assert manifest.selected_iteration == expected_iteration
    assert diagnostics["evaluated_counts"][candidate_metric] == 6
    assert diagnostics["undefined_counts"][candidate_metric] == 0


def test_exact_ties_choose_first_iteration_and_undefined_values_are_never_selected():
    candidates = [
        {
            "iteration": 1, "l2_rmse": 0.5, "daily_rank_ic": None,
            "weekly_rank_ic": None,
            "defined": {"l2_rmse": True, "daily_rank_ic": False, "weekly_rank_ic": False},
            "undefined_reasons": {
                "l2_rmse": [], "daily_rank_ic": ["CONSTANT_PREDICTION"],
                "weekly_rank_ic": ["NO_OBSERVATIONS"],
            },
        },
        {
            "iteration": 2, "l2_rmse": 0.5, "daily_rank_ic": 0.25,
            "weekly_rank_ic": 0.1,
            "defined": {"l2_rmse": True, "daily_rank_ic": True, "weekly_rank_ic": True},
            "undefined_reasons": {"l2_rmse": [], "daily_rank_ic": [], "weekly_rank_ic": []},
        },
        {
            "iteration": 3, "l2_rmse": 0.7, "daily_rank_ic": 0.25,
            "weekly_rank_ic": 0.1,
            "defined": {"l2_rmse": True, "daily_rank_ic": True, "weekly_rank_ic": True},
            "undefined_reasons": {"l2_rmse": [], "daily_rank_ic": [], "weekly_rank_ic": []},
        },
    ]

    assert select_candidate_iteration(candidates, "l2") == 1
    assert select_candidate_iteration(candidates, "daily_rank_ic") == 2
    assert select_candidate_iteration(candidates, "weekly_rank_ic") == 2

    undefined = [
        {
            "iteration": iteration, "l2_rmse": 1.0, "daily_rank_ic": None,
            "weekly_rank_ic": None,
            "defined": {"l2_rmse": True, "daily_rank_ic": False, "weekly_rank_ic": False},
            "undefined_reasons": {
                "l2_rmse": [], "daily_rank_ic": ["CONSTANT_PREDICTION"],
                "weekly_rank_ic": ["CONSTANT_PREDICTION"],
            },
        }
        for iteration in range(1, 4)
    ]
    with pytest.raises(ValueError, match="no finite daily_rank_ic"):
        select_candidate_iteration(undefined, "daily_rank_ic")


def test_undefined_ic_candidates_keep_reasons_and_l2_can_still_be_selected(tmp_path):
    store, _, _, request, labels = _fixture(tmp_path, n_estimators=3, constant_features=True)
    manifest = _train(tmp_path, request, labels)
    diagnostics = manifest.selection_diagnostics

    assert diagnostics["undefined_counts"]["daily_rank_ic"] == 3
    assert diagnostics["undefined_counts"]["weekly_rank_ic"] == 3
    assert manifest.selected_iteration == select_candidate_iteration(diagnostics["candidates"], "l2")
    for candidate in diagnostics["candidates"]:
        assert candidate["daily_rank_ic"] is None
        assert candidate["weekly_rank_ic"] is None
        assert "NO_FINITE_DAILY_RANK_IC" in candidate["undefined_reasons"]["daily_rank_ic"]
        assert "CONSTANT_PREDICTION" in candidate["undefined_reasons"]["daily_rank_ic"]
        assert "NO_FINITE_WEEKLY_RANK_IC" in candidate["undefined_reasons"]["weekly_rank_ic"]


def test_same_configuration_and_seed_reproduce_iteration_and_validation_predictions(tmp_path):
    store, _, _, request, labels = _fixture(tmp_path, n_estimators=5, seed=19)
    first = _train(tmp_path, request, labels, name="models-first")
    second = _train(tmp_path, request, labels, name="models-second")

    assert first.selected_iteration == second.selected_iteration
    assert (
        first.selection_diagnostics["selected_validation_prediction_hash"]
        == second.selection_diagnostics["selected_validation_prediction_hash"]
    )
    assert first.model_id == second.model_id


def test_direct_baseline_comparison_has_positive_and_negative_deltas_and_keeps_denominators(tmp_path):
    store, _, _, request, labels = _fixture(tmp_path, n_estimators=5)
    positive_spec = DirectSignalSpec(
        factor_name="f1", score_multiplier=1, version="direct-factor-v1",
    )
    negative_spec = positive_spec.model_copy(update={"factor_name": "oracle"})
    positive = _train(
        tmp_path,
        request.model_copy(update={
            "selection_metric": "daily_rank_ic", "baseline_signal": positive_spec,
        }),
        labels,
        name="models-positive-baseline",
    ).baseline_comparison
    negative = _train(
        tmp_path,
        request.model_copy(update={
            "selection_metric": "daily_rank_ic", "baseline_signal": negative_spec,
        }),
        labels,
        name="models-negative-baseline",
    ).baseline_comparison

    assert positive is not None and negative is not None
    assert positive["schema_version"] == "validation-baseline-comparison-v1"
    assert positive["comparison"] == "model_minus_baseline_on_common_samples"
    assert positive["daily"]["delta"] > 0
    assert negative["daily"]["delta"] < 0
    assert positive["weekly"]["common_sample_count"] > 0
    assert negative["weekly"]["common_sample_count"] > 0
    assert positive["daily"]["common_sample_count"] == negative["daily"]["common_sample_count"]
    assert positive["weekly"]["common_sample_count"] == negative["weekly"]["common_sample_count"]
    assert positive["daily"]["model_sample_count"] == negative["daily"]["model_sample_count"]
    assert positive["weekly"]["model_sample_count"] == negative["weekly"]["model_sample_count"]
    assert positive["daily"]["baseline_sample_count"] > 0
    assert positive["weekly"]["baseline_sample_count"] > 0
    assert 0 <= positive["daily"]["baseline_coverage"] <= 1
    assert 0 <= positive["weekly"]["baseline_coverage"] <= 1
    assert positive["daily"]["model_coverage"] == negative["daily"]["model_coverage"]
    assert positive["weekly"]["model_coverage"] == negative["weekly"]["model_coverage"]
    assert positive["daily"]["common_sample_count"] <= positive["daily"]["model_sample_count"]
    assert positive["weekly"]["common_sample_count"] <= positive["weekly"]["model_sample_count"]


def test_weekly_selection_uses_supplied_actual_session_grid_without_friday_fallback(tmp_path):
    store, _, split, request, labels = _fixture(tmp_path, n_estimators=4)
    validation_sessions = []
    current = split.validation_start
    while current < split.validation_end:
        day = current.date()
        # 2024-03-15 is a Friday omitted from this synthetic holiday-shortened grid.
        if day.weekday() < 5 and day != date(2024, 3, 15):
            validation_sessions.append(day)
        current += timedelta(days=1)
    manifest = _train(
        tmp_path,
        request.model_copy(update={"selection_metric": "weekly_rank_ic"}),
        labels,
        validation_sessions=validation_sessions,
    )
    diagnostics = manifest.selection_diagnostics

    assert "2024-03-14" in diagnostics["validation_grid"]["weekly_decision_dates"]
    assert "2024-03-15" not in diagnostics["validation_grid"]["weekly_decision_dates"]
    assert diagnostics["validation_grid"]["source"] == "mapped_validation_sessions"
    assert diagnostics["validation_grid"]["daily_decision_dates"] == [
        session.isoformat() for session in validation_sessions
    ]


def test_selection_diagnostics_read_only_through_validation_cutoff(tmp_path):
    store, _, split, request, labels = _fixture(tmp_path, n_estimators=3)
    observed = []
    original = store.read_rows

    def spy(dataset_id, columns=None, start_at=None, end_at=None):
        observed.append((dataset_id, start_at, end_at))
        return original(dataset_id, columns, start_at, end_at)

    store.read_rows = spy
    sentinel = LabelRow(
        feature_decision_at=datetime(2015, 1, 2, tzinfo=timezone.utc),
        entry_at=datetime(2015, 1, 3, tzinfo=timezone.utc),
        exit_at=datetime(2015, 1, 8, tzinfo=timezone.utc),
        label_available_at=datetime(2015, 1, 8, tzinfo=timezone.utc),
        label_value=1e9,
        label_quality=[],
        instrument_id="S00",
        label_name="stock_forward_return",
    )
    manifest = _train(tmp_path, request, [*labels, sentinel], store=store)

    assert observed
    assert all(end is None or end <= split.validation_end for _, _, end in observed)
    assert "test_window" not in manifest.model_dump(mode="json") or manifest.test_window is None
    assert "oos" not in manifest.metrics


def test_selection_options_are_validation_only_contract_fields(tmp_path):
    store, _, split, request, labels = _fixture(tmp_path)
    legacy_split = TemporalSplit(
        train_start=split.train_start,
        train_end=split.train_end,
        validation_start=split.validation_start,
        validation_end=split.validation_end,
        test_start=split.validation_end,
        test_end=split.validation_end + timedelta(days=10),
    )
    with pytest.raises(ValidationError, match="selection metrics are validation-only"):
        TrainAlphaRequest.model_validate({
            **request.model_dump(mode="python"),
            "evaluation_mode": "legacy_inline_test",
            "split": legacy_split,
            "selection_metric": "daily_rank_ic",
        })
    with pytest.raises(ValidationError, match="baseline_signal is validation-only"):
        TrainAlphaRequest.model_validate({
            **request.model_dump(mode="python"),
            "evaluation_mode": "legacy_inline_test",
            "split": legacy_split,
            "baseline_signal": {"factor_name": "f1", "score_multiplier": 1, "version": "direct-factor-v1"},
        })


def test_legacy_inline_test_path_has_no_selection_diagnostics(tmp_path):
    store, _, split, request, labels = _fixture(tmp_path)
    legacy_split = TemporalSplit(
        train_start=split.train_start,
        train_end=split.train_end,
        validation_start=split.validation_start,
        validation_end=split.validation_end,
        test_start=split.validation_end,
        test_end=split.validation_end + timedelta(days=10),
    )
    legacy = request.model_copy(update={"evaluation_mode": "legacy_inline_test", "split": legacy_split})
    manifest = train_alpha(
        legacy,
        store=DatasetStore(tmp_path / "alpha"),
        registry=AlphaModelRegistry(tmp_path / "legacy-models"),
        label_rows=labels,
    )

    assert manifest.evaluation_mode == "legacy_inline_test"
    assert manifest.selection_diagnostics is None
    assert manifest.baseline_comparison is None
    assert "oos" in manifest.metrics
