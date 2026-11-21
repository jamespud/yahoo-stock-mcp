from __future__ import annotations

import copy
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from stock_quant.alpha.contracts import ModelResourceBudget, StockDatasetRequest
from stock_quant.alpha.data.catalog import DatasetStore
from stock_quant.alpha.errors import AlphaError, AlphaErrorCode
from stock_quant.alpha.jobs.requests import ALPHA_REQUEST_ADAPTER, TrainAlphaModelJob
from stock_quant.alpha.models.diagnostic_protocol import (
    PROTOCOL_HASH,
    SOURCE_SYMBOLS,
    evaluate_frozen_validation_gate,
    frozen_protocol,
    protocol_claim,
    validate_frozen_label_contract,
)
from stock_quant.alpha.models.labels import LabelRow
from stock_quant.alpha.models.lightgbm_trainer import (
    AlphaModelRegistry,
    TrainAlphaRequest,
    train_alpha,
)
from stock_quant.alpha.models.temporal_split import ModelDevelopmentSplit
from stock_quant.alpha.models.window_diagnostics import diagnostic_window_importance


UTC = timezone.utc
FAKE_ID = "sha256:" + "1" * 64
FROZEN_SPLIT = {
    "train_start": "2003-01-01T00:00:00Z",
    "train_end": "2008-01-01T00:00:00Z",
    "validation_start": "2008-01-14T00:00:00Z",
    "validation_end": "2012-01-01T00:00:00Z",
    "embargo_days": 14,
}


def _replace(value, path: tuple[object, ...], replacement):
    current = value
    for key in path[:-1]:
        current = current[key]
    current[path[-1]] = replacement
    return value


def _frozen_training_request() -> dict:
    execution = frozen_protocol()["budgets"]["execution"]["training"]
    return {
        "kind": "train_alpha_model",
        "feature_set_id": FAKE_ID,
        "label_name": "stock_forward_return",
        "target_type": "stock_return",
        "stock_dataset_id": "sha256:" + "2" * 64,
        "evaluation_mode": "validation_only",
        "research_protocol_hash": PROTOCOL_HASH,
        "protocol_mode": "frozen_v1_3",
        "candidate_id": "H01",
        "split": copy.deepcopy(FROZEN_SPLIT),
        "hyperparameters": {
            "learning_rate": 0.03,
            "num_leaves": 7,
            "max_depth": 3,
            "min_child_samples": 100,
            "n_estimators": 150,
        },
        "random_seed": 20261120,
        "resource_budget": {
            "max_seconds": execution["max_seconds"],
            "max_rows": execution["max_rows"],
        },
        "selection_metric": "daily_rank_ic",
        "baseline_signal": {
            "factor_name": "mom63",
            "score_multiplier": 1,
            "version": "direct-factor-v1",
        },
    }


def _training_request_model(raw: dict) -> TrainAlphaRequest:
    return TrainAlphaRequest(
        feature_set_id=raw["feature_set_id"],
        label_source_dataset_id=raw["stock_dataset_id"],
        label_name=raw["label_name"],
        target_type=raw["target_type"],
        evaluation_mode=raw["evaluation_mode"],
        research_protocol_hash=raw["research_protocol_hash"],
        protocol_mode=raw["protocol_mode"],
        candidate_id=raw["candidate_id"],
        split=ModelDevelopmentSplit.model_validate(raw["split"]),
        hyperparameters=raw["hyperparameters"],
        random_seed=raw["random_seed"],
        resource_budget=ModelResourceBudget.model_validate(raw["resource_budget"]),
        min_rank_ic=raw.get("min_rank_ic", 0.03),
        selection_metric=raw["selection_metric"],
        baseline_signal=raw["baseline_signal"],
    )


def test_validation_only_rejects_reserved_test_ranges_at_both_contract_layers():
    raw = _frozen_training_request()
    raw["protocol_mode"] = "synthetic_contract"
    raw["research_protocol_hash"] = "sha256:" + "0" * 64
    raw["candidate_id"] = None
    raw["split"]["validation_end"] = "2012-01-02T00:00:00Z"

    with pytest.raises(ValidationError, match="reserved Test"):
        TrainAlphaModelJob.model_validate(raw)
    with pytest.raises(ValidationError, match="reserved Test"):
        ALPHA_REQUEST_ADAPTER.validate_python(raw)
    with pytest.raises(ValidationError, match="reserved Test"):
        _training_request_model(raw)


