import json
import sqlite3

import pytest

from stock_quant.jobs import JobStore, execute_job
from stock_quant.runtime import ExecutionConfig
from test_compiler import strategy
from test_runtime import fixture, plan


def setup(path):
    store = JobStore(path)
    raw = {"schema_version": "1.0", "name": "hold", "decision_timeframe": "1d", "entry": {"kind": "const", "value": True}, "exit": {"kind": "const", "value": False}}
    sid = store.register(raw)["strategy_id"]
    pid = store.compile(sid)["plan_id"]
    snapshot = fixture()
    store.import_snapshot([b.model_dump(mode="json") for b in snapshot.bars], snapshot.calendar_version)
    return store, {"plan_id": pid, "data_snapshot_id": snapshot.id, "symbol": "AAPL", "timeframe": "1d", "config": ExecutionConfig().model_dump()}


def test_durable_job_result_and_pagination(tmp_path):
    path = tmp_path / "jobs.db"
    store, request = setup(path)
    job = store.submit(request)
    assert job["status"] == "queued"
    store.connection.close()
    with JobStore(path) as store:
        claim = store.claim()
        assert claim["id"] == job["job_id"]
        assert execute_job(store, claim)
        assert store.get_job(job["job_id"])["status"] == "completed"
        result = store.get_result(job["job_id"], "equity_curve", 2)
        assert len(result["rows"]) == 2
        assert result["next_cursor"] is not None
        rest = store.get_result(job["job_id"], "equity_curve", 2, result["next_cursor"])
        assert len(rest["rows"]) == 2
        assert rest["next_cursor"] is not None
        assert result["total_count"] == 8
        collected = result["rows"] + rest["rows"]
        while rest["next_cursor"]:
            rest = store.get_result(job["job_id"], "equity_curve", 2, rest["next_cursor"])
            collected += rest["rows"]
        assert len(collected) == 8
        assert len({row["time"] for row in collected}) == 8
        assert store.get_result(job["job_id"], "summary")["total_return"] > 0
        with pytest.raises(ValueError):
            store.get_result(job["job_id"], "fills", 2, result["next_cursor"])


def test_cancel_fences_running_worker_and_terminal_result(tmp_path):
    store, request = setup(tmp_path / "jobs.db")
    job = store.submit(request)
    claim = store.claim()
    assert store.cancel(job["job_id"])["status"] == "cancelled"
    assert not execute_job(store, claim)
    assert store.get_job(job["job_id"])["status"] == "cancelled"
    assert store.connection.execute("SELECT result FROM jobs WHERE id=?", (job["job_id"],)).fetchone()[0] is None


def test_expired_lease_reclaimed_and_old_generation_cannot_publish(tmp_path):
    store, request = setup(tmp_path / "jobs.db")
    job = store.submit(request)
    old = store.claim()
    store.connection.execute("UPDATE jobs SET lease_until=0 WHERE id=?", (job["job_id"],))
    store.connection.commit()
    new = store.claim()
    assert old["owner"] != new["owner"]
    assert not store.heartbeat(old)
    assert not store.finish(old, {"old": True})
    assert execute_job(store, new)
    assert store.get_job(job["job_id"])["status"] == "completed"
    assert store.cancel(job["job_id"])["status"] == "completed"


def test_unknown_snapshot_plan_bad_config_fail_before_enqueue(tmp_path):
    store, request = setup(tmp_path / "jobs.db")
    for change in ({"data_snapshot_id": "missing"}, {"plan_id": "missing"}, {"config": {"initial_cash": -1}}, {"timeframe": "15m"}, {"typo": 1}):
        with pytest.raises((ValueError, KeyError)):
            store.submit({**request, **change})
    assert store.connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_claim_capacity_and_pending_cancel(tmp_path):
    store, request = setup(tmp_path / "jobs.db")
    jobs = [store.submit(request) for _ in range(5)]
    assert all(store.claim() for _ in range(4))
    assert store.claim() is None
    assert store.cancel(jobs[-1]["job_id"])["status"] == "cancelled"


def test_result_integrity_and_snapshot_integrity(tmp_path):
    store, request = setup(tmp_path / "jobs.db")
    job = store.submit(request)
    claim = store.claim()
    execute_job(store, claim)
    store.connection.execute("UPDATE jobs SET result='{}' WHERE id=?", (job["job_id"],))
    store.connection.commit()
    with pytest.raises(ValueError, match="integrity"):
        store.get_result(job["job_id"], "summary")


