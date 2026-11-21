from __future__ import annotations

import sqlite3
import time
import json
from datetime import datetime, timedelta, timezone

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


@pytest.mark.parametrize("winner", ["cancel", "reclaim"])
def test_losing_publication_is_not_visible(tmp_path, monkeypatch, winner):
    path = tmp_path / "registry.sqlite3"
    datasets = DatasetStore(path.with_suffix(".alpha"))
    stock = _stock(datasets)
    with JobStore(path) as worker, JobStore(path) as competing:
        worker.submit_alpha({"kind": "compute_factors", "dataset_id": stock.dataset_id,
                             "factor_spec": _spec().model_dump(mode="json")})
        claim = worker.claim()
        pending = []
        register = DatasetStore.register
        fresh = None

        def race(self, manifest, *args, **kwargs):
            nonlocal fresh
            pending.append(manifest.dataset_id)
            register(self, manifest, *args, **kwargs)
            with pytest.raises(KeyError):
                datasets.load_manifest(manifest.dataset_id)
            if winner == "cancel":
                competing.cancel(claim["id"])
            else:
                with competing.connection:
                    competing.connection.execute("UPDATE jobs SET lease_until=unixepoch()-1 WHERE id=?", (claim["id"],))
                fresh = competing.claim()
                assert fresh["owner"] != claim["owner"]

        monkeypatch.setattr(DatasetStore, "register", race)
        assert execute_job(worker, claim) is False
        assert len(pending) == 1
        with pytest.raises(KeyError):
            datasets.load_manifest(pending[0])
        # The losing attempt does not delete shared content-addressed bytes.
        staged = datasets.load_published_manifest(pending[0])
        register(datasets, staged)
        with pytest.raises(KeyError):
            datasets.load_manifest(pending[0])
        if fresh is not None:
            monkeypatch.setattr(DatasetStore, "register", register)
            assert execute_job(competing, fresh)
            assert datasets.load_manifest(pending[0]).dataset_id == pending[0]


def test_same_catalog_can_accept_receipt_from_second_registry(tmp_path, monkeypatch):
    path = tmp_path / "registry.sqlite3"
    datasets = DatasetStore(path.with_suffix(".alpha"))
    stock = _stock(datasets)
    request = {"kind": "compute_factors", "dataset_id": stock.dataset_id,
               "factor_spec": _spec().model_dump(mode="json")}
    register = DatasetStore.register
    pending = []
    with JobStore(path) as first, JobStore(path.with_suffix(".db")) as second:
        first.submit_alpha(request)
        stale = first.claim()

        def abandon(self, manifest, *args, **kwargs):
            register(self, manifest, *args, **kwargs)
            pending.append(manifest.dataset_id)
            first.cancel(stale["id"])

        with monkeypatch.context() as patch:
            patch.setattr(DatasetStore, "register", abandon)
            assert execute_job(first, stale) is False
        with pytest.raises(KeyError):
            datasets.load_manifest(pending[0])
        second.submit_alpha(request)
        assert execute_job(second, second.claim()) is True
        assert datasets.load_manifest(pending[0]).dataset_id == pending[0]


def test_losing_republication_preserves_legacy_catalog_entry(tmp_path, monkeypatch):
    path = tmp_path / "registry.sqlite3"
    datasets = DatasetStore(path.with_suffix(".alpha"))
    stock = _stock(datasets)
    existing = compute_stock_factors(stock, compile_factor_spec(_spec()), store=datasets)
    original = DatasetStore.register
    with JobStore(path) as worker, JobStore(path) as competing:
        worker.submit_alpha({"kind": "compute_factors", "dataset_id": stock.dataset_id,
                             "factor_spec": _spec().model_dump(mode="json")})
        claim = worker.claim()

        def cancel(self, manifest, *args, **kwargs):
            assert manifest.dataset_id == existing.dataset_id
            original(self, manifest, *args, **kwargs)
            competing.cancel(claim["id"])

        monkeypatch.setattr(DatasetStore, "register", cancel)
        assert execute_job(worker, claim) is False
        assert worker.get_job(claim["id"])["status"] == "cancelled"
        assert DatasetStore(datasets.root).load_manifest(existing.dataset_id) == existing


def test_audit_rejects_manifest_tampering(tmp_path):
    datasets = DatasetStore(tmp_path)
    stock = _stock(datasets)
    path = datasets.datasets / stock.dataset_id.removeprefix("sha256:") / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["partition_manifest"][0]["row_count"] += 1
    path.write_text(json.dumps(manifest))
    assert not datasets.validate_dataset(stock.dataset_id).valid
    from stock_quant.alpha.errors import AlphaError
    with pytest.raises(AlphaError, match="Manifest identity mismatch"):
        datasets.audit_dataset(stock.dataset_id, "partition_manifest")


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
