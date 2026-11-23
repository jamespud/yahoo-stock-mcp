from __future__ import annotations

import hashlib
import math
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from stock_quant.alpha.contracts import DatasetManifest, DatasetPartition, StockDatasetRequest
from stock_quant.alpha.data.catalog import DatasetStore
from stock_quant.alpha.data.manifests import canonical_json, content_hash, dataset_identity
from stock_quant.alpha.data.partitions import StockBarRow, write_stock_partition
from stock_quant.alpha.data.research_quality import (
    ResearchQualityLimits,
    TrustedResearchEvidence,
    assess_research_quality,
)
from stock_quant.alpha.errors import AlphaError, AlphaErrorCode
from stock_quant.alpha.factors.ast import FactorSpec
from stock_quant.alpha.factors.compiler import compile_factor_spec
from stock_quant.alpha.factors.engine import compute_stock_factors


START = datetime(2024, 1, 1, tzinfo=timezone.utc)
END = datetime(2024, 1, 10, tzinfo=timezone.utc)


def _rows(*, symbols=("AAA",), days=3, adjustment="raw", available_delta=timedelta(minutes=5), source="yahoo"):
    rows = []
    sessions = [datetime(2024, 1, day, 14, 30, tzinfo=timezone.utc) for day in (2, 3, 4)]
    for symbol_index, symbol in enumerate(symbols):
        for index, start in enumerate(sessions[:days]):
            close = 100.0 + symbol_index + index
            end = start.replace(hour=21, minute=0)
            rows.append({
                "instrument_id": symbol,
                "start_at": start,
                "end_at": end,
                "available_at": end + available_delta,
                "open": close - 0.5,
                "high": close + 1,
                "low": close - 1,
                "close": close,
                "volume": 1000 + index,
                "adjustment": adjustment,
                "source": source,
                "source_version": "fixture-v1",
                "currency": "USD",
            })
    return rows


def _complete_session_quality(symbols=("AAA",)):
    reports = [{
        "instrument_id": symbol,
        "calendar_id": "fixture",
        "first_observed_session": "2024-01-02",
        "last_observed_session": "2024-01-04",
        "expected_sessions_within_observed_range": 3,
        "observed_sessions_within_observed_range": 3,
        "missing_sessions": [],
        "extra_sessions": [],
        "duplicate_sessions": [],
        "missing_session_count": 0,
        "extra_session_count": 0,
        "duplicate_session_count": 0,
        "session_coverage": 1.0,
        "session_status": "COMPLETE",
        "outside_observed_range_status": "UNVERIFIED",
    } for symbol in symbols]
    return {"session_coverage": 1.0, "session_coverage_by_symbol": reports}


def _publish(tmp_path, *, rows=None, symbols=("AAA",), price_basis="price_basis_unverified",
             universe_definition=None, adjustment="raw", source="yahoo", data_quality=None):
    store = DatasetStore(tmp_path / "alpha")
    request = StockDatasetRequest(
        source=source,
        symbols=list(symbols),
        start=START,
        end=END,
        price_basis=price_basis,
        adjustment=adjustment,
        universe_definition=universe_definition or {},
    )
    quality = _complete_session_quality(symbols) if data_quality is None else data_quality
    manifest = store.publish_stock(
        request,
        _rows(symbols=symbols, adjustment=adjustment, source=source) if rows is None else rows,
        calendar_version="fixture",
        source_version="fixture-v1",
        data_quality=quality,
    )
    return store, manifest


def _all_evidence(manifest: DatasetManifest):
    symbols = tuple(sorted(manifest.instrument_ids))
    return TrustedResearchEvidence(
        dataset_id=manifest.dataset_id,
        price_basis=manifest.price_basis,
        price_symbols=symbols,
        price_start_at=manifest.start_at,
        price_end_at=manifest.end_at,
        price_evidence_ids=("independent-price-1",),
        price_independent_source="fixture-independent-reference",
        corporate_action_evidence_ids=("corporate-actions-1",),
        temporal_policy=manifest.availability_policy,
        temporal_symbols=symbols,
        temporal_start_at=manifest.start_at,
        temporal_end_at=manifest.end_at,
        temporal_evidence_ids=("publication-log-1",),
        universe_symbols=symbols,
        universe_start_at=manifest.start_at,
        universe_end_at=manifest.end_at,
        universe_evidence_ids=("pit-membership-1",),
    )


