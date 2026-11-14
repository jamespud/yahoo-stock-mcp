import pytest
from stock_quant.artifacts import ArtifactStore


def test_parquet_pages_preserve_heterogeneous_rows_and_integrity(tmp_path):
    store = ArtifactStore(tmp_path)
    rows = [{"time": "2026-01-01T00:00:00Z", "symbol": "AAPL", "value": i, "nested": {"a": [None, True, 0.1]}} if i % 2 else {"other": "中文", "value": i} for i in range(1100)]
    reference = store.write_rows(iter(rows))
    assert reference["row_count"] == 1100
    assert reference["format"] == "parquet-json-v1"
    assert store.read_rows(reference, 498, 7) == rows[498:505]
    assert store.read_rows(reference, 1099, 20) == rows[-1:]
    assert store.write_rows(iter(rows)) == reference
    path = store.resolve(reference)
    path.write_bytes(path.read_bytes()[:-4] + b"BAD!")
    with pytest.raises(ValueError, match="integrity"):
        store.read_rows(reference, 0, 1)


def test_empty_rows_bounds_path_and_cancellation(tmp_path):
    store = ArtifactStore(tmp_path)
    ref = store.write_rows(iter([]))
    assert store.read_rows(ref, 0, 10) == []
    with pytest.raises(ValueError):
        store.resolve({**ref, "content_hash": "../../secret"})
    with pytest.raises(ValueError):
        store.write_rows(iter([{"bad": float("nan")}]))
    calls = 0
    def checkpoint():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise InterruptedError("cancelled")
    with pytest.raises(InterruptedError):
        store.write_rows(({"i": i} for i in range(1100)), checkpoint=checkpoint)
    assert not list(tmp_path.glob("*.tmp"))


def test_large_job_result_uses_small_manifest_and_catalog(tmp_path):
    import json
    from stock_quant.jobs import JobStore
    from test_jobs import setup
    store, request = setup(tmp_path / "jobs.db")
    job = store.submit(request)
    claim = store.claim()
    rows = [{"value": i, "padding": "a" * 20000} for i in range(900)]
    assert store.finish(claim, {"summary": {"ok": True}, "fills": rows})
    encoded = store.connection.execute("SELECT result FROM jobs WHERE id=?", (job["job_id"],)).fetchone()[0]
    assert len(encoded) < 2000
    assert json.loads(encoded)["fills"]["$artifact"]["row_count"] == 900
    page = store.get_result(job["job_id"], "fills", 1000)
    assert 0 < len(page["rows"]) < 900
    assert page["next_cursor"]
    assert page["rows"][0] == rows[0]
    metadata = store.get_result(job["job_id"], "artifacts")["rows"]
    assert metadata[0]["format"] == "parquet-json-v1"
    assert store.connection.execute("SELECT count(*) FROM result_artifacts").fetchone()[0] == 1
    with JobStore(tmp_path / "jobs.db") as reopened:
        assert reopened.get_result(job["job_id"], "fills", 1)["rows"] == rows[:1]


def test_cancel_during_artifact_write_cannot_publish_manifest(tmp_path):
    from stock_quant.jobs import JobStore
    from test_jobs import setup
    store, request = setup(tmp_path / "jobs.db")
    job = store.submit(request)
    claim = store.claim()
    original = store.artifacts.write_rows
    def rows(source):
        for i, item in enumerate(source):
            if i == 300:
                with JobStore(tmp_path / "jobs.db") as other:
                    other.cancel(job["job_id"])
            yield item
    store.artifacts.write_rows = lambda source, **kwargs: original(rows(source), **kwargs)
    assert not store.finish(claim, {"fills": [{"i": i} for i in range(600)]})
    assert store.get_job(job["job_id"])["status"] == "cancelled"
    assert store.connection.execute("SELECT count(*) FROM result_artifacts").fetchone()[0] == 0
    assert store.connection.execute("SELECT result FROM jobs WHERE id=?", (job["job_id"],)).fetchone()[0] is None


def test_legacy_json_snapshot_and_result_remain_readable(tmp_path):
    import json
    from stock_quant.compiler import digest
    from test_jobs import setup
    from test_runtime import fixture
    store, request = setup(tmp_path / "jobs.db")
    snapshot = fixture()
    payload = {"bars": [b.model_dump(mode="json") for b in snapshot.bars], "calendar_version": snapshot.calendar_version, "actions": []}
    store.connection.execute("UPDATE data_snapshots SET payload=?,content_hash=? WHERE id=?", (json.dumps(payload), digest(payload), snapshot.id))
    store.connection.commit()
    assert store.get_snapshot(snapshot.id).id == snapshot.id
    job = store.submit(request)
    result = {"summary": {"total_return": 0.}, "fills": [{"id": 1}, {"id": 2}]}
    store.connection.execute("UPDATE jobs SET status='completed',result=?,result_hash=? WHERE id=?", (json.dumps(result), digest(result), job["job_id"]))
    store.connection.commit()
    page = store.get_result(job["job_id"], "fills", 1)
    assert page["rows"] == result["fills"][:1]
    assert store.get_result(job["job_id"], "fills", 1, page["next_cursor"])["rows"] == result["fills"][1:]
    assert store.get_result(job["job_id"], "artifacts")["rows"] == []


@pytest.mark.parametrize("payload", ["x" * 2600000, "中" * 510000])
def test_rows_exceeding_response_budget_fail_before_publication(tmp_path, payload):
    from test_jobs import setup
    store, request = setup(tmp_path / "jobs.db")
    job = store.submit(request)
    assert store.finish(store.claim(), {"fills": [{"payload": payload}]})
    assert store.get_job(job["job_id"])["status"] == "failed"
    assert store.connection.execute("SELECT count(*) FROM result_artifacts").fetchone()[0] == 0


def test_unicode_response_budget_and_catalog_integrity(tmp_path):
    from test_jobs import setup
    store, request = setup(tmp_path / "jobs.db")
    job = store.submit(request)
    rows = [{"value": "中" * 200000}] * 3
    assert store.finish(store.claim(), {"fills": rows})
    first = store.get_result(job["job_id"], "fills", 3)
    assert len(first["rows"]) == 2
    assert store.get_result(job["job_id"], "fills", 3, first["next_cursor"])["rows"] == rows[2:]
    store.connection.execute("DELETE FROM result_artifacts WHERE job_id=?", (job["job_id"],))
    store.connection.commit()
    with pytest.raises(ValueError, match="catalog integrity"):
        store.get_result(job["job_id"], "artifacts")
