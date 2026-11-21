from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from pydantic import ValidationError

from stock_quant.alpha.contracts import StockDatasetRequest
from stock_quant.alpha.data.catalog import DatasetStore
from stock_quant.alpha.jobs.requests import ALPHA_REQUEST_ADAPTER, TrainAlphaDiagnosticFoldsJob
from stock_quant.alpha.models.labels import LabelRow
from stock_quant.alpha.models.temporal_split import ModelDevelopmentSplit
from stock_quant.alpha.models.window_diagnostics import diagnostic_window_importance
from stock_quant.jobs import JobStore, execute_job


UTC = timezone.utc
START = datetime(2009, 1, 1, tzinfo=UTC)
SPLIT_AT = START + timedelta(days=50)
TRAIN_END = START + timedelta(days=100)
VALIDATION_END = START + timedelta(days=120)
SYMBOLS = [f"S{index:02d}" for index in range(20)]


def _split() -> ModelDevelopmentSplit:
    return ModelDevelopmentSplit(
        train_start=START,
        train_end=TRAIN_END,
        validation_start=TRAIN_END,
        validation_end=VALIDATION_END,
    )


def _window_fixture() -> tuple[list[dict], list[LabelRow]]:
    rng = np.random.default_rng(20261120)
    features = []
    labels = []
    for day in range(100):
        decision_at = START + timedelta(days=day, hours=15)
        first_slice = decision_at < SPLIT_AT
        for instrument in SYMBOLS:
            x1, x2, x3 = rng.normal(size=3)
            target = (
                3.0 * x1 - 2.0 * x2 + 0.2 * x3 if first_slice
                else -2.0 * x1 + 3.0 * x2 + 0.2 * x3
            ) + 0.01 * rng.normal()
            for name, value in (("f1", x1), ("f2", x2), ("f3", x3)):
                features.append({
                    "decision_at": decision_at,
                    "instrument_id": instrument,
                    "factor_name": name,
                    "factor_value": float(value),
                    "feature_available_at": decision_at,
                    "eligible": True,
                    "quality_flags": [],
                    "factor_version": "fixture-v1",
                })
            labels.append(LabelRow(
                feature_decision_at=decision_at,
                entry_at=decision_at + timedelta(hours=1),
                exit_at=decision_at + timedelta(hours=2),
                label_available_at=decision_at + timedelta(hours=3),
                label_value=float(target),
                label_quality=[],
                instrument_id=instrument,
                label_name="stock_forward_return",
                label_version="session-aligned-v2",
            ))
    return features, labels


def _fold_request() -> dict:
    return {
        "kind": "train_alpha_diagnostic_folds",
        "research_protocol_hash": "sha256:" + "0" * 64,
        "feature_set_id": "sha256:" + "1" * 64,
        "stock_dataset_id": "sha256:" + "2" * 64,
        "label_name": "stock_forward_return",
        "target_type": "stock_return",
        "horizon_days": 5,
        "split": _split().model_dump(mode="json"),
        "split_at": SPLIT_AT.isoformat(),
        "hyperparameters": {
            "learning_rate": 0.1,
            "num_leaves": 4,
            "min_child_samples": 2,
            "n_estimators": 5,
        },
        "random_seed": 11,
    }


def _job_fixture(tmp_path) -> tuple[DatasetStore, str, str, dict]:
    store = DatasetStore(tmp_path / "registry.alpha")
    rng = np.random.default_rng(20261121)
    bars = []
    feature_rows = []
    for day in range(112):
        sold_at = START + timedelta(days=day, hours=21, minutes=5)
        opened_at = sold_at - timedelta(hours=6, minutes=30)
        for index, symbol in enumerate(SYMBOLS):
            x1, x2 = rng.normal(size=2)
            open_price = 100.0 + index + day * 0.05 + 2.0 * x1 - x2
            bars.append({
                "instrument_id": symbol,
                "start_at": opened_at,
                "end_at": sold_at - timedelta(minutes=5),
                "available_at": sold_at,
                "open": float(open_price),
                "high": float(open_price + 1.0),
                "low": float(open_price - 1.0),
                "close": float(open_price + 0.1),
                "volume": 1000.0,
                "adjustment": "raw",
                "source": "fixture",
                "source_version": "fixture-v1",
                "currency": "USD",
            })
            for name, value in (("f1", x1), ("f2", x2)):
                feature_rows.append({
                    "decision_at": sold_at,
                    "instrument_id": symbol,
                    "factor_name": name,
                    "factor_value": float(value),
                    "feature_available_at": sold_at,
                    "eligible": True,
                    "quality_flags": [],
                    "factor_version": "fixture-v1",
                })
    stock = store.publish_stock(
        StockDatasetRequest(
            source="fixture", symbols=SYMBOLS, start=START,
            end=START + timedelta(days=112),
        ),
        bars,
        calendar_version="fixture",
        source_version="fixture-v1",
        data_quality={},
    )
    feature_set = store.publish_feature_panel(
        feature_rows,
        parent_dataset_id=stock.dataset_id,
        factor_spec_hash="sha256:" + "a" * 64,
        instrument_ids=SYMBOLS,
        start_at=START,
        end_at=START + timedelta(days=112),
        calendar_version="fixture",
        price_basis="price_basis_unverified",
    )
    raw = {
        **_fold_request(),
        "feature_set_id": feature_set.dataset_id,
        "stock_dataset_id": stock.dataset_id,
    }
    return store, stock.dataset_id, feature_set.dataset_id, raw