def _feature_rows(manifest, *, available_at=None):
    at = datetime(2024, 1, 4, 21, 5, tzinfo=timezone.utc)
    return [{
        "decision_at": at,
        "instrument_id": "AAA",
        "factor_name": "ret",
        "factor_value": 0.01,
        "feature_available_at": available_at or at,
        "eligible": True,
        "quality_flags": [],
        "factor_version": "fixture-v1",
    }]


def _publish_feature(store, parents, *, rows=None):
    parent = parents[0]
    return store.publish_feature_panel(
        _feature_rows(parent) if rows is None else rows,
        parent_dataset_id=parent.dataset_id,
        parent_dataset_ids=[item.dataset_id for item in parents],
        factor_spec_hash="sha256:" + "a" * 64,
        instrument_ids=["AAA"],
        start_at=START,
        end_at=END,
        calendar_version="fixture",
        price_basis=parent.price_basis,
    )


def _unchecked_publish_stock(store, request, rows, *, data_quality):
    stage = Path(tempfile.mkdtemp(prefix="stage-", dir=store.staging))
    try:
        logical = "stock_bars/instrument_id=AAA/part-00000.parquet"
        constructed = [StockBarRow.model_construct(**row) for row in rows]
        file_hash, schema_hash, row_count, byte_count = write_stock_partition(stage / logical, constructed)
        partition = DatasetPartition(
            ordinal=0,
            logical_path=logical,
            format="parquet-v1",
            content_hash=file_hash,
            schema_hash=schema_hash,
            row_count=row_count,
            byte_count=byte_count,
        )
        body = {
            "schema_version": "1.0",
            "dataset_kind": "stock_bars",
            "source": request.source,
            "source_version": "fixture-v1",
            "calendar_version": "fixture",
            "universe_definition": {"mode": "explicit", "symbols": request.symbols},
            "instrument_ids": request.symbols,
            "start_at": request.start.astimezone(timezone.utc).isoformat(),
            "end_at": request.end.astimezone(timezone.utc).isoformat(),
            "availability_policy": request.availability_policy,
            "price_basis": request.price_basis,
            "data_quality": data_quality,
            "parent_dataset_ids": [],
        }
        partitions = [partition]
        identity = dataset_identity(body, partitions)
        manifest = DatasetManifest.model_validate({
            **body,
            "dataset_id": identity,
            "captured_at": datetime(2024, 1, 10, tzinfo=timezone.utc),
            "partition_manifest": [partition.model_dump(mode="json")],
            "content_hash": content_hash(partitions),
        })
        (stage / "manifest.json").write_bytes(canonical_json(manifest.model_dump(mode="json")))
        destination = store.datasets / identity.removeprefix("sha256:")
        os.rename(stage, destination)
        store.register(manifest)
        return manifest
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)


def _codes(report):
    return {finding.code for finding in report.findings}


def test_valid_artifact_does_not_imply_research_validation(tmp_path):
    store, manifest = _publish(
        tmp_path,
        price_basis="raw_attested",
        data_quality={**_complete_session_quality(), "price_attestation_status": "VERIFIED"},
    )
    assert store.validate_dataset(manifest.dataset_id).valid is True
    report = assess_research_quality(manifest.dataset_id, store)
    assert report.research_readiness == "RESEARCH_EXPLORATORY"
    assert report.dimensions["price_attestation"].status == "UNVERIFIED"
    assert report.dimensions["temporal_availability"].status == "UNVERIFIED"
    assert report.dimensions["universe_validity"].status == "UNVERIFIED"
    assert {"PRICE_ATTESTATION_UNVERIFIED", "PIT_AVAILABILITY_MODELED", "UNIVERSE_MEMBERSHIP_UNVERIFIED"} <= _codes(report)


