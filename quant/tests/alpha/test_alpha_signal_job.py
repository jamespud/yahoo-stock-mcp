from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone

import numpy as np
import pytest
from pydantic import ValidationError

from stock_quant.alpha.contracts import StockDatasetRequest
from stock_quant.alpha.data.catalog import DatasetStore
from stock_quant.alpha.data.exchange_mapping import build_exchange_mapping
from stock_quant.alpha.errors import AlphaErrorCode
from stock_quant.calendar import generate_long_calendar
from stock_quant.jobs import JobStore, execute_job

UTC = timezone.utc
START = date(2009, 1, 2)
TRAIN_END = date(2009, 2, 2)
EVALUATION_START = date(2009, 2, 2)
EVALUATION_END = date(2009, 3, 2)
VALIDATION_END = date(2009, 4, 1)
TEST_START = datetime(2012, 1, 1, tzinfo=UTC)
TEST_END = date(2012, 1, 3)
SYMBOLS = ["AAPL", "MSFT", "NVDA", "JPM"]


def _at(value: date, hour: int = 0) -> datetime:
    return datetime.combine(value, time(hour), tzinfo=UTC)


def _fixture(tmp_path, *, stock_end=VALIDATION_END, feature_end=EVALUATION_END):
    calendar = generate_long_calendar(
        calendar_name="XNYS", exchange="XNYS",
        start_date=EVALUATION_START, end_date=max(VALIDATION_END, stock_end, feature_end),
    )
    mappings = {
        symbol: build_exchange_mapping(
            symbol, "XNYS", start_date=EVALUATION_START, end_date=max(VALIDATION_END, stock_end, feature_end),
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
    if stock_end > VALIDATION_END:
        sentinel_at = datetime(2012, 1, 3, 21, tzinfo=UTC)
        bars.append({
            "instrument_id": "AAPL",
            "start_at": sentinel_at.replace(hour=14, minute=30),
            "end_at": sentinel_at,
            "available_at": sentinel_at + timedelta(minutes=5),
            "open": 90.0, "high": 91.0, "low": 89.0, "close": 90.5,
            "volume": 1000.0, "adjustment": "raw", "source": "fixture",
            "source_version": "fixture-v1", "currency": "USD",
        })
    stock = store.publish_stock(
        StockDatasetRequest(
            source="fixture", symbols=SYMBOLS,
            start=_at(EVALUATION_START), end=_at(stock_end),
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
    if feature_end > EVALUATION_END:
        sentinel_at = datetime(2012, 1, 3, 21, 5, tzinfo=UTC)
        features.append({
            "decision_at": sentinel_at,
            "instrument_id": "AAPL",
            "factor_name": "mom63",
            "factor_value": 999.0,
            "feature_available_at": sentinel_at,
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
        end_at=_at(feature_end),
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
        (lambda value: value.update(label_name="future_return"), "stock_forward_return"),
        (lambda value: value.update(stock_dataset_id="sha256:" + "f" * 64), "missing stock dataset"),
        (lambda value: value["evaluation_window"].update(start="2008-12-31T00:00:00Z"), "evaluation window must stay within development split"),
        (lambda value: value.update(evaluation_window={"start": "2009-02-02T00:00:00Z", "end": "2009-02-02T23:59:59Z"}), "UTC midnight"),
        (lambda value: value.update(evaluation_window={"start": "2009-02-02T12:00:00Z", "end": "2009-02-03T00:00:00Z"}), "UTC midnight"),
        (lambda value: value["split"].update(validation_end="2012-01-02T00:00:00Z"), "must not enter reserved Test"),
        (lambda value: value.update(top_n=20), "Extra inputs are not permitted"),
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


@pytest.mark.parametrize("dataset_kind", ["stock", "feature"])
def test_signal_job_rejects_physical_test_coverage_before_any_partition_read(tmp_path, monkeypatch, dataset_kind):
    store, stock, feature_set, _, _ = _fixture(
        tmp_path,
        stock_end=TEST_END if dataset_kind == "stock" else VALIDATION_END,
        feature_end=TEST_END if dataset_kind == "feature" else EVALUATION_END,
    )
    read_calls = []

    def forbidden_read_rows(*args, **kwargs):
        read_calls.append((args, kwargs))
        raise AssertionError("read_rows must not run for physically Test-extending manifests")

    monkeypatch.setattr(DatasetStore, "read_rows", forbidden_read_rows)
    registry_path = tmp_path / "registry.sqlite3"
    with JobStore(registry_path) as jobs:
        with pytest.raises(ValueError, match="physical coverage enters reserved Test"):
            jobs.submit_alpha(_request(stock, feature_set))
        assert jobs.connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
    assert read_calls == []


def test_signal_executor_defensively_rejects_physical_test_coverage_before_read(tmp_path, monkeypatch):
    store, stock, feature_set, _, _ = _fixture(tmp_path, stock_end=TEST_END)
    read_calls = []

    def forbidden_read_rows(*args, **kwargs):
        read_calls.append((args, kwargs))
        raise AssertionError("read_rows must not run for physically Test-extending manifests")

    monkeypatch.setattr(DatasetStore, "read_rows", forbidden_read_rows)
    monkeypatch.setattr(JobStore, "_validate_signal_datasets", lambda self, request: None)
    registry_path = tmp_path / "registry.sqlite3"
    with JobStore(registry_path) as jobs:
        submitted = jobs.submit_alpha(_request(stock, feature_set))
        assert execute_job(jobs, jobs.claim()) is True
        job = jobs.get_job(submitted["job_id"])
        assert job["status"] == "failed"
        assert "physical coverage enters reserved Test" in job["error_message"]
    assert read_calls == []


def test_signal_job_rejects_empty_session_aligned_label_set(tmp_path, monkeypatch):
    store, stock, feature_set, _, _ = _fixture(tmp_path)
    from stock_quant.alpha.jobs import executor as alpha_executor

    monkeypatch.setattr(alpha_executor, "stock_forward_labels", lambda *args, **kwargs: [])
    registry_path = tmp_path / "registry.sqlite3"
    with JobStore(registry_path) as jobs:
        submitted = jobs.submit_alpha(_request(stock, feature_set))
        assert execute_job(jobs, jobs.claim()) is True
        job = jobs.get_job(submitted["job_id"])
        assert job["status"] == "failed"
        assert job["error_code"] == AlphaErrorCode.DATA_UNAVAILABLE.value
        assert "No session-aligned-v2 labels generated" in job["error_message"]


def test_signal_job_normalizes_midnight_offsets_to_utc(tmp_path):
    store, stock, feature_set, _, _ = _fixture(tmp_path)
    raw = _request(stock, feature_set)
    raw["evaluation_window"] = {
        "start": "2009-02-02T08:00:00+08:00",
        "end": "2009-03-02T08:00:00+08:00",
    }
    registry_path = tmp_path / "registry.sqlite3"
    with JobStore(registry_path) as jobs:
        submitted = jobs.submit_alpha(raw)
        stored = jobs.connection.execute(
            "SELECT request FROM jobs WHERE id=?", (submitted["job_id"],)
        ).fetchone()[0]
    window = json.loads(stored)["evaluation_window"]
    assert window == {"start": "2009-02-02T00:00:00Z", "end": "2009-03-02T00:00:00Z"}


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
            "daily_decision_count", "daily_sample_count", "daily_prediction_coverage", "daily_label_coverage",
            "weekly_rank_ic_mean", "weekly_valid_decision_count", "weekly_sample_count",
            "weekly_prediction_coverage", "weekly_label_coverage", "weekly_coverage",
            "top20_forward_label_summary",
        } <= set(summary)
        assert summary["feature_set_id"] == feature_set.dataset_id
        assert summary["stock_dataset_id"] == stock.dataset_id
        assert summary["label_version"] == "session-aligned-v2"
        assert summary["evaluation_window"] == {
            "start": _at(EVALUATION_START).isoformat(),
            "end": _at(EVALUATION_END).isoformat(),
        }
        assert summary["decision_count"] == len(sessions)
        assert summary["daily_decision_count"] == len(sessions)
        assert summary["daily_sample_count"] == summary["coverage"]["valid_matched_opportunities"]
        assert summary["coverage"]["conservation_holds"] is True
        assert summary["excluded_opportunities"] == summary["coverage"]["excluded_opportunities"]
        assert all(row["rank_ic"] is not None for row in summary["rank_ic_by_decision"])
        assert summary["weekly_decision_count"] < summary["daily_decision_count"]
        assert summary["weekly_coverage"]["expected_date_count"] == summary["weekly_decision_count"]
        assert summary["weekly_coverage"] is not summary["coverage"]
        assert summary["top20_forward_label_summary"]["analysis_label"] == (
            "predictive_label_analysis_not_executable_backtest"
        )

        page = jobs.get_result(submitted["job_id"], "rank_ic_by_decision", limit=2)
        assert page["total_count"] == len(sessions)
        assert len(page["rows"]) == 2
        assert page["next_cursor"]
        tail = jobs.get_result(
            submitted["job_id"], "rank_ic_by_decision", limit=1000, cursor=page["next_cursor"],
        )
        assert [*page["rows"], *tail["rows"]] == summary["rank_ic_by_decision"]
        assert tail["next_cursor"] is None

        weekly_page = jobs.get_result(submitted["job_id"], "weekly_rank_ic", limit=1)
        assert weekly_page["total_count"] == summary["weekly_decision_count"]
        assert len(weekly_page["rows"]) == 1
        assert weekly_page["next_cursor"]
        weekly_tail = jobs.get_result(
            submitted["job_id"], "weekly_rank_ic", limit=1000, cursor=weekly_page["next_cursor"],
        )
        assert len([*weekly_page["rows"], *weekly_tail["rows"]]) == summary["weekly_decision_count"]
        assert weekly_tail["next_cursor"] is None

        top20_page = jobs.get_result(submitted["job_id"], "top20_forward_labels", limit=1)
        assert top20_page["total_count"] == summary["top20_forward_label_summary"]["decision_count"]
        assert len(top20_page["rows"]) == 1
        assert top20_page["rows"][0]["analysis_label"] == "predictive_label_analysis_not_executable_backtest"
        assert "INSUFFICIENT_TOP20_NAMES" in top20_page["rows"][0]["exclusion_reasons"]

        reproducibility = jobs.get_result(submitted["job_id"], "reproducibility")
        assert reproducibility["signal_spec_hash"] == summary["signal_spec_hash"]
        assert reproducibility["source_windows"]["feature_end"] == _at(EVALUATION_END).isoformat()
        assert reproducibility["source_windows"]["stock_end"] == _at(VALIDATION_END).isoformat()
        assert reproducibility["label_version"] == "session-aligned-v2"
        assert reproducibility["coverage"] == summary["coverage"]
        assert reproducibility["excluded_opportunities"] == summary["excluded_opportunities"]
        assert reproducibility["weekly_decision_dates"] == summary["weekly_decision_dates"]
        assert reproducibility["top20_forward_label_summary"] == summary["top20_forward_label_summary"]
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
