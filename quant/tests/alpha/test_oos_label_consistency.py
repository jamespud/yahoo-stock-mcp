from datetime import datetime, timedelta, timezone

import pytest

from stock_quant.alpha.contracts import StockDatasetRequest
from stock_quant.alpha.data.catalog import DatasetStore
from stock_quant.alpha.data.exchange_mapping import build_exchange_mapping
from stock_quant.alpha.data.stock_dataset import StockHistoryBatch, prepare_stock_dataset
from stock_quant.alpha.errors import AlphaError, AlphaErrorCode
from stock_quant.alpha.factors.ast import FactorSpec
from stock_quant.alpha.factors.compiler import compile_factor_spec
from stock_quant.alpha.factors.engine import compute_stock_factors
from stock_quant.alpha.models.labels import stock_forward_labels
from stock_quant.alpha.models.lightgbm_trainer import AlphaModelRegistry, TrainAlphaRequest, train_alpha
from stock_quant.alpha.models.oos_evaluation import evaluate_alpha_oos
from stock_quant.alpha.models.research_freeze import FreezeRequest, FrozenResearchStore, freeze_alpha_selection
from stock_quant.alpha.models.temporal_split import ModelDevelopmentSplit
from stock_quant.calendar import generate_calendar


def _fixture(tmp_path):
    store = DatasetStore(tmp_path / "alpha")
    calendar = generate_calendar(calendar_name="XNYS", exchange="XNYS", start_date="2024-01-01", end_date="2024-06-30")
    symbols = [f"S{index:02d}" for index in range(24)]
    skip = {("S00", "2024-02-15"), ("S00", "2024-05-15")}
    rows, dates_by_symbol = [], {symbol: [] for symbol in symbols}
    for symbol_index, symbol in enumerate(symbols):
        for session_index, session in enumerate(calendar.sessions):
            if session.date.isoformat() in {date for name, date in skip if name == symbol}:
                continue
            price = 100.0 + symbol_index + session_index * 0.02
            start = session.intervals[0].start
            end = session.intervals[-1].end
            rows.append({
                "instrument_id": symbol, "start_at": start, "end_at": end,
                "available_at": end + timedelta(minutes=5), "open": price,
                "high": price + 1, "low": price - 1, "close": price,
                "volume": 1000 + session_index, "adjustment": "raw",
                "source": "fixture", "source_version": "fixture-v1", "currency": "USD",
            })
            dates_by_symbol[symbol].append(session.date)
    request = StockDatasetRequest(
        source="fixture", symbols=symbols,
        start=datetime(2024, 1, 1, tzinfo=timezone.utc),
        end=datetime(2024, 7, 1, tzinfo=timezone.utc),
        price_basis="price_basis_unverified",
    )
    mappings = {
        symbol: build_exchange_mapping(
            symbol, "NYSE", start_date=request.start.date(), end_date=request.end.date()
        )
        for symbol in symbols
    }
    dataset = prepare_stock_dataset(request, store=store, provider=type("Provider", (), {
        "read": lambda self, _request: StockHistoryBatch(
            rows=rows, calendar_version=calendar.version, calendar_id=calendar.id,
            calendar_session_dates=[session.date for session in calendar.sessions],
            session_dates_by_symbol=dates_by_symbol, exchange_mappings=mappings,
            source_version="fixture-v1",
        )
    })())
    factor = compute_stock_factors(dataset, compile_factor_spec(FactorSpec.model_validate({
        "schema_version": "1.0", "timeframe": "1d",
        "nodes": [
            {"id": "ret1", "operator": "pct_change", "inputs": ["close"], "params": {"period": 1}},
            {"id": "ret5", "operator": "pct_change", "inputs": ["close"], "params": {"period": 5}},
            {"id": "vol5", "operator": "rolling_std", "inputs": ["ret1"], "params": {"period": 5}},
        ],
        "outputs": ["ret1", "ret5", "vol5"],
    })), store=store)
    labels = stock_forward_labels(store.read_rows(dataset.dataset_id), horizon=5, calendar=calendar)
    train_request = TrainAlphaRequest(
        feature_set_id=factor.dataset_id, label_source_dataset_id=dataset.dataset_id,
        label_name="stock_forward_return", horizon_days=5, target_type="stock_return",
        evaluation_mode="validation_only", research_protocol_hash="sha256:" + "0" * 64,
        split=ModelDevelopmentSplit(
            train_start=datetime(2024, 1, 2, tzinfo=timezone.utc),
            train_end=datetime(2024, 4, 1, tzinfo=timezone.utc),
            validation_start=datetime(2024, 4, 1, tzinfo=timezone.utc),
            validation_end=datetime(2024, 5, 1, tzinfo=timezone.utc),
            embargo_days=0,
        ),
        hyperparameters={"n_estimators": 12, "min_child_samples": 5},
        random_seed=7, min_rank_ic=-1,
    )
    registry = AlphaModelRegistry(tmp_path / "models")
    model = train_alpha(train_request, store=store, registry=registry, label_rows=labels)
    freeze_request = FreezeRequest(
        research_protocol_hash="sha256:" + "0" * 64,
        candidate_ledger_hash="sha256:" + "1" * 64,
        selected_model_ids=[model.model_id], test_feature_set_id=factor.dataset_id,
        test_label_source_dataset_id=dataset.dataset_id,
        test_start=datetime(2024, 5, 1, tzinfo=timezone.utc),
        test_end=datetime(2024, 6, 29, tzinfo=timezone.utc),
        selection_reason="fixture selection", label_name="stock_forward_return", horizon_days=5,
    )
    return store, registry, model, freeze_request, calendar