@pytest.mark.parametrize(("basis", "adjustment"), [
    ("raw_attested", "raw"),
    ("split_adjusted", "split"),
    ("total_return", "total_return"),
])
def test_declared_price_basis_is_not_an_attestation(tmp_path, basis, adjustment):
    store, manifest = _publish(tmp_path, price_basis=basis, adjustment=adjustment)
    report = assess_research_quality(manifest.dataset_id, store)
    assert report.dimensions["price_attestation"].status == "UNVERIFIED"
    assert report.dimensions["corporate_action_coverage"].status == "UNVERIFIED"
    assert "PRICE_BASIS_DECLARED_NOT_ATTESTED" in _codes(report)


def test_row_basis_conflict_blocks_research(tmp_path):
    store, manifest = _publish(tmp_path, rows=_rows(adjustment="split"), price_basis="raw_attested", adjustment="split")
    report = assess_research_quality(manifest.dataset_id, store)
    assert report.dimensions["price_semantics"].status == "BLOCKED"
    assert report.dimensions["price_attestation"].status == "BLOCKED"
    assert report.research_readiness == "RESEARCH_BLOCKED"
    assert "PRICE_ROW_BASIS_CONFLICT" in _codes(report)


def test_mixed_row_adjustments_block_research(tmp_path):
    rows = _rows()
    rows[1]["adjustment"] = "split"
    store, manifest = _publish(tmp_path, rows=rows)
    report = assess_research_quality(manifest.dataset_id, store)
    assert report.dimensions["price_semantics"].status == "BLOCKED"
    assert report.dimensions["price_attestation"].status == "BLOCKED"
    assert "PRICE_ROW_BASIS_INCONSISTENT" in _codes(report)


def test_trusted_synthetic_evidence_can_reach_validated_state(tmp_path):
    store, manifest = _publish(tmp_path, price_basis="raw_attested")
    report = assess_research_quality(manifest.dataset_id, store, evidence=_all_evidence)
    assert report.research_readiness == "RESEARCH_DATA_VALIDATED"
    assert all(dimension.status in {"VERIFIED", "NOT_APPLICABLE"} for dimension in report.dimensions.values())
    assert "PRICE_SEMANTICS_VERIFIED" in _codes(report)
    assert "UNIVERSE_MEMBERSHIP_VERIFIED" in _codes(report)


def test_report_hash_is_deterministic_and_manifest_is_unchanged(tmp_path):
    store, manifest = _publish(tmp_path)
    path = store.datasets / manifest.dataset_id.removeprefix("sha256:") / "manifest.json"
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    first = assess_research_quality(manifest.dataset_id, store)
    second = assess_research_quality(manifest.dataset_id, store)
    after = hashlib.sha256(path.read_bytes()).hexdigest()
    assert first == second
    assert first.report_hash == second.report_hash
    assert before == after
    assert store.load_manifest(manifest.dataset_id).dataset_id == manifest.dataset_id


def test_report_hash_rejects_mutation(tmp_path):
    from stock_quant.alpha.data.research_quality import ResearchQualityReport

    store, manifest = _publish(tmp_path)
    report = assess_research_quality(manifest.dataset_id, store)
    payload = report.model_dump(mode="json")
    payload["research_readiness"] = "RESEARCH_DATA_VALIDATED"
    with pytest.raises(ValidationError, match="report hash mismatch"):
        ResearchQualityReport.model_validate(payload)


def test_capture_time_is_display_metadata_not_report_identity(tmp_path):
    import json

    store, manifest = _publish(tmp_path)
    first = assess_research_quality(manifest.dataset_id, store)
    manifest_path = store.datasets / manifest.dataset_id.removeprefix("sha256:") / "manifest.json"
    payload = json.loads(manifest_path.read_text())
    payload["captured_at"] = "2030-01-01T00:00:00Z"
    manifest_path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    assert store.load_manifest(manifest.dataset_id).dataset_id == manifest.dataset_id
    second = assess_research_quality(manifest.dataset_id, store)
    assert first.report_hash == second.report_hash
    assert first.findings == second.findings