def test_diagnostic_folds_fit_independent_deterministic_train_only_models(tmp_path):
    features, labels = _window_fixture()
    kwargs = {
        "split": _split(),
        "split_at": SPLIT_AT,
        "hyperparameters": _fold_request()["hyperparameters"],
        "random_seed": 11,
    }

    first = diagnostic_window_importance(features, labels, **kwargs)
    second = diagnostic_window_importance(features, labels, **kwargs)

    assert first == second
    assert first["status"] == "TESTED"
    assert len(first["folds"]) == 2
    assert [fold["fold_id"] for fold in first["folds"]] == ["train_first", "train_second"]
    assert first["folds"][0]["window"] == {
        "start": START.isoformat(), "end": SPLIT_AT.isoformat(),
    }
    assert first["folds"][1]["window"] == {
        "start": SPLIT_AT.isoformat(), "end": TRAIN_END.isoformat(),
    }
    assert first["folds"][0]["model_hash"] != first["folds"][1]["model_hash"]
    for fold in first["folds"]:
        assert fold["status"] == "TESTED"
        assert fold["label_evidence"]["minimum_decision_at"] >= fold["window"]["start"]
        assert fold["label_evidence"]["maximum_label_available_at"] < fold["window"]["end"]
        assert fold["sample_count"] > 0
        assert sum(fold["normalized_importance"].values()) == pytest.approx(1.0)
    assert set(first["importance_deltas"]) == {"f1", "f2", "f3"}
    assert any(abs(value) > 0 for value in first["importance_deltas"].values())
    assert first["importance_rank_correlation"] is not None
    assert first["stability_claim"] is None
    assert first["definition"] == "independent_train_window_importance_not_model_selection"


def test_diagnostic_folds_return_not_tested_without_two_comparable_fits():
    features, labels = _window_fixture()
    first_slice_only = [
        label for label in labels
        if label.feature_decision_at < SPLIT_AT
    ]

    result = diagnostic_window_importance(
        features,
        first_slice_only,
        split=_split(),
        split_at=SPLIT_AT,
        hyperparameters=_fold_request()["hyperparameters"],
        random_seed=11,
    )

    assert result["status"] == "NOT_TESTED"
    assert result["reason"] == "FEWER_THAN_TWO_COMPARABLE_FITS"
    assert [fold["status"] for fold in result["folds"]] == ["TESTED", "NOT_TESTED"]
    assert result["folds"][1]["reason"] == "NO_MATURED_LABELS"
    assert result["importance_deltas"] is None
    assert result["importance_rank_correlation"] is None
    assert result["stability_claim"] is None


def test_diagnostic_folds_with_no_labels_retain_both_not_tested_windows():
    features, _ = _window_fixture()

    result = diagnostic_window_importance(
        features,
        [],
        split=_split(),
        split_at=SPLIT_AT,
        hyperparameters=_fold_request()["hyperparameters"],
        random_seed=11,
    )

    assert result["status"] == "NOT_TESTED"
    assert result["reason"] == "FEWER_THAN_TWO_COMPARABLE_FITS"
    assert [fold["reason"] for fold in result["folds"]] == ["NO_MATURED_LABELS", "NO_MATURED_LABELS"]
    assert result["importance_deltas"] is None