def test_training_and_oos_use_the_same_frozen_session_label_version(tmp_path):
    store, registry, model, freeze_request, _ = _fixture(tmp_path)
    model_metadata = registry.load(model.model_id).metadata
    assert model_metadata["label_spec"]["label_version"] == "session-aligned-v2"
    record = freeze_alpha_selection(
        freeze_request, store=store, model_registry=registry,
        freeze_store=FrozenResearchStore(tmp_path / "freezes"),
    )
    assert record.label_spec["label_version"] == "session-aligned-v2"
    result = evaluate_alpha_oos(record, store=store, model_registry=registry)
    assert result["summary"]["label_version"] == "session-aligned-v2"
    assert result["summary"]["exclusion_reasons"].get("INVALID_LABEL", 0) > 0


def test_freeze_rejects_requested_label_version_mismatch(tmp_path):
    store, registry, _, freeze_request, _ = _fixture(tmp_path)
    with pytest.raises(AlphaError) as error:
        freeze_alpha_selection(
            freeze_request.model_copy(update={"label_version": "observed-record-v1"}),
            store=store, model_registry=registry,
        )
    assert error.value.code == AlphaErrorCode.DATASET_MISMATCH
    assert any(item["field"] == "label_spec.label_version" for item in error.value.context["mismatches"])


def test_v2_oos_without_calendar_evidence_fails_closed(tmp_path):
    store, registry, model, freeze_request, _ = _fixture(tmp_path)
    record = freeze_alpha_selection(freeze_request, store=store, model_registry=registry)
    stripped = record.model_copy(update={
        "label_spec": {**record.label_spec, "label_version": "session-aligned-v2"},
    })
    manifest = store.load_manifest(record.test_dataset_ids["test_label_source_dataset_id"])
    manifest = manifest.model_copy(update={"data_quality": {**manifest.data_quality, "exchange_mappings": {}}})
    from stock_quant.alpha.models.oos_evaluation import _label_rows_for_oos
    with pytest.raises(AlphaError) as error:
        _label_rows_for_oos(
            stripped, store=store, label_manifest=manifest,
            start=datetime(2024, 5, 1, tzinfo=timezone.utc),
            end=datetime(2024, 6, 29, tzinfo=timezone.utc),
        )
    assert error.value.code == AlphaErrorCode.DATA_UNAVAILABLE


def test_v2_freeze_requires_verifiable_test_label_calendar(tmp_path, monkeypatch):
    store, registry, _, freeze_request, _ = _fixture(tmp_path)
    original = store.load_manifest
    def stripped(dataset_id):
        manifest = original(dataset_id)
        if dataset_id == freeze_request.test_label_source_dataset_id:
            manifest = manifest.model_copy(update={"data_quality": {**manifest.data_quality, "exchange_mappings": {}}})
        return manifest
    monkeypatch.setattr(store, "load_manifest", stripped)
    with pytest.raises(AlphaError) as error:
        freeze_alpha_selection(freeze_request, store=store, model_registry=registry)
    assert error.value.code == AlphaErrorCode.DATA_UNAVAILABLE
    assert error.value.context["label_version"] == "session-aligned-v2"