def test_impossible_availability_time_is_blocking(tmp_path):
    store = DatasetStore(tmp_path / "alpha")
    row = _rows()[0]
    row["available_at"] = row["end_at"] - timedelta(seconds=1)
    request = StockDatasetRequest(source="yahoo", symbols=["AAA"], start=START, end=END)
    manifest = _unchecked_publish_stock(store, request, [row], data_quality={"session_coverage_by_symbol": []})
    report = assess_research_quality(manifest.dataset_id, store)
    assert report.dimensions["temporal_availability"].status == "BLOCKED"
    assert report.research_readiness == "RESEARCH_BLOCKED"
    assert "PIT_AVAILABLE_BEFORE_OBSERVATION_END" in _codes(report)


def test_duplicate_bars_are_blocking(tmp_path):
    store = DatasetStore(tmp_path / "alpha")
    row = _rows()[0]
    request = StockDatasetRequest(source="yahoo", symbols=["AAA"], start=START, end=END)
    manifest = _unchecked_publish_stock(store, request, [row, dict(row)], data_quality={"session_coverage_by_symbol": []})
    report = assess_research_quality(manifest.dataset_id, store)
    assert report.dimensions["artifact_integrity"].status == "BLOCKED"
    assert "DUPLICATE_STOCK_BAR" in _codes(report)


def test_overlapping_stock_intervals_are_blocking(tmp_path):
    store = DatasetStore(tmp_path / "alpha")
    rows = _rows()
    overlapping = dict(rows[0])
    overlapping["end_at"] = rows[0]["end_at"] + timedelta(minutes=1)
    overlapping["available_at"] = overlapping["end_at"] + timedelta(minutes=1)
    request = StockDatasetRequest(source="yahoo", symbols=["AAA"], start=START, end=END)
    manifest = _unchecked_publish_stock(store, request, [rows[0], overlapping], data_quality={"session_coverage_by_symbol": []})
    report = assess_research_quality(manifest.dataset_id, store)
    assert report.dimensions["artifact_integrity"].status == "BLOCKED"
    assert "STOCK_ROW_OVERLAP" in _codes(report)


def test_invalid_ohlc_and_volume_are_counted_without_auto_repair(tmp_path):
    store = DatasetStore(tmp_path / "alpha")
    row = _rows()[0]
    row["high"] = row["close"] - 1
    row["volume"] = -10
    request = StockDatasetRequest(source="yahoo", symbols=["AAA"], start=START, end=END)
    manifest = _unchecked_publish_stock(store, request, [row], data_quality={"session_coverage_by_symbol": []})
    report = assess_research_quality(manifest.dataset_id, store)
    assert report.dimensions["artifact_integrity"].status == "BLOCKED"
    assert {"INVALID_STOCK_OHLC", "INVALID_STOCK_VOLUME"} <= _codes(report)
    assert store.read_rows(manifest.dataset_id)[0]["high"] == row["high"]


@pytest.mark.parametrize("price", [0.0, -1.0, float("nan"), float("inf")])
def test_nonpositive_or_nonfinite_ohlc_is_blocking(tmp_path, price):
    store = DatasetStore(tmp_path / "alpha")
    row = _rows()[0]
    row["open"] = row["high"] = row["low"] = row["close"] = price
    request = StockDatasetRequest(source="yahoo", symbols=["AAA"], start=START, end=END)
    manifest = _unchecked_publish_stock(store, request, [row], data_quality={"session_coverage_by_symbol": []})
    report = assess_research_quality(manifest.dataset_id, store)
    assert report.dimensions["artifact_integrity"].status == "BLOCKED"
    assert "INVALID_STOCK_OHLC" in _codes(report)


