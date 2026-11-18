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


def test_alpha_publication_is_two_phase_and_fenced(tmp_path):
    registry_path = tmp_path / "registry.sqlite3"
    with JobStore(registry_path) as jobs:
        store = DatasetStore(registry_path.with_suffix(".alpha"))
        dataset = _stock(store)
        run = jobs.submit_alpha({"kind": "compute_factors", "dataset_id": dataset.dataset_id,
                                 "factor_spec": {"schema_version": "1.0", "timeframe": "1d", "nodes": [{"id": "ret", "operator": "pct_change", "inputs": ["close"], "params": {"period": 1}}], "outputs": ["ret"]}})
        claim = jobs.claim()
        assert jobs.begin_publish(claim, {"summary": {"ok": True}}) is True
        internal = jobs.connection.execute("SELECT status FROM jobs WHERE id=?", (run["job_id"],)).fetchone()[0]
        assert internal == "publishing"
        public_job = jobs.get_job(run["job_id"])
        assert public_job["status"] == "running" and public_job["phase"] == "publishing"
        assert jobs.complete_publish(claim) is True
        assert jobs.get_job(run["job_id"])["status"] == "completed"


def test_dataset_publication_failure_never_leaves_completed_job(tmp_path, monkeypatch):
    from stock_quant.alpha.data.catalog import DatasetStore as StoreClass
    registry_path = tmp_path / "registry.sqlite3"
    with JobStore(registry_path) as jobs:
        store = StoreClass(registry_path.with_suffix(".alpha"))
        dataset = _stock(store)
        run = jobs.submit_alpha({"kind": "compute_factors", "dataset_id": dataset.dataset_id,
                                 "factor_spec": {"schema_version": "1.0", "timeframe": "1d", "nodes": [{"id": "ret", "operator": "pct_change", "inputs": ["close"], "params": {"period": 1}}], "outputs": ["ret"]}})
        claim = jobs.claim()
        def fail_register(self, manifest):
            raise RuntimeError("catalog unavailable")
        monkeypatch.setattr(StoreClass, "register", fail_register)
        assert execute_job(jobs, claim) is True
        job = jobs.get_job(run["job_id"])
        assert job["status"] == "failed"
        assert job["error_code"] == "EXECUTION_FAILED"


def test_model_commit_failure_never_leaves_completed_job(tmp_path, monkeypatch):
    from stock_quant.alpha.data.catalog import DatasetStore as StoreClass
    from stock_quant.alpha.models.registry import AlphaModelRegistry
    registry_path = tmp_path / "registry.sqlite3"
    with JobStore(registry_path) as jobs:
        store = StoreClass(registry_path.with_suffix(".alpha"))
        dataset = _stock(store)
        factor_run = jobs.submit_alpha({"kind": "compute_factors", "dataset_id": dataset.dataset_id,
            "factor_spec": {"schema_version": "1.0", "timeframe": "1d", "nodes": [{"id": "ret", "operator": "pct_change", "inputs": ["close"], "params": {"period": 1}}], "outputs": ["ret"]}})
        assert execute_job(jobs, jobs.claim()) is True
        feature_id = jobs.get_result(factor_run["job_id"], "dataset")["dataset_id"]
        train_run = jobs.submit_alpha({
            "kind": "train_alpha_model", "feature_set_id": feature_id, "label_name": "stock_forward_return",
            "target_type": "stock_return", "stock_dataset_id": dataset.dataset_id, "horizon_days": 1,
            "split": {"train_start": "2024-01-01T00:00:00Z", "train_end": "2024-01-05T00:00:00Z",
                      "validation_start": "2024-01-05T00:00:00Z", "validation_end": "2024-01-08T00:00:00Z",
                      "test_start": "2024-01-08T00:00:00Z", "test_end": "2024-01-11T00:00:00Z"},
            "hyperparameters": {"n_estimators": 4, "min_child_samples": 1}, "resource_budget": {"max_seconds": 60, "max_rows": 1000},
        })
        claim = jobs.claim()
        def fail_commit(self, staging, model_id):
            raise RuntimeError("model registry unavailable")
        monkeypatch.setattr(AlphaModelRegistry, "commit_from", fail_commit)
        assert execute_job(jobs, claim) is True
        job = jobs.get_job(train_run["job_id"])
        assert job["status"] == "failed"
        assert job["error_code"] == "EXECUTION_FAILED"