def test_cancel_during_execution_checkpoint_fences_publication(tmp_path):
    path = tmp_path / "jobs.db"
    store, request = setup(path)
    job = store.submit(request)
    claim = store.claim()
    original = store.heartbeat
    checks = 0
    def heartbeat(owner, progress=0.):
        nonlocal checks
        checks += 1
        if checks == 2:
            with JobStore(path) as other:
                other.cancel(job["job_id"])
        return original(owner, progress)
    store.heartbeat = heartbeat
    assert not execute_job(store, claim)
    assert checks == 2
    assert store.get_job(job["job_id"])["status"] == "cancelled"


def test_worker_runs_without_parent_pipe_and_recovery_uses_durable_queue(tmp_path):
    import os
    import subprocess
    import sys
    import time
    path = tmp_path / "jobs.db"
    store, request = setup(path)
    job = store.submit(request)
    # This short-lived control process exits immediately after dispatch. The
    # worker's inherited pipes would make communicate() hang if not detached.
    control = subprocess.run([sys.executable, "-c", "from stock_quant.worker import wake_worker; import sys; wake_worker(sys.argv[1])", str(path)], capture_output=True, timeout=5)
    assert control.returncode == 0, control.stderr
    deadline = time.monotonic() + 5
    while store.get_job(job["job_id"])["status"] in ("queued", "running") and time.monotonic() < deadline:
        time.sleep(.02)
    assert store.get_job(job["job_id"])["status"] == "completed"


def test_failed_execution_has_durable_error_and_no_result(tmp_path):
    store, request = setup(tmp_path / "jobs.db")
    request["start"] = "2099-01-01T00:00:00Z"
    job = store.submit(request)
    assert execute_job(store, store.claim())
    assert store.get_job(job["job_id"])["status"] == "failed"
    assert store.get_job(job["job_id"])["error_code"] == "EXECUTION_FAILED"
    with pytest.raises(ValueError, match="RESULT_NOT_READY"):
        store.get_result(job["job_id"])


