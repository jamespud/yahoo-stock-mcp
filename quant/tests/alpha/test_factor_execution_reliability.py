from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from stock_quant.alpha.data.catalog import DatasetStore
from stock_quant.alpha.factors.ast import FactorSpec
from stock_quant.alpha.factors.compiler import compile_factor_spec
from stock_quant.alpha.factors.engine import compute_stock_factors
from stock_quant.jobs import JobStore, OwnershipLost, execute_job


def catalog_count(store: DatasetStore, kind="feature_panel") -> int:
    path = store.catalog_path
    with sqlite3.connect(path) as connection:
        return connection.execute("SELECT count(*) FROM alpha_datasets WHERE dataset_kind=?", (kind,)).fetchone()[0]


def _bar_rows(days=8):
    rows = []
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    for symbol in ("AAPL", "MSFT"):
        for day in range(days):
            decision = start + timedelta(days=day)
            close = 100.0 + day
            rows.append({
                "instrument_id": symbol, "start_at": decision,
                "end_at": decision + timedelta(hours=6),
                "available_at": decision + timedelta(hours=6, minutes=1),
                "open": close - 0.5, "high": close + 1, "low": close - 1,
                "close": close, "volume": 1000 + day, "adjustment": "raw",
                "source": "fixture", "source_version": "v1", "currency": "USD",
            })
    return rows


def _stock(store: DatasetStore):
    from stock_quant.alpha.contracts import StockDatasetRequest
    rows = _bar_rows()
    return store.publish_stock(StockDatasetRequest(source="fixture", symbols=["AAPL", "MSFT"], start="2024-01-01T00:00:00Z", end="2024-01-09T00:00:00Z"), rows, calendar_version="fixture", source_version="v1", data_quality={})


def _spec(period=2):
    return FactorSpec.model_validate({
        "schema_version": "1.0", "timeframe": "1d",
        "nodes": [
            {"id": "ret", "operator": "pct_change", "inputs": ["close"], "params": {"period": 1}},
            {"id": "lag", "operator": "lag", "inputs": ["close"], "params": {"period": period}},
        ], "outputs": ["ret", "lag"],
    })


def test_stage_telemetry_records_materialization_before_parquet(tmp_path):
    store = DatasetStore(tmp_path / "registry.alpha")
    dataset = _stock(store)
    compiled = compile_factor_spec(_spec())
    stages = []
    started = time.monotonic()

    def observe(progress):
        stages.append({"progress": progress, "elapsed_ms": int((time.monotonic() - started) * 1000)})
        time.sleep(0.01)

    manifest = compute_stock_factors(dataset, compiled, store=store, checkpoint=observe)
    assert manifest.dataset_kind == "feature_panel"
    materialization = [item for item in stages if item["progress"] == 0.75]
    parquet = [item for item in stages if item["progress"] > 0.75]
    assert materialization
    assert parquet
    assert min(item["progress"] for item in parquet) > materialization[-1]["progress"]


def test_cancelled_worker_cannot_publish_feature_set(tmp_path, monkeypatch):
    store = DatasetStore(tmp_path / "registry.alpha")
    dataset = _stock(store)
    with JobStore(tmp_path / "registry.sqlite3") as jobs:
        run = jobs.submit_alpha({"kind": "compute_factors", "dataset_id": dataset.dataset_id,
                                 "factor_spec": {"schema_version": "1.0", "timeframe": "1d",
                                                 "nodes": [{"id": "ret", "operator": "pct_change", "inputs": ["close"], "params": {"period": 1}}],
                                                 "outputs": ["ret"]}})
        claim = jobs.claim()
        original = DatasetStore.publish_feature_panel
        def cancel_after_manifest(self, rows, **kwargs):
            manifest = original(self, rows, **kwargs)
            jobs.cancel(claim["id"])
            return manifest
        monkeypatch.setattr(DatasetStore, "publish_feature_panel", cancel_after_manifest)
        assert execute_job(jobs, claim) is False
        assert jobs.get_job(run["job_id"])["status"] == "cancelled"
        row = catalog_count(store)
        assert row == 0


def test_expired_owner_cannot_commit_after_reclaim(tmp_path):
    store = DatasetStore(tmp_path / "registry.alpha")
    dataset = _stock(store)
    with JobStore(tmp_path / "registry.sqlite3") as jobs:
        run = jobs.submit_alpha({"kind": "compute_factors", "dataset_id": dataset.dataset_id,
                                 "factor_spec": {"schema_version": "1.0", "timeframe": "1d",
                                                 "nodes": [{"id": "ret", "operator": "pct_change", "inputs": ["close"], "params": {"period": 1}}],
                                                 "outputs": ["ret"]}})
        stale = jobs.claim()
        jobs.connection.execute("UPDATE jobs SET lease_until=unixepoch()-1 WHERE id=?", (run["job_id"],))
        jobs.connection.commit()
        fresh = jobs.claim()
        assert fresh and fresh["id"] == run["job_id"] and fresh["owner"] != stale["owner"]
        assert jobs.begin_publish(stale, {"summary": {"stale": True}}) is False
        assert catalog_count(store) == 0
        assert execute_job(jobs, fresh) is True
        job = jobs.get_job(run["job_id"])
        assert job["status"] == "completed" and job["attempts"] == 2
        assert catalog_count(store) == 1


def test_worker_crash_recovers_without_partial_completed_result(tmp_path):
    store = DatasetStore(tmp_path / "registry.alpha")
    dataset = _stock(store)
    with JobStore(tmp_path / "registry.sqlite3") as jobs:
        run = jobs.submit_alpha({"kind": "compute_factors", "dataset_id": dataset.dataset_id,
                                 "factor_spec": {"schema_version": "1.0", "timeframe": "1d",
                                                 "nodes": [{"id": "ret", "operator": "pct_change", "inputs": ["close"], "params": {"period": 1}}],
                                                 "outputs": ["ret"]}})
        crashed = jobs.claim()
        jobs.connection.execute("UPDATE jobs SET lease_until=unixepoch()-1 WHERE id=?", (run["job_id"],))
        jobs.connection.commit()
        fresh = jobs.claim()
        assert fresh
        assert execute_job(jobs, fresh) is True
        job = jobs.get_job(run["job_id"])
        assert job["status"] == "completed" and job["attempts"] == 2
        assert catalog_count(store) == 1
        try:
            jobs.heartbeat(crashed, 1.0)
        except OwnershipLost:
            pass


def test_root_cause_report_records_h01_long_materialization():
    path = Path(__file__).resolve().parents[3] / "reports/alpha-reliability/factor-execution-diagnosis.json"
    report = json.loads(path.read_text())
    assert report["schema_version"] == "1.0"
    assert report["root_cause"]["code"] == "LONG_FEATURE_ROW_MATERIALIZATION"
    assert report["root_cause"]["last_heartbeat_gap_ms"] >= 15000
    assert report["root_cause"]["classification"] == "CPU_BLOCKED_BETWEEN_LEASE_CHECKPOINTS"
    assert report["dataset_hash"] == "sha256:b4d8310454a2339ec36a9e5e2801ea0836e199440104f3e749d9740e04cf937c"
    assert report["evidence"]["observed_bar_count"] == 172997
    assert report["evidence"]["last_stage"] == "materialize_features"
    assert report["evidence"]["prior_stage"] == "compute_factor_nodes"
    assert report["remediation"] == "BATCH_VECTORIZE_FEATURE_ROWS_AND_KEEP_FENCED_LEASE"