def _physical_test_store(tmp_path):
    store = DatasetStore(tmp_path / "physical-test")
    symbols = ["AAA", "BBB"]
    start = datetime(2009, 1, 2, tzinfo=UTC)
    end = datetime(2012, 1, 3, tzinfo=UTC)
    bars = []
    features = []
    for symbol in symbols:
        bars.append({
            "instrument_id": symbol,
            "start_at": start,
            "end_at": start + timedelta(hours=6),
            "available_at": start + timedelta(hours=6, minutes=5),
            "open": 100,
            "high": 101,
            "low": 99,
            "close": 100,
            "volume": 1,
            "adjustment": "raw",
            "source": "fixture",
            "source_version": "fixture-v1",
            "currency": "USD",
        })
        features.append({
            "decision_at": start + timedelta(hours=6, minutes=5),
            "instrument_id": symbol,
            "factor_name": "f1",
            "factor_value": 1.0,
            "feature_available_at": start + timedelta(hours=6, minutes=5),
            "eligible": True,
            "quality_flags": [],
            "factor_version": "fixture-v1",
        })
    stock = store.publish_stock(
        StockDatasetRequest(source="fixture", symbols=symbols, start=start, end=end),
        bars,
        calendar_version="fixture-v1",
        source_version="fixture-v1",
        data_quality={},
    )
    feature_set = store.publish_feature_panel(
        features,
        parent_dataset_id=stock.dataset_id,
        factor_spec_hash="sha256:" + "a" * 64,
        instrument_ids=symbols,
        start_at=start,
        end_at=end,
        calendar_version="fixture-v1",
        price_basis="price_basis_unverified",
    )
    labels = [
        LabelRow(
            feature_decision_at=start + timedelta(hours=6, minutes=5),
            entry_at=start + timedelta(days=1),
            exit_at=start + timedelta(days=5),
            label_available_at=start + timedelta(days=5),
            label_value=0.01,
            label_quality=[],
            instrument_id=symbol,
            label_name="stock_forward_return",
            label_version="session-aligned-v2",
        )
        for symbol in symbols
    ]
    request = TrainAlphaRequest(
        feature_set_id=feature_set.dataset_id,
        label_source_dataset_id=stock.dataset_id,
        label_name="stock_forward_return",
        target_type="stock_return",
        evaluation_mode="validation_only",
        research_protocol_hash="sha256:" + "0" * 64,
        protocol_mode="synthetic_contract",
        split=ModelDevelopmentSplit(
            train_start=start,
            train_end=start + timedelta(days=30),
            validation_start=start + timedelta(days=30),
            validation_end=start + timedelta(days=60),
        ),
        hyperparameters={"num_leaves": 4, "min_child_samples": 1, "n_estimators": 2},
        random_seed=7,
        resource_budget=ModelResourceBudget(max_seconds=30, max_rows=1000),
        min_rank_ic=-1,
    )
    return store, request, labels


def test_validation_only_rejects_test_manifest_before_any_partition_read(tmp_path, monkeypatch):
    store, request, labels = _physical_test_store(tmp_path)
    reads = []

    def forbidden_partition_read(*args, **kwargs):
        reads.append((args, kwargs))
        raise AssertionError("reserved Test partition was materialized")

    monkeypatch.setattr("stock_quant.alpha.data.catalog.read_partition", forbidden_partition_read)
    with pytest.raises(AlphaError, match="physical coverage enters reserved Test"):
        train_alpha(
            request,
            store=store,
            registry=AlphaModelRegistry(tmp_path / "models"),
            label_rows=labels,
        )
    assert reads == []


def test_protocol_mode_separates_generic_synthetic_from_frozen_v1_3():
    frozen = _frozen_training_request()
    parsed = TrainAlphaModelJob.model_validate(frozen)
    assert parsed.protocol_mode == "frozen_v1_3"
    assert parsed.candidate_id == "H01"

    synthetic = copy.deepcopy(frozen)
    synthetic["protocol_mode"] = "synthetic_contract"
    synthetic["research_protocol_hash"] = "sha256:" + "0" * 64
    synthetic["candidate_id"] = None
    synthetic = TrainAlphaModelJob.model_validate(synthetic)
    assert protocol_claim(synthetic.protocol_mode)["v1_3_frozen"] is False
    assert protocol_claim("frozen_v1_3")["v1_3_frozen"] is True


@pytest.mark.parametrize(
    ("path", "replacement", "reason"),
    [
        (("research_protocol_hash",), "sha256:" + "0" * 64, "protocol hash"),
        (("split", "validation_start"), "2008-01-15T00:00:00Z", "window"),
        (("split", "embargo_days"), 0, "window"),
        (("candidate_id",), "H99", "candidate"),
        (("hyperparameters", "num_leaves"), 8, "candidate"),
        (("random_seed",), 20261121, "candidate"),
        (("resource_budget", "max_rows"), 1, "budget"),
        (("min_rank_ic",), 0.02, "gate"),
    ],
)
def test_frozen_mode_rejects_protocol_overrides(path, replacement, reason):
    raw = _replace(_frozen_training_request(), path, replacement)

    with pytest.raises(ValidationError, match=reason):
        TrainAlphaModelJob.model_validate(raw)
    with pytest.raises(ValidationError, match=reason):
        _training_request_model(raw)