def test_independent_processes_cannot_claim_more_than_capacity(tmp_path):
    import concurrent.futures
    import subprocess
    import sys
    path = tmp_path / "jobs.db"
    store, request = setup(path)
    for _ in range(6):
        store.submit(request)
    code = "from stock_quant.jobs import JobStore; import json,sys; s=JobStore(sys.argv[1]); c=s.claim(); print(json.dumps(c['id'] if c else None))"
    def claim_one(_):
        result = subprocess.run([sys.executable, "-c", code, str(path)], capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        claims = list(pool.map(claim_one, range(6)))
    claimed = [item for item in claims if item]
    assert len(claimed) == 4
    assert len(set(claimed)) == 4
    assert store.connection.execute("SELECT count(*) FROM jobs WHERE status='running'").fetchone()[0] == 4


def test_submit_retry_recovers_job_reference_and_rejects_key_reuse(tmp_path):
    store, request = setup(tmp_path / "jobs.db")
    request["idempotency_key"] = "client-request-1"
    first = store.submit(request)
    second = store.submit(request)
    assert first["job_id"] == second["job_id"]
    assert store.connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
    with pytest.raises(ValueError, match="IDEMPOTENCY_CONFLICT"):
        store.submit({**request, "config": {"initial_cash": 20000}})


def test_completed_result_records_source_and_environment_identity(tmp_path):
    store, request = setup(tmp_path / "jobs.db")
    job = store.submit(request)
    execute_job(store, store.claim())
    record = store.get_result(job["job_id"], "reproducibility")
    assert record["data_snapshot_id"] == request["data_snapshot_id"]
    assert record["environment"]["runtime_source_hash"].startswith("sha256:")
    assert record["environment"]["pydantic"]
    assert record["environment"]["python"]
    assert store.get_plan(request["plan_id"])["compiler_version"].startswith("1.0+")


def test_evaluate_signal_uses_same_pit_view_and_warmup(tmp_path):
    store, request = setup(tmp_path / "jobs.db")
    raw = {"schema_version": "1.0", "name": "threshold", "decision_timeframe": "1d", "entry": {"kind": "binary", "op": "gt", "left": {"kind": "ref", "symbol": "AAPL", "field": "close", "timeframe": "1d"}, "right": {"kind": "const", "value": 10}}, "exit": {"kind": "const", "value": False}}
    registered = store.register(raw)
    early = store.evaluate(registered["strategy_id"], request["data_snapshot_id"], "2026-01-02T17:00:00Z")
    assert early["valid"] is False
    assert early["errors"][0]["code"] == "DATA_UNAVAILABLE"
    assert early["signals"] is None
    later = store.evaluate(registered["strategy_id"], request["data_snapshot_id"], "2026-01-02T22:00:00Z")
    assert later["valid"] is True
    assert later["signals"] == {"entry": True, "exit": False}
    with pytest.raises(ValueError):
        store.evaluate(registered["strategy_id"], request["data_snapshot_id"], "2026-01-02T22:00:00")


def test_portfolio_job_preserves_corporate_action_snapshot_and_ledgers(tmp_path):
    store, request = setup(tmp_path / "jobs.db")
    rows = [b.model_copy(update={"open": 100., "high": 100., "low": 100., "close": 100.}) for b in fixture().bars]
    for i in (2, 3):
        rows[i] = rows[i].model_copy(update={"open": 50., "high": 50., "low": 50., "close": 50.})
    action = {"kind": "split", "id": "persisted_split", "symbol": "AAPL", "ratio": 2., "effective_at": rows[2].start_time.isoformat(), "available_at": rows[0].start_time.isoformat()}
    snapshot = store.import_snapshot([b.model_dump(mode="json") for b in rows], "golden-actions", [action])
    assert snapshot["action_count"] == 1
    assert store.get_snapshot(snapshot["data_snapshot_id"]).actions[0].ratio == 2
    job = store.submit({"portfolio": {"AAPL": request["plan_id"]}, "data_snapshot_id": snapshot["data_snapshot_id"], "config": {"quantity": 2., "initial_cash": 1000.}})
    assert execute_job(store, store.claim())
    assert store.get_job(job["job_id"])["status"] == "completed"
    account = store.get_result(job["job_id"], "final_account")
    assert account["positions"]["AAPL"]["quantity"] == 4
    assert account["positions"]["AAPL"]["basis"] == 200
    assert account["equity"] == pytest.approx(1000.)
    assert store.get_result(job["job_id"], "corporate_actions")["rows"][0]["id"] == "persisted_split"
    with pytest.raises(ValueError):
        store.submit({**request, "portfolio": {"AAPL": request["plan_id"]}})


def test_research_jobs_share_durable_queue_pagination_and_cancellation(tmp_path):
    from test_parameters import template
    from test_research import research_snapshot, windows
    store, _ = setup(tmp_path / "jobs.db")
    sid = store.register(template())["strategy_id"]
    snapshot = research_snapshot()
    store.import_snapshot([b.model_dump(mode="json") for b in snapshot.bars], snapshot.calendar_version)
    job = store.submit({"kind": "optimize", "strategy_id": sid, "data_snapshot_id": snapshot.id, "symbol": "AAPL", "search_space": {"period": [1, 2]}, "windows": windows(), "train_top_k": 1})
    claim = store.claim()
    assert execute_job(store, claim)
    assert store.get_job(job["job_id"])["status"] == "completed"
    page = store.get_result(job["job_id"], "trials", 1)
    assert page["next_cursor"]
    folds = store.get_result(job["job_id"], "walk_forward")
    assert folds["total_count"] == 2
    for fold in folds["rows"]:
        assert store.load_plan(fold["selected_plan_id"]).symbol_binding == "AAPL"
    with pytest.raises(ValueError, match="RESULT_SECTION_UNAVAILABLE"):
        store.get_result(job["job_id"], "fills")
    screening = store.submit({"kind": "screen", "strategy_id": sid, "data_snapshot_id": snapshot.id, "symbols": ["AAPL"], "as_of": snapshot.bars[-1].available_at.isoformat()})
    claim = store.claim()
    store.cancel(screening["job_id"])
    assert not execute_job(store, claim)
    assert store.get_job(screening["job_id"])["status"] == "cancelled"


def test_screen_publishes_valid_rows_and_executable_plans_despite_bad_symbol(tmp_path):
    from test_parameters import template
    store, _ = setup(tmp_path / "jobs.db")
    raw = template()
    raw["entry"]["left"]["args"][0]["adjustment"] = "split_asof"
    sid = store.register(raw)["strategy_id"]
    a = fixture().bars[0]
    b = a.model_copy(update={"symbol": "MSFT", "adjustment": "split"})
    snap = store.import_snapshot([a.model_dump(mode="json"), b.model_dump(mode="json")], "test")
    job = store.submit({"kind": "screen", "strategy_id": sid, "data_snapshot_id": snap["data_snapshot_id"], "symbols": ["AAPL", "MSFT"], "as_of": a.available_at.isoformat(), "parameters": {"period": 1, "threshold": 0}})
    assert execute_job(store, store.claim())
    assert store.get_job(job["job_id"])["status"] == "completed"
    rows = store.get_result(job["job_id"], "screen")["rows"]
    assert sum(row["selected"] for row in rows) == 1
    assert store.load_plan(rows[0]["plan_id"]).symbol_binding == "AAPL"