@pytest.mark.parametrize("mutation", ["missing-symbol", "duplicate-symbol", "missing-field"])
def test_session_reports_must_exactly_cover_manifest_symbols(tmp_path, mutation):
    quality = _complete_session_quality(("AAA", "BBB"))
    reports = quality["session_coverage_by_symbol"]
    if mutation == "missing-symbol":
        quality["session_coverage_by_symbol"] = reports[:1]
    elif mutation == "duplicate-symbol":
        quality["session_coverage_by_symbol"] = [reports[0], dict(reports[0]), reports[1]]
    else:
        reports[0].pop("session_status")
    store, manifest = _publish(tmp_path, symbols=("AAA", "BBB"), data_quality=quality)
    report = assess_research_quality(manifest.dataset_id, store)
    assert report.dimensions["artifact_integrity"].status == "UNVERIFIED"
    assert "SESSION_COVERAGE_IDENTITY_INCOMPLETE" in _codes(report) or "SESSION_COVERAGE_FIELDS_INCOMPLETE" in _codes(report)


def test_session_gaps_are_reported_from_existing_audit(tmp_path):
    quality = _complete_session_quality()
    quality["session_coverage_by_symbol"][0].update({
        "missing_sessions": ["2024-01-03"],
        "missing_session_count": 1,
        "observed_sessions_within_observed_range": 2,
        "session_coverage": 2 / 3,
        "session_status": "MISSING_SESSIONS",
    })
    store, manifest = _publish(tmp_path, data_quality=quality)
    report = assess_research_quality(manifest.dataset_id, store)
    assert report.dimensions["artifact_integrity"].status == "UNVERIFIED"
    assert "SESSION_COVERAGE_GAPS" in _codes(report)


def test_malformed_quality_metadata_is_a_finding_not_an_exception(tmp_path):
    quality = {
        "missing_ohlcv_rows": "not-a-count",
        "session_coverage_by_symbol": [{
            "instrument_id": "AAA",
            "missing_session_count": "bad",
            "extra_session_count": 0,
            "duplicate_session_count": 0,
            "session_status": "COMPLETE",
        }],
    }
    store, manifest = _publish(tmp_path, data_quality=quality)
    report = assess_research_quality(manifest.dataset_id, store)
    assert report.dimensions["artifact_integrity"].status == "UNVERIFIED"
    assert "QUALITY_METADATA_INVALID" in _codes(report)


def test_abnormal_price_jump_is_warning_only(tmp_path):
    rows = _rows()
    rows[1]["open"] = rows[0]["close"] * 10
    rows[1]["high"] = rows[1]["open"] + 1
    rows[1]["low"] = rows[1]["open"] - 1
    rows[1]["close"] = rows[1]["open"]
    store, manifest = _publish(tmp_path, rows=rows, data_quality={**_complete_session_quality(), "session_coverage_by_symbol": []})
    before = store.read_rows(manifest.dataset_id)
    report = assess_research_quality(manifest.dataset_id, store)
    finding = next(item for item in report.findings if item.code == "ABNORMAL_PRICE_JUMP")
    assert finding.severity == "WARNING"
    assert "auto-adjust" in " ".join(finding.unverified_facts).lower()
    assert store.read_rows(manifest.dataset_id) == before


def test_current_constituent_lookback_reports_survivorship_risk(tmp_path):
    store, manifest = _publish(
        tmp_path,
        universe_definition={"mode": "current_index_constituents", "index": "FIXTURE"},
    )
    report = assess_research_quality(manifest.dataset_id, store)
    assert report.dimensions["universe_validity"].status == "UNVERIFIED"
    assert "RETROSPECTIVE_CONSTITUENT_BIAS_RISK" in _codes(report)


def test_renamed_source_still_has_no_independent_evidence(tmp_path):
    store, manifest = _publish(tmp_path, price_basis="raw_attested", source="independent-yahoo")
    report = assess_research_quality(manifest.dataset_id, store)
    assert report.dimensions["price_attestation"].status == "UNVERIFIED"
    assert report.research_readiness != "RESEARCH_DATA_VALIDATED"


