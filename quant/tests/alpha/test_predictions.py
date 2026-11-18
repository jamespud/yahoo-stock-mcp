from datetime import datetime, timedelta, timezone

from stock_quant.alpha.contracts import StockDatasetRequest
from stock_quant.alpha.data.catalog import DatasetStore
from stock_quant.jobs import JobStore, execute_job


def _stock(store):
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    rows = []
    for day in range(10):
        at = start + timedelta(days=day)
        rows.append({"instrument_id": "AAPL", "start_at": at, "end_at": at.replace(hour=21),
                     "available_at": at.replace(hour=21, minute=1), "open": 100 + day, "high": 102 + day,
                     "low": 99 + day, "close": 101 + day, "volume": 1000, "adjustment": "raw",
                     "source": "fixture", "source_version": "v1", "currency": "USD"})
    return store.publish_stock(StockDatasetRequest(source="fixture", symbols=["AAPL"], start=start,
                                                   end=start + timedelta(days=11)), rows,
                               calendar_version="fixture", source_version="v1", data_quality={})


def test_alpha_job_submission_idempotency_and_result(tmp_path):
    registry_path = tmp_path / "registry.sqlite3"
    with JobStore(registry_path) as jobs:
        store = DatasetStore(registry_path.with_suffix(".alpha"))
        dataset = _stock(store)
        raw = {
            "kind": "compute_factors", "dataset_id": dataset.dataset_id,
            "factor_spec": {"schema_version": "1.0", "timeframe": "1d",
                            "nodes": [{"id": "ret", "operator": "pct_change", "inputs": ["close"], "params": {"period": 1}}],
                            "outputs": ["ret"]},
            "idempotency_key": "factor-run-1",
        }
        submission = jobs.submit_alpha(raw)
        repeated = jobs.submit_alpha(raw)
        assert submission["job_id"] == repeated["job_id"]
        assert submission["job_type"] == "alpha_compute_factors"
        assert submission["status"] == "queued"
        claim = jobs.claim()
        assert execute_job(jobs, claim) is True
        job = jobs.get_job(submission["job_id"])
        assert job["status"] == "completed"
        dataset_result = jobs.get_result(submission["job_id"], "dataset")
        assert dataset_result["section"] == "dataset"
        factors = jobs.get_result(submission["job_id"], "factors")
        assert factors["total_count"] == 1


def test_alpha_cancel_fences_worker_and_failure_code_is_stable(tmp_path):
    registry_path = tmp_path / "registry.sqlite3"
    with JobStore(registry_path) as jobs:
        store = DatasetStore(registry_path.with_suffix(".alpha"))
        dataset = _stock(store)
        run = jobs.submit_alpha({"kind": "compute_factors", "dataset_id": dataset.dataset_id,
                                 "factor_spec": {"schema_version": "1.0", "timeframe": "1d", "nodes": [{"id": "ret", "operator": "pct_change", "inputs": ["close"], "params": {"period": 1}}], "outputs": ["ret"]}})
        claim = jobs.claim()
        jobs.cancel(run["job_id"])
        assert execute_job(jobs, claim) is False
        assert jobs.get_job(run["job_id"])["status"] == "cancelled"

        bad = jobs.submit_alpha({"kind": "compute_factors", "dataset_id": "sha256:" + "f" * 64,
                                 "factor_spec": {"schema_version": "1.0", "timeframe": "1d", "nodes": [{"id": "ret", "operator": "pct_change", "inputs": ["close"], "params": {"period": 1}}], "outputs": ["ret"]}})
        claim = jobs.claim()
        assert execute_job(jobs, claim) is True
        failed = jobs.get_job(bad["job_id"])
        assert failed["status"] == "failed"
        assert failed["error_code"] == "DATA_UNAVAILABLE"


def test_factor_compile_failure_has_stable_job_error_code(tmp_path):
    registry_path = tmp_path / "registry.sqlite3"
    with JobStore(registry_path) as jobs:
        store = DatasetStore(registry_path.with_suffix(".alpha"))
        dataset = _stock(store)
        run = jobs.submit_alpha({"kind": "compute_factors", "dataset_id": dataset.dataset_id,
                                 "factor_spec": {"schema_version": "1.0", "timeframe": "1d",
                                   "nodes": [{"id": "a", "operator": "add", "inputs": ["b", "close"], "params": {}},
                                             {"id": "b", "operator": "add", "inputs": ["a", "close"], "params": {}}],
                                   "outputs": ["a"]}})
        claim = jobs.claim()
        assert execute_job(jobs, claim) is True
        job = jobs.get_job(run["job_id"])
        assert job["status"] == "failed"
        assert job["error_code"] == "DATASET_MISMATCH"
