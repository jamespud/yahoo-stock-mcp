from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import numpy as np
import pytest
from pydantic import ValidationError

from stock_quant.alpha.contracts import StockDatasetRequest
from stock_quant.alpha.data.catalog import DatasetStore
from stock_quant.alpha.data.exchange_mapping import build_exchange_mapping
from stock_quant.calendar import generate_long_calendar
from stock_quant.jobs import JobStore, execute_job

UTC = timezone.utc
START = date(2009, 1, 2)
TRAIN_END = date(2009, 2, 2)
EVALUATION_START = date(2009, 2, 2)
EVALUATION_END = date(2009, 3, 2)
VALIDATION_END = date(2009, 4, 1)
TEST_START = datetime(2012, 1, 1, tzinfo=UTC)
SYMBOLS = ["AAPL", "MSFT", "NVDA", "JPM"]


def _at(value: date, hour: int = 0) -> datetime:
    return datetime.combine(value, time(hour), tzinfo=UTC)


def _fixture(tmp_path):
    calendar = generate_long_calendar(
        calendar_name="XNYS", exchange="XNYS",
        start_date=EVALUATION_START, end_date=VALIDATION_END,
    )
    mappings = {
        symbol: build_exchange_mapping(
            symbol, "XNYS", start_date=EVALUATION_START, end_date=VALIDATION_END,
        )
        for symbol in SYMBOLS
    }
    quality = {
        "calendar_id": calendar.id,
        "exchange_mappings": mappings,
        "calendar_mapping_status_by_symbol": {
            symbol: mapping["calendar_mapping_status"] for symbol, mapping in mappings.items()
        },
    }
    store = DatasetStore(tmp_path / "registry.alpha")
    bars = []
    for session in calendar.sessions:
        if not EVALUATION_START <= session.date < VALIDATION_END:
            continue
        available_at = session.intervals[-1].end + timedelta(minutes=5)
        for offset, symbol in enumerate(SYMBOLS):
            base = 50.0 + offset * 5 + (session.date - EVALUATION_START).days * 0.07
            bars.append({
                "instrument_id": symbol,
                "start_at": session.intervals[0].start,
                "end_at": session.intervals[-1].end,
                "available_at": available_at,
                "open": base,
                "high": base + 1,
                "low": base - 1,
                "close": base + 0.2,
                "volume": 1000.0,
                "adjustment": "raw",
                "source": "fixture",
                "source_version": "fixture-v1",
                "currency": "USD",
            })
    stock = store.publish_stock(
        StockDatasetRequest(
            source="fixture", symbols=SYMBOLS,
            start=_at(EVALUATION_START), end=_at(VALIDATION_END),
        ),
        bars,
        calendar_version=calendar.version,
        source_version="fixture-v1",
        data_quality=quality,
    )

    rng = np.random.default_rng(20261121)
    features = []
    sessions = [
        session for session in calendar.sessions
        if EVALUATION_START <= session.date < EVALUATION_END
    ]
    for session in sessions:
        available_at = session.intervals[-1].end + timedelta(minutes=5)
        for symbol in SYMBOLS:
            features.append({
                "decision_at": available_at,
                "instrument_id": symbol,
                "factor_name": "mom63",
                "factor_value": float(rng.normal()),
                "feature_available_at": available_at,
                "eligible": True,
                "quality_flags": [],
                "factor_version": "fixture-v1",
            })
    feature_set = store.publish_feature_panel(
        features,
        parent_dataset_id=stock.dataset_id,
        factor_spec_hash="sha256:" + "a" * 64,
        instrument_ids=SYMBOLS,
        start_at=_at(EVALUATION_START),
        end_at=_at(EVALUATION_END),
        calendar_version=calendar.version,
        price_basis=stock.price_basis,
    )
    return store, stock, feature_set, calendar, sessions