def test_frozen_mode_rejects_wrong_signal_identity_and_direction():
    raw = _frozen_training_request()
    raw["baseline_signal"]["score_multiplier"] = -1

    with pytest.raises(ValidationError, match="signal"):
        TrainAlphaModelJob.model_validate(raw)


def _frozen_manifests(*, symbols=None, calendar_mapped=False):
    symbol_values = list(symbols or SOURCE_SYMBOLS)
    start = datetime(2003, 1, 1, tzinfo=UTC)
    end = datetime(2012, 1, 1, tzinfo=UTC)
    stock_quality = {"exchange_mappings": {}} if calendar_mapped else {}
    stock = SimpleNamespace(
        dataset_id="sha256:" + "2" * 64,
        dataset_kind="stock_bars",
        instrument_ids=symbol_values,
        start_at=start,
        end_at=end,
        calendar_version="fixture-v1",
        data_quality=stock_quality,
    )
    feature = SimpleNamespace(
        dataset_id=FAKE_ID,
        dataset_kind="feature_panel",
        instrument_ids=symbol_values,
        start_at=start,
        end_at=end,
        calendar_version="fixture-v1",
        source_version="sha256:" + "a" * 64,
        parent_dataset_ids=[stock.dataset_id],
    )
    return stock, feature


class _ManifestOnlyStore:
    def __init__(self, manifests):
        self.manifests = manifests
        self.read_calls = 0

    def load_manifest(self, dataset_id):
        return self.manifests[dataset_id]

    def read_rows(self, *args, **kwargs):
        self.read_calls += 1
        raise AssertionError("read_rows must not run before frozen manifest admission")


@pytest.mark.parametrize(
    ("symbols", "message"),
    [
        (SOURCE_SYMBOLS[:-1], "membership"),
        (SOURCE_SYMBOLS, "mapped calendar"),
    ],
)
def test_frozen_training_rejects_bad_manifests_before_any_rows_are_read(symbols, message, tmp_path):
    raw = _frozen_training_request()
    request = _training_request_model(raw)
    stock, feature = _frozen_manifests(symbols=symbols)
    store = _ManifestOnlyStore({feature.dataset_id: feature, stock.dataset_id: stock})
    labels = [
        LabelRow(
            feature_decision_at=datetime(2008, 2, 1, tzinfo=UTC),
            entry_at=datetime(2008, 2, 2, tzinfo=UTC),
            exit_at=datetime(2008, 2, 3, tzinfo=UTC),
            label_available_at=datetime(2008, 2, 3, tzinfo=UTC),
            label_value=0.01,
            label_quality=[],
            instrument_id=SOURCE_SYMBOLS[0],
            label_name="stock_forward_return",
            label_version="observed-record-v1",
        )
    ]

    with pytest.raises(AlphaError, match=message):
        train_alpha(
            request,
            store=store,
            registry=AlphaModelRegistry(tmp_path / "models"),
            label_rows=labels,
        )
    assert store.read_calls == 0


def test_frozen_label_contract_rejects_observed_record_fallback():
    legacy = LabelRow(
        feature_decision_at=datetime(2008, 2, 1, tzinfo=UTC),
        entry_at=datetime(2008, 2, 2, tzinfo=UTC),
        exit_at=datetime(2008, 2, 3, tzinfo=UTC),
        label_available_at=datetime(2008, 2, 3, tzinfo=UTC),
        label_value=0.01,
        label_quality=[],
        instrument_id="AAPL",
        label_name="stock_forward_return",
        label_version="observed-record-v1",
    )

    with pytest.raises(AlphaError, match="session-aligned-v2"):
        validate_frozen_label_contract([legacy])


def _gate_records(instruments, sessions, *, omitted_session=None):
    records = []
    for session_index, session in enumerate(sessions):
        if session == omitted_session:
            continue
        for index, instrument in enumerate(instruments):
            value = float(index) + session_index
            prediction = value
            if session_index == 1 and index == 0:
                prediction = float(1)
            elif session_index == 1 and index == 1:
                prediction = float(0)
            records.append({
                "decision_at": datetime.combine(session, datetime.min.time(), tzinfo=UTC),
                "instrument_id": instrument,
                "label_value": value,
                "prediction": prediction,
            })
    return records