def test_feature_panel_inherits_parent_limitations(tmp_path):
    store, manifest = _publish(tmp_path)
    compiled = compile_factor_spec(FactorSpec.model_validate({
        "schema_version": "1.0", "timeframe": "1d",
        "nodes": [{"id": "ret", "operator": "pct_change", "inputs": ["close"], "params": {"period": 1}}],
        "outputs": ["ret"],
    }))
    feature = compute_stock_factors(manifest, compiled, store=store)
    report = assess_research_quality(feature.dataset_id, store)
    assert report.research_readiness == "RESEARCH_EXPLORATORY"
    assert report.dimensions["price_attestation"].status == "UNVERIFIED"
    assert report.dimensions["universe_validity"].status == "UNVERIFIED"
    assert any(item.evidence.get("inherited_from") == manifest.dataset_id for item in report.findings)


def test_blocked_parent_blocks_feature_child(tmp_path):
    store, manifest = _publish(tmp_path)
    feature = _publish_feature(store, [manifest])
    partition = store.datasets / manifest.dataset_id.removeprefix("sha256:") / manifest.partition_manifest[0].logical_path
    with partition.open("ab") as stream:
        stream.write(b"corruption")
    report = assess_research_quality(feature.dataset_id, store)
    assert report.dimensions["artifact_integrity"].status == "BLOCKED"
    assert report.research_readiness == "RESEARCH_BLOCKED"
    assert "ARTIFACT_INTEGRITY_FAILED" in _codes(report)


def test_multiple_parents_retain_all_risks(tmp_path):
    store, first = _publish(tmp_path)
    request = StockDatasetRequest(source="yahoo", symbols=["BBB"], start=START, end=END)
    second = store.publish_stock(
        request,
        _rows(symbols=("BBB",)),
        calendar_version="fixture",
        source_version="fixture-v1",
        data_quality={**_complete_session_quality(("BBB",)), "provider": "second-source"},
    )
    feature = _publish_feature(store, [first, second], rows=_feature_rows(first))
    report = assess_research_quality(feature.dataset_id, store)
    inherited = {item.evidence.get("inherited_from") for item in report.findings}
    assert first.dataset_id in inherited
    assert second.dataset_id in inherited


def test_missing_parent_fails_closed(tmp_path):
    store, parent = _publish(tmp_path)
    feature = _publish_feature(store, [parent])
    connection = sqlite3.connect(store.catalog_path)
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute("DELETE FROM alpha_dataset_parents WHERE dataset_id=?", (feature.dataset_id,))
    connection.execute("DELETE FROM alpha_datasets WHERE dataset_id=?", (parent.dataset_id,))
    connection.commit()
    connection.close()
    report = assess_research_quality(feature.dataset_id, store)
    assert report.dimensions["lineage_integrity"].status == "BLOCKED"
    assert report.research_readiness == "RESEARCH_BLOCKED"
    assert "PARENT_DATASET_UNREADABLE" in _codes(report)


def test_duplicate_parent_declarations_fail_closed(tmp_path):
    store, parent = _publish(tmp_path)
    feature = _publish_feature(store, [parent, parent])
    report = assess_research_quality(feature.dataset_id, store)
    assert report.dimensions["lineage_integrity"].status == "BLOCKED"
    assert report.research_readiness == "RESEARCH_BLOCKED"
    assert "DUPLICATE_PARENT_DATASET" in _codes(report)


def test_cyclic_parents_terminate_fail_closed(tmp_path, monkeypatch):
    store, parent = _publish(tmp_path)
    feature = _publish_feature(store, [parent])
    original = store.load_manifest

    def load(identifier):
        manifest = original(identifier)
        if identifier == parent.dataset_id:
            return manifest.model_copy(update={"parent_dataset_ids": [feature.dataset_id]})
        return manifest

    monkeypatch.setattr(store, "load_manifest", load)
    report = assess_research_quality(feature.dataset_id, store)
    assert report.dimensions["lineage_integrity"].status == "BLOCKED"
    assert "PARENT_DATASET_CYCLE" in _codes(report)