def _request(stock, feature_set) -> dict:
    return {
        "kind": "evaluate_alpha_signal",
        "feature_set_id": feature_set.dataset_id,
        "stock_dataset_id": stock.dataset_id,
        "label_name": "stock_forward_return",
        "target_type": "stock_return",
        "horizon_days": 5,
        "split": {
            "train_start": _at(START).isoformat(),
            "train_end": _at(TRAIN_END).isoformat(),
            "validation_start": _at(TRAIN_END).isoformat(),
            "validation_end": _at(VALIDATION_END).isoformat(),
        },
        "evaluation_window": {
            "start": _at(EVALUATION_START).isoformat(),
            "end": _at(EVALUATION_END).isoformat(),
        },
        "signal_spec": {
            "factor_name": "mom63",
            "score_multiplier": 1,
            "version": "direct-factor-v1",
        },
    }


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda value: value.update(signal_spec={**value["signal_spec"], "score_multiplier": 0}), "Input should be 1 or -1"),
        (lambda value: value.pop("stock_dataset_id"), "Field required"),
        (lambda value: value.update(stock_dataset_id="sha256:" + "f" * 64), "missing stock dataset"),
        (lambda value: value["evaluation_window"].update(start="2008-12-31T00:00:00Z"), "evaluation window must stay within development split"),
        (lambda value: value["split"].update(validation_end="2012-01-02T00:00:00Z"), "must not enter reserved Test"),
    ],
)
def test_signal_job_rejects_invalid_admission_before_enqueue(tmp_path, mutation, reason):
    store, stock, feature_set, _, _ = _fixture(tmp_path)
    raw = _request(stock, feature_set)
    mutation(raw)
    registry_path = tmp_path / "registry.sqlite3"
    with JobStore(registry_path) as jobs:
        with pytest.raises((ValidationError, ValueError)) as raised:
            jobs.submit_alpha(raw)
        assert reason.lower() in str(raised.value).lower()
        assert jobs.connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
    assert store.load_manifest(stock.dataset_id)


def test_signal_job_reads_only_development_windows_and_returns_paginated_diagnostics(tmp_path, monkeypatch):
    store, stock, feature_set, calendar, sessions = _fixture(tmp_path)
    registry_path = tmp_path / "registry.sqlite3"
    observed_reads = []
    original_read_rows = DatasetStore.read_rows

    def read_rows(self, dataset_id, columns=None, start_at=None, end_at=None):
        observed_reads.append((dataset_id, start_at, end_at))
        return original_read_rows(self, dataset_id, columns, start_at, end_at)

    monkeypatch.setattr(DatasetStore, "read_rows", read_rows)
    with JobStore(registry_path) as jobs:
        submitted = jobs.submit_alpha(_request(stock, feature_set))
        assert execute_job(jobs, jobs.claim()) is True
        job = jobs.get_job(submitted["job_id"])
        assert job["status"] == "completed"

        summary = jobs.get_result(submitted["job_id"], "summary")
        assert {
            "signal_spec_hash", "feature_set_id", "label_version", "evaluation_window",
            "rank_ic_by_decision", "rank_ic_mean", "icir", "coverage", "excluded_opportunities",
        } <= set(summary)
        assert summary["feature_set_id"] == feature_set.dataset_id
        assert summary["stock_dataset_id"] == stock.dataset_id
        assert summary["label_version"] == "session-aligned-v2"
        assert summary["evaluation_window"] == {
            "start": _at(EVALUATION_START).isoformat(),
            "end": _at(EVALUATION_END).isoformat(),
        }
        assert summary["decision_count"] == len(sessions)
        assert summary["coverage"]["conservation_holds"] is True
        assert summary["excluded_opportunities"] == summary["coverage"]["excluded_opportunities"]
        assert all(row["rank_ic"] is not None for row in summary["rank_ic_by_decision"])

        page = jobs.get_result(submitted["job_id"], "rank_ic_by_decision", limit=2)
        assert page["total_count"] == len(sessions)
        assert len(page["rows"]) == 2
        assert page["next_cursor"]
        tail = jobs.get_result(
            submitted["job_id"], "rank_ic_by_decision", limit=1000, cursor=page["next_cursor"],
        )
        assert [*page["rows"], *tail["rows"]] == summary["rank_ic_by_decision"]
        assert tail["next_cursor"] is None

        reproducibility = jobs.get_result(submitted["job_id"], "reproducibility")
        assert reproducibility["signal_spec_hash"] == summary["signal_spec_hash"]
        assert reproducibility["source_windows"]["feature_end"] == _at(EVALUATION_END).isoformat()
        assert reproducibility["source_windows"]["stock_end"] == _at(VALIDATION_END).isoformat()
        assert reproducibility["label_version"] == "session-aligned-v2"
        assert reproducibility["reserved_test_start"] == TEST_START.isoformat()

    feature_reads = [item for item in observed_reads if item[0] == feature_set.dataset_id]
    stock_reads = [item for item in observed_reads if item[0] == stock.dataset_id]
    assert feature_reads
    assert stock_reads
    assert {item[0] for item in observed_reads} == {feature_set.dataset_id, stock.dataset_id}
    assert all(start_at >= _at(EVALUATION_START) for _, start_at, _ in observed_reads)
    assert all(end_at <= _at(VALIDATION_END) for _, _, end_at in observed_reads)
    assert all(end_at == _at(EVALUATION_END) for _, _, end_at in feature_reads)
    assert all(end_at == _at(VALIDATION_END) for _, _, end_at in stock_reads)
    assert all(calendar.start_date <= session.date <= calendar.end_date for session in calendar.sessions)
