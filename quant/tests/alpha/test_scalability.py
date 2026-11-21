import pytest

from stock_quant.alpha.contracts import DatasetManifest
from stock_quant.alpha.data.catalog import DatasetStore
from stock_quant.alpha.jobs.executor import _manifest_payload
from stock_quant.jobs import JobStore


def _claim(store):
    store.submit_alpha({
        "kind": "prepare_dataset", "symbols": ["AAPL"],
        "start": "2002-01-01T00:00:00Z", "end": "2012-01-01T00:00:00Z",
    })
    return store.claim()


def test_serialization_failure_does_not_authorize_registration(tmp_path):
    from stock_quant.artifacts import MAX_RESPONSE_ROW_BYTES

    with JobStore(str(tmp_path / "quant.db")) as store:
        claim = _claim(store)
        assert store.begin_publish(claim, {"data_quality": [{"detail": "x" * MAX_RESPONSE_ROW_BYTES}]}) is False
        assert store.get_job(claim["id"])["status"] == "failed"
        assert store.complete_publish(claim) is False


def test_cancelled_worker_cannot_begin_publication(tmp_path):
    with JobStore(str(tmp_path / "quant.db")) as store:
        claim = _claim(store)
        store.cancel(claim["id"])
        assert store.begin_publish(claim, {"data_quality": []}) is False
        assert store.get_job(claim["id"])["status"] == "cancelled"


def _manifest(quality):
    return DatasetManifest(
        dataset_id="sha256:" + "a" * 64, content_hash="sha256:" + "b" * 64,
        schema_version="1.0", dataset_kind="stock_bars", source="yahoo", source_version="test",
        calendar_version="test", universe_definition={"mode": "explicit"}, instrument_ids=["AAPL"],
        start_at="2002-01-01T00:00:00Z", end_at="2012-01-01T00:00:00Z",
        captured_at="2012-01-01T00:00:00Z", availability_policy="modeled_bar_end_plus_delay",
        price_basis="price_basis_unverified", data_quality=quality, partition_manifest=[{
            "ordinal": 0, "logical_path": "bars/part.parquet", "format": "parquet-v1",
            "content_hash": "sha256:" + "c" * 64, "schema_hash": "sha256:" + "d" * 64,
            "row_count": 5, "byte_count": 100,
        }],
    )


def test_large_stock_result_only_inlines_summary():
    quality = {"coverage": 1.0, "exchange_mappings": {f"S{i}": {"session_dates": ["2002-01-02"] * 2520} for i in range(100)}}
    manifest = _manifest(quality)
    payload = _manifest_payload(manifest)
    assert payload["dataset_id"] == manifest.dataset_id
    assert payload["partition_count"] == 1
    assert payload["row_count"] == 5
    assert payload["quality_summary"] == {"coverage": 1.0}
    assert payload["quality_detail_reference"]["dataset_id"] == manifest.dataset_id
    assert "dataset" not in payload and "partition_manifest" not in payload and "data_quality" not in payload
    assert manifest.data_quality == quality


def test_small_stock_payload_keeps_inline_contract():
    manifest = _manifest({"coverage": 1.0})
    payload = _manifest_payload(manifest)
    assert payload["dataset"] == manifest.model_dump(mode="json")
    assert payload["data_quality"] == manifest.data_quality


def test_stock_payload_above_one_megabyte_preserves_legacy_when_it_fits():
    manifest = _manifest({"disclosure": "x" * 1_100_000})
    payload = _manifest_payload(manifest)
    assert payload["data_quality"] == manifest.data_quality


@pytest.mark.parametrize("path", ["begin_publish", "finish"])
def test_new_job_rejects_a_row_that_cannot_fit_even_alone(tmp_path, path):
    # Fits the single-encoded result budget, but not the MCP text envelope.
    with JobStore(tmp_path / "quant.db") as store:
        claim = _claim(store)
        assert getattr(store, path)(claim, {"data_quality": [{"detail": '\\' * 1_100_000}]}) is (path == "finish")
        assert store.get_job(claim["id"])["status"] == "failed"


def test_non_result_artifact_keeps_existing_storage_budget(tmp_path):
    from stock_quant.artifacts import ArtifactStore

    store = ArtifactStore(tmp_path)
    row = {"detail": "\\" * 1_100_000}
    assert store.write_rows([row])["row_count"] == 1


@pytest.mark.parametrize("target_bytes,accepted", [
    (2_499_998, True),
    (2_499_999, False),
    (2_500_000, False),
])
def test_job_artifact_admission_and_result_page_share_array_budget(tmp_path, target_bytes, accepted):
    import json

    from stock_quant.artifacts import MAX_RESPONSE_ROW_BYTES

    empty = len(json.dumps({"detail": ""}).encode())
    row = {"detail": "x" * (target_bytes - empty)}
    assert len(json.dumps(row).encode()) == target_bytes
    with JobStore(tmp_path / "quant.db") as store:
        claim = _claim(store)
        begun = store.begin_publish(claim, {"data_quality": [row]})
        assert begun is accepted
        if not accepted:
            assert store.get_job(claim["id"])["status"] == "failed"
            return
        assert store.complete_publish(claim) is True
        page = store.get_result(claim["id"], section="data_quality")
        assert page["rows"] == [row]
        assert len(json.dumps(page["rows"]).encode()) <= MAX_RESPONSE_ROW_BYTES