def test_feature_future_availability_is_blocking(tmp_path):
    store, parent = _publish(tmp_path)
    at = datetime(2024, 1, 4, 21, 5, tzinfo=timezone.utc)
    feature = store.publish_feature_panel(
        _feature_rows(parent, available_at=at + timedelta(minutes=1)),
        parent_dataset_id=parent.dataset_id,
        factor_spec_hash="sha256:" + "a" * 64,
        instrument_ids=["AAA"],
        start_at=START,
        end_at=END,
        calendar_version="fixture",
        price_basis=parent.price_basis,
    )
    report = assess_research_quality(feature.dataset_id, store)
    assert report.dimensions["feature_integrity"].status == "BLOCKED"
    assert "FEATURE_AVAILABLE_AFTER_DECISION" in _codes(report)


def test_prediction_panel_cannot_upgrade_parent_quality(tmp_path):
    store, parent = _publish(tmp_path)
    feature = _publish_feature(store, [parent])
    prediction = store.publish_prediction_panel(
        [{"decision_at": datetime(2024, 1, 4, 21, 5, tzinfo=timezone.utc), "instrument_id": "AAA",
          "prediction_value": 0.01, "model_id": "sha256:" + "b" * 64,
          "model_set_id": "sha256:" + "c" * 64, "fold": "validation"}],
        parent_feature_set_id=feature.dataset_id,
        model_id="sha256:" + "b" * 64,
        model_set_id="sha256:" + "c" * 64,
        start_at=START,
        end_at=END,
        price_basis=parent.price_basis,
    )
    report = assess_research_quality(prediction.dataset_id, store)
    assert report.research_readiness == "RESEARCH_EXPLORATORY"
    assert report.dimensions["price_attestation"].status == "UNVERIFIED"
    assert any(
        item.evidence.get("inheritance_chain") == [parent.dataset_id, feature.dataset_id]
        for item in report.findings
    )


@pytest.mark.parametrize(("limit_name", "limit_value", "code"), [
    ("max_scan_rows", 4, "RESOURCE_LIMIT_EXCEEDED"),
    ("max_nodes", 2, "RESOURCE_LIMIT_EXCEEDED"),
])
def test_scan_budget_is_shared_across_lineage(tmp_path, limit_name, limit_value, code):
    store = DatasetStore(tmp_path / "alpha")
    first_request = StockDatasetRequest(source="yahoo", symbols=["AAA"], start=START, end=END)
    first = store.publish_stock(
        first_request,
        _rows(symbols=("AAA",), days=2),
        calendar_version="fixture",
        source_version="fixture-v1",
        data_quality=_complete_session_quality(("AAA",)),
    )
    second_request = StockDatasetRequest(source="yahoo", symbols=["BBB"], start=START, end=END)
    second = store.publish_stock(
        second_request,
        _rows(symbols=("BBB",), days=2),
        calendar_version="fixture",
        source_version="fixture-v1",
        data_quality=_complete_session_quality(("BBB",)),
    )
    feature = _publish_feature(store, [first, second], rows=_feature_rows(first))
    limits = ResearchQualityLimits(**{limit_name: limit_value})
    with pytest.raises(AlphaError) as error:
        assess_research_quality(feature.dataset_id, store, limits=limits)
    assert error.value.code == AlphaErrorCode(code)


def test_scan_resource_limit_fails_without_partial_report(tmp_path):
    store, manifest = _publish(tmp_path)
    with pytest.raises(AlphaError) as error:
        assess_research_quality(
            manifest.dataset_id,
            store,
            limits=ResearchQualityLimits(max_scan_rows=2),
        )
    assert error.value.code == AlphaErrorCode.RESOURCE_LIMIT_EXCEEDED


def test_publication_still_rejects_invalid_bar_contract():
    row = _rows()[0]
    row["available_at"] = row["end_at"] - timedelta(seconds=1)
    with pytest.raises(Exception, match="end_at <= available_at"):
        StockBarRow.model_validate(row)