def test_diagnostic_fold_excludes_label_available_exactly_at_window_end():
    features, labels = _window_fixture()
    boundary_decision = START + timedelta(days=49, hours=15)
    boundary_sentinel = LabelRow(
        feature_decision_at=boundary_decision,
        entry_at=boundary_decision + timedelta(hours=1),
        exit_at=boundary_decision + timedelta(hours=2),
        label_available_at=SPLIT_AT,
        label_value=1.0,
        label_quality=[],
        instrument_id=SYMBOLS[0],
        label_name="stock_forward_return",
        label_version="session-aligned-v2",
    )
    labels = [
        label for label in labels
        if not (label.feature_decision_at == boundary_decision and label.instrument_id == SYMBOLS[0])
    ]
    labels.append(boundary_sentinel)

    result = diagnostic_window_importance(
        features,
        labels,
        split=_split(),
        split_at=SPLIT_AT,
        hyperparameters=_fold_request()["hyperparameters"],
        random_seed=11,
    )

    assert result["folds"][0]["label_evidence"]["maximum_label_available_at"] < SPLIT_AT.isoformat()


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda value: value.update(split_at=START.isoformat()), "split_at must be strictly inside Train"),
        (lambda value: value.update(split_at=TRAIN_END.isoformat()), "split_at must be strictly inside Train"),
        (
            lambda value: value.update(
                split={
                    "train_start": "2011-12-01T00:00:00Z",
                    "train_end": "2012-01-03T00:00:00Z",
                    "validation_start": "2012-01-03T00:00:00Z",
                    "validation_end": "2012-01-10T00:00:00Z",
                },
                split_at="2012-01-02T00:00:00Z",
            ),
            "must not enter reserved Test interval",
        ),
        (lambda value: value.update(test_start="2009-04-01T00:00:00Z"), "Extra inputs are not permitted"),
        (
            lambda value: value["hyperparameters"].update(random_seed=11),
            "random_seed",
        ),
    ],
)
def test_fold_request_rejects_split_outside_train_or_test_interval(mutation, reason):
    raw = _fold_request()
    mutation(raw)

    with pytest.raises(ValidationError, match=reason):
        TrainAlphaDiagnosticFoldsJob.model_validate(raw)
    with pytest.raises(ValidationError, match=reason):
        ALPHA_REQUEST_ADAPTER.validate_python(raw)


def test_fold_job_publishes_paginated_diagnostics_and_reads_only_train(tmp_path, monkeypatch):
    store, stock_id, feature_id, raw = _job_fixture(tmp_path)
    observed_reads = []
    original = DatasetStore.read_rows

    def spy(self, dataset_id, columns=None, start_at=None, end_at=None):
        observed_reads.append((dataset_id, start_at, end_at))
        return original(self, dataset_id, columns, start_at, end_at)

    monkeypatch.setattr(DatasetStore, "read_rows", spy)
    registry_path = tmp_path / "registry.sqlite3"
    with JobStore(registry_path) as jobs:
        submitted = jobs.submit_alpha(raw)
        assert execute_job(jobs, jobs.claim()) is True
        job = jobs.get_job(submitted["job_id"])
        assert job["status"] == "completed", job

        summary = jobs.get_result(submitted["job_id"], "summary")
        assert summary["kind"] == "train_alpha_diagnostic_folds"
        assert summary["status"] == "TESTED"
        assert summary["feature_set_id"] == feature_id
        assert summary["stock_dataset_id"] == stock_id
        assert summary["split_at"] == SPLIT_AT.isoformat()
        assert summary["fold_count"] == 2

        first_page = jobs.get_result(submitted["job_id"], "diagnostics", limit=1)
        assert first_page["total_count"] == 2
        assert len(first_page["rows"]) == 1
        assert first_page["rows"][0]["fold_id"] == "train_first"
        assert first_page["next_cursor"]
        second_page = jobs.get_result(
            submitted["job_id"], "diagnostics", limit=1, cursor=first_page["next_cursor"],
        )
        assert second_page["rows"][0]["fold_id"] == "train_second"
        assert second_page["next_cursor"] is None

        reproducibility = jobs.get_result(submitted["job_id"], "reproducibility")
        assert reproducibility["model_selection_affected"] is False
        assert reproducibility["frozen_candidate_ledger_modified"] is False
        assert reproducibility["test_or_oos_read"] is False
        assert reproducibility["source_windows"] == {
            "feature_start": START.isoformat(),
            "feature_end": TRAIN_END.isoformat(),
            "stock_start": START.isoformat(),
            "stock_end": TRAIN_END.isoformat(),
        }

    assert observed_reads
    assert all(end is None or end <= TRAIN_END for _, _, end in observed_reads)
    assert {dataset_id for dataset_id, _, _ in observed_reads} == {stock_id, feature_id}
    assert store.load_manifest(stock_id)