def test_frozen_gate_uses_expected_instrument_session_denominator():
    instruments = [f"S{index:02d}" for index in range(20)]
    sessions = [date(2009, 6, 1), date(2011, 6, 1)]
    records = _gate_records(instruments, sessions, omitted_session=sessions[0])

    result = evaluate_frozen_validation_gate(
        records,
        expected_instruments=instruments,
        expected_sessions=sessions,
    )

    assert result["passed"] is False
    assert result["overall"]["expected_opportunities"] == 40
    assert result["overall"]["sample_count"] == 20
    assert result["overall"]["coverage"] == pytest.approx(0.5)
    assert any("coverage" in reason.lower() for reason in result["failure_reasons"])


def test_frozen_gate_requires_both_exact_validation_subperiods_and_thresholds():
    instruments = [f"S{index:02d}" for index in range(20)]
    sessions = [date(2009, 6, 1), date(2011, 6, 1)]
    complete = evaluate_frozen_validation_gate(
        _gate_records(instruments, sessions),
        expected_instruments=instruments,
        expected_sessions=sessions,
    )
    missing_second = evaluate_frozen_validation_gate(
        _gate_records(instruments, sessions, omitted_session=sessions[1]),
        expected_instruments=instruments,
        expected_sessions=sessions,
    )

    assert complete["passed"] is True
    assert [item["min_rank_ic"] for item in complete["subperiods"]] == [0.0, 0.0]
    assert missing_second["passed"] is False
    assert any("subperiod" in reason.lower() for reason in missing_second["failure_reasons"])


def _fold_fixture():
    start = datetime(2008, 1, 2, tzinfo=UTC)
    features = []
    labels = []
    for day in range(10):
        decision_at = start + timedelta(days=day, hours=1)
        for index in range(4):
            instrument = f"S{index}"
            features.append({
                "decision_at": decision_at,
                "instrument_id": instrument,
                "factor_name": "f1",
                "factor_value": float(index + day),
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
                label_value=float(index + day),
                label_quality=[],
                instrument_id=instrument,
                label_name="stock_forward_return",
                label_version="observed-record-v1",
            ))
    split = ModelDevelopmentSplit(
        train_start=start,
        train_end=start + timedelta(days=8),
        validation_start=start + timedelta(days=8),
        validation_end=start + timedelta(days=10),
    )
    return features, labels, split


def test_diagnostic_folds_reject_rows_over_budget_before_training(monkeypatch):
    features, labels, split = _fold_fixture()
    import stock_quant.alpha.models.window_diagnostics as window_diagnostics
    monkeypatch.setattr(window_diagnostics.lgb, "train", lambda *args, **kwargs: pytest.fail("training started"))

    with pytest.raises(AlphaError, match="row budget"):
        diagnostic_window_importance(
            features,
            labels,
            split=split,
            split_at=split.train_start + timedelta(days=4),
            hyperparameters={"num_leaves": 4, "min_child_samples": 1, "n_estimators": 4},
            resource_budget=ModelResourceBudget(max_seconds=30, max_rows=10),
            max_iterations=10,
        )


def test_diagnostic_folds_enforce_hard_iteration_cap_before_training(monkeypatch):
    features, labels, split = _fold_fixture()
    import stock_quant.alpha.models.window_diagnostics as window_diagnostics
    monkeypatch.setattr(window_diagnostics.lgb, "train", lambda *args, **kwargs: pytest.fail("training started"))

    with pytest.raises(AlphaError, match="iteration"):
        diagnostic_window_importance(
            features,
            labels,
            split=split,
            split_at=split.train_start + timedelta(days=4),
            hyperparameters={"num_leaves": 4, "min_child_samples": 1, "n_estimators": 11},
            resource_budget=ModelResourceBudget(max_seconds=30, max_rows=1000),
            max_iterations=10,
        )


def test_diagnostic_folds_enforce_deadline_inside_lightgbm(monkeypatch):
    features, labels, split = _fold_fixture()
    import stock_quant.alpha.models.window_diagnostics as window_diagnostics

    def fake_train(params, dataset, *, num_boost_round, callbacks):
        for callback in callbacks:
            callback(SimpleNamespace(iteration=1))
        pytest.fail("deadline callback did not stop LightGBM")

    monkeypatch.setattr(window_diagnostics.lgb, "train", fake_train)
    times = iter([0.0, 2.0, 2.0, 2.0])
    monkeypatch.setattr(window_diagnostics.time, "monotonic", lambda: next(times))

    with pytest.raises(AlphaError, match="time budget"):
        diagnostic_window_importance(
            features,
            labels,
            split=split,
            split_at=split.train_start + timedelta(days=4),
            hyperparameters={"num_leaves": 4, "min_child_samples": 1, "n_estimators": 4},
            resource_budget=ModelResourceBudget(max_seconds=1, max_rows=1000),
            max_iterations=10,
        )