def test_dataset_admission_counts_array_brackets(tmp_path):
    import json

    from stock_quant.artifacts import MAX_RESPONSE_ROW_BYTES

    row = {"detail": "x" * (MAX_RESPONSE_ROW_BYTES - len(json.dumps({"detail": ""})))}
    store = DatasetStore(tmp_path)
    with pytest.raises(ValueError, match="one exchange_mappings row"):
        store._validate_audit_rows(_manifest({"exchange_mappings": {"AAPL": row}}))


def test_dataset_page_cap_counts_array_brackets_and_separators(tmp_path, monkeypatch):
    import json

    from stock_quant.artifacts import MAX_RESPONSE_ROW_BYTES

    row = {"detail": "x" * (MAX_RESPONSE_ROW_BYTES // 2 - len(json.dumps({"detail": ""})))}
    manifest = _manifest({"exchange_mappings": {"A": row, "B": row}})
    store = DatasetStore(tmp_path)
    monkeypatch.setattr(store, "load_manifest", lambda _: manifest)
    page = store.audit_dataset(manifest.dataset_id, "exchange_mappings")
    assert len(json.dumps(page["rows"]).encode()) <= MAX_RESPONSE_ROW_BYTES
    assert len(page["rows"]) == 1
    last = store.audit_dataset(manifest.dataset_id, "exchange_mappings", cursor=page["next_cursor"])
    assert last["rows"] == [row] and last["next_cursor"] is None


def test_large_mcp_overhead_reduces_page_without_losing_rows():
    from stock_quant.pagination import result_page

    rows = [{"payload": "x" * 1000}] * 2
    def page(cursor=None):
        return result_page("dataset", "section", "hash", len(rows), 100, cursor,
                           lambda offset, limit: rows[offset:offset + limit], resource_uri="",
                           byte_limit=2500000, mcp_overhead_bytes=4_194_304 - 1700)
    first = page()
    assert first["rows"] == rows[:1] and first["next_cursor"]
    second = page(first["next_cursor"])
    assert second["rows"] == rows[1:] and second["next_cursor"] is None


def test_dataset_audit_pages_bind_identity_and_section(tmp_path, monkeypatch):
    manifest = _manifest({"exchange_mappings": {symbol: {"instrument_id": symbol} for symbol in ("C", "A", "B")}})
    store = DatasetStore(tmp_path)
    monkeypatch.setattr(store, "load_manifest", lambda _: manifest)
    cursor = None
    collected = []
    for symbol in ("A", "B", "C"):
        page = store.audit_dataset(manifest.dataset_id, "exchange_mappings", 1, cursor)
        assert page["total_count"] == 3
        assert page["rows"] == [{"instrument_id": symbol}]
        collected.extend(page["rows"])
        cursor = page["next_cursor"]
        if cursor:
            with pytest.raises(ValueError, match="cursor"):
                store.audit_dataset(manifest.dataset_id, "partition_manifest", 1, cursor)
            with pytest.raises(ValueError, match="cursor"):
                store.audit_dataset("sha256:" + "e" * 64, "exchange_mappings", 1, cursor)
            with pytest.raises(ValueError, match="cursor"):
                store.audit_dataset(manifest.dataset_id, "exchange_mappings", 1, cursor, symbol="A")
    assert cursor is None
    assert page["hash_version"] == "logical-rows-f64-v1"
    empty = store.audit_dataset(manifest.dataset_id, "session_coverage_by_symbol", 10)
    assert empty["rows"] == [] and empty["total_count"] == 0 and empty["next_cursor"] is None
    with pytest.raises(ValueError, match="cursor"):
        store.audit_dataset(manifest.dataset_id, "exchange_mappings", cursor="")


def test_changed_logical_rows_reject_old_cursor(tmp_path, monkeypatch):
    mappings = {symbol: {"coverage": 1.0} for symbol in "AB"}
    manifest = _manifest({"exchange_mappings": mappings})
    store = DatasetStore(tmp_path)
    monkeypatch.setattr(store, "load_manifest", lambda _: manifest)
    page = store.audit_dataset(manifest.dataset_id, "exchange_mappings", limit=1)
    manifest.data_quality["exchange_mappings"]["B"]["coverage"] = 0.5
    with pytest.raises(ValueError, match="cursor"):
        store.audit_dataset(manifest.dataset_id, "exchange_mappings", cursor=page["next_cursor"])


def test_empty_legacy_result_cursor_is_first_page(tmp_path):
    with JobStore(tmp_path / "quant.db") as store:
        claim = _claim(store)
        assert store.begin_publish(claim, {"data_quality": [{"quality": True}]})
        assert store.complete_publish(claim)
        assert store.get_result(claim["id"], "data_quality", cursor="")["rows"] == [{"quality": True}]


def test_dataset_page_fits_after_mcp_text_escaping(tmp_path, monkeypatch):
    import json

    row = {"data": "\\" * 415_000}
    manifest = _manifest({"exchange_mappings": {symbol: row for symbol in "ABC"}})
    store = DatasetStore(tmp_path)
    monkeypatch.setattr(store, "load_manifest", lambda _: manifest)
    page = store.audit_dataset(manifest.dataset_id, "exchange_mappings")
    message = {"jsonrpc": "2.0", "id": 20, "result": {
        "content": [{"type": "text", "text": json.dumps(page, separators=(",", ":"))}],
    }}
    assert len(json.dumps(message).encode()) + 1 <= 4 * 1024 * 1024
    assert 0 < len(page["rows"]) < 3
    tail = store.audit_dataset(manifest.dataset_id, "exchange_mappings", cursor=page["next_cursor"])
    assert page["rows"] + tail["rows"] == [row] * 3
    assert tail["next_cursor"] is None
