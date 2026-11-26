"""Lossless transport for large Baseline Comparison reports."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from ...artifacts import MAX_RESPONSE_ROW_BYTES
from ...compiler import digest
from ...pagination import fits_response_row
from .baseline_comparison import BaselineComparisonReport

SECTION = "baseline_comparison"
ARTIFACT_VERSION = "baseline-comparison-artifact-v1"
ENCODING = "canonical-json-v1"
_MAX_CHUNK_BYTES = 1_500_000


class BaselineEvidenceError(ValueError):
    """Invalid or ambiguous persisted Baseline evidence."""


def _encoded_size(value: Any) -> int:
    return len(json.dumps(value, allow_nan=False).encode())


def _canonical_report_bytes(report: dict[str, Any]) -> bytes:
    text = json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return text.encode("utf-8")


def _byte_chunks(data: bytes) -> list[bytes]:
    chunks: list[bytes] = []
    start = 0
    while start < len(data):
        end = min(len(data), start + _MAX_CHUNK_BYTES)
        while end < len(data):
            try:
                data[start:end].decode("utf-8")
                break
            except UnicodeDecodeError:
                end -= 1
        if end <= start:
            raise ValueError("Baseline report cannot be split on a UTF-8 boundary")
        chunks.append(data[start:end])
        start = end
    return chunks


def _manifest(report: dict[str, Any], job_id: str, payload: bytes, chunk_count: int) -> dict[str, Any]:
    return {
        "schema_version": ARTIFACT_VERSION,
        "artifact_version": ARTIFACT_VERSION,
        "record_type": "manifest",
        "job_id": job_id,
        "report_hash": report["report_hash"],
        "encoding": ENCODING,
        "path": "$",
        "payload_bytes": len(payload),
        "payload_hash": "sha256:" + hashlib.sha256(payload).hexdigest(),
        "chunk_count": chunk_count,
    }


def paginated_baseline_records(report: dict[str, Any], *, job_id: str) -> list[dict[str, Any]]:
    """Return a deterministic manifest followed by contiguous canonical-JSON byte chunks."""
    BaselineComparisonReport.model_validate(report)
    payload = _canonical_report_bytes(report)
    chunks = _byte_chunks(payload)
    manifest = _manifest(report, job_id, payload, len(chunks))
    records = [manifest]
    offset = 0
    for ordinal, chunk in enumerate(chunks):
        text = chunk.decode("utf-8")
        record = {
            "schema_version": ARTIFACT_VERSION,
            "artifact_version": ARTIFACT_VERSION,
            "record_type": "chunk",
            "job_id": job_id,
            "report_hash": report["report_hash"],
            "path": "$",
            "ordinal": ordinal,
            "chunk_count": len(chunks),
            "byte_start": offset,
            "byte_end": offset + len(chunk),
            "payload": text,
        }
        size = _encoded_size(record)
        if size > MAX_RESPONSE_ROW_BYTES or not fits_response_row(record, size):
            raise ValueError("RESOURCE_LIMIT_EXCEEDED: a Baseline evidence chunk exceeds the MCP row budget")
        records.append(record)
        offset += len(chunk)
    return records


def paginated_reference(records: list[dict[str, Any]]) -> dict[str, Any]:
    manifest = records[0]
    return {
        "schema_version": ARTIFACT_VERSION,
        "artifact_version": ARTIFACT_VERSION,
        "record_type": "paginated-reference",
        "section": SECTION,
        "job_id": manifest["job_id"],
        "report_hash": manifest["report_hash"],
        "payload_hash": manifest["payload_hash"],
        "payload_bytes": manifest["payload_bytes"],
        "chunk_count": manifest["chunk_count"],
    }


def training_metrics_with_report(metrics: dict[str, Any], report: dict[str, Any], *, job_id: str):
    """Keep small reports inline; move only over-budget reports into the new section."""
    inline = {**metrics, "baseline_comparison": report}
    size = _encoded_size(inline)
    if size <= MAX_RESPONSE_ROW_BYTES and fits_response_row(inline, size):
        return inline, None
    records = paginated_baseline_records(report, job_id=job_id)
    referenced = {**metrics, "baseline_comparison": paginated_reference(records)}
    size = _encoded_size(referenced)
    if size > MAX_RESPONSE_ROW_BYTES or not fits_response_row(referenced, size):
        raise ValueError("RESOURCE_LIMIT_EXCEEDED: training_metrics reference exceeds the MCP row budget")
    return referenced, records


class BaselineEvidenceLoader:
    """Shared compatibility reader for historical inline and paginated Baseline evidence."""

    def __init__(self, job_store):
        if job_store is None:
            raise BaselineEvidenceError("JobStore is required for Baseline evidence")
        self.job_store = job_store

    def _all_rows(self, job_id: str, section: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        cursor = None
        total = None
        while True:
            page = self.job_store.get_result_unexposed(job_id, section, 1000, cursor)
            if total is None:
                total = page["total_count"]
            elif total != page["total_count"]:
                raise BaselineEvidenceError("Baseline pagination identity changed")
            rows.extend(page["rows"])
            cursor = page.get("next_cursor")
            if cursor is None:
                break
        if total != len(rows):
            raise BaselineEvidenceError("Baseline evidence pagination is incomplete")
        return rows

    def load(self, job_id: str, result: dict[str, Any]) -> tuple[dict[str, Any], BaselineComparisonReport | None]:
        metrics_rows = self._all_rows(job_id, "training_metrics")
        if len(metrics_rows) != 1:
            raise BaselineEvidenceError("training_metrics must contain exactly one record")
        metrics = metrics_rows[0]
        baseline = metrics.get("baseline_comparison")
        separate_section = result.get(SECTION) is not None
        if baseline is None:
            if separate_section:
                raise BaselineEvidenceError("Baseline section exists without a training_metrics reference")
            return metrics, None
        if not isinstance(baseline, dict):
            raise BaselineEvidenceError("Baseline reference or inline report must be an object")
        paginated = baseline.get("record_type") == "paginated-reference"
        if not paginated:
            if separate_section:
                raise BaselineEvidenceError("Baseline evidence has both inline and paginated sources")
            try:
                return metrics, BaselineComparisonReport.model_validate(baseline)
            except Exception as error:  # noqa: BLE001 - malformed evidence remains invalid evidence
                raise BaselineEvidenceError("Inline Baseline report is invalid") from error
        if baseline.get("artifact_version") != ARTIFACT_VERSION or baseline.get("schema_version") != ARTIFACT_VERSION:
            raise BaselineEvidenceError("Unsupported Baseline evidence version")
        if baseline.get("section") != SECTION:
            raise BaselineEvidenceError("Baseline reference names the wrong section")
        if baseline.get("job_id") != job_id:
            raise BaselineEvidenceError("Baseline reference belongs to another Job")
        records = self._all_rows(job_id, SECTION)
        return metrics, self._reconstruct(job_id, baseline, records)

    def _reconstruct(self, job_id: str, reference: dict[str, Any], records: list[dict[str, Any]]) -> BaselineComparisonReport:
        if not records:
            raise BaselineEvidenceError("Baseline evidence manifest is missing")
        manifest = records[0]
        if manifest.get("record_type") != "manifest":
            raise BaselineEvidenceError("Baseline evidence manifest is not first")
        if manifest.get("artifact_version") != ARTIFACT_VERSION or manifest.get("schema_version") != ARTIFACT_VERSION:
            raise BaselineEvidenceError("Unsupported Baseline evidence version")
        if manifest.get("encoding") != ENCODING or manifest.get("path") != "$":
            raise BaselineEvidenceError("Unsupported Baseline evidence encoding")
        if manifest.get("job_id") != job_id:
            raise BaselineEvidenceError("Baseline evidence belongs to another Job")
        for field in ("report_hash", "payload_hash", "payload_bytes", "chunk_count"):
            if manifest.get(field) != reference.get(field):
                raise BaselineEvidenceError(f"Baseline reference mismatch: {field}")
        chunk_count = manifest["chunk_count"]
        if type(chunk_count) is not int or chunk_count < 1 or len(records) != chunk_count + 1:
            raise BaselineEvidenceError("Baseline evidence chunk count is inconsistent")
        chunks = records[1:]
        payload = bytearray()
        for expected, record in enumerate(chunks):
            if record.get("record_type") != "chunk":
                raise BaselineEvidenceError("Baseline evidence contains a non-chunk record")
            if record.get("artifact_version") != ARTIFACT_VERSION or record.get("schema_version") != ARTIFACT_VERSION:
                raise BaselineEvidenceError("Unsupported Baseline chunk version")
            if record.get("job_id") != job_id or record.get("report_hash") != manifest["report_hash"]:
                raise BaselineEvidenceError("Baseline chunk identity mismatch")
            if record.get("path") != manifest["path"]:
                raise BaselineEvidenceError("Baseline chunk path mismatch")
            if record.get("ordinal") != expected or record.get("chunk_count") != chunk_count:
                raise BaselineEvidenceError("Baseline chunk order or count mismatch")
            if record.get("byte_start") != len(payload):
                raise BaselineEvidenceError("Baseline chunk byte range is not contiguous")
            text = record.get("payload")
            if not isinstance(text, str):
                raise BaselineEvidenceError("Baseline chunk payload is missing")
            encoded = text.encode("utf-8")
            if record.get("byte_end") != len(payload) + len(encoded):
                raise BaselineEvidenceError("Baseline chunk byte range is inconsistent")
            payload.extend(encoded)
        if len(payload) != manifest["payload_bytes"]:
            raise BaselineEvidenceError("Baseline evidence payload length mismatch")
        actual = "sha256:" + hashlib.sha256(payload).hexdigest()
        if actual != manifest["payload_hash"]:
            raise BaselineEvidenceError("Baseline evidence payload hash mismatch")
        try:
            report = json.loads(payload.decode("utf-8"))
            parsed = BaselineComparisonReport.model_validate(report)
        except Exception as error:  # noqa: BLE001 - malformed evidence remains invalid evidence
            raise BaselineEvidenceError("Baseline evidence reconstruction failed") from error
        if parsed.report_hash != manifest["report_hash"]:
            raise BaselineEvidenceError("Baseline report hash mismatch")
        body = report.copy()
        body.pop("report_hash", None)
        if digest(body) != parsed.report_hash:
            raise BaselineEvidenceError("Baseline report hash does not match its payload")
        return parsed
