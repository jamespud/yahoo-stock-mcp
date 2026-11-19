"""Content-addressed dataset publication plus a transactional SQLite catalog."""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import tempfile

import numpy as np
import pyarrow as pa
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Sequence
from uuid import uuid4

from ...compiler import digest
from ..contracts import DatasetManifest, DatasetPartition, Hash, StockDatasetRequest
from ..errors import AlphaError, AlphaErrorCode
from datetime import datetime, timezone
from .manifests import canonical_json, content_hash, dataset_identity
from .partitions import FORMAT, StockBarRow, normalise_stock_rows, read_partition, stock_rows_by_instrument, write_feature_partition, write_option_partition, write_prediction_partition, write_stock_partition
from .quality import DataQualityReport

_DATASET_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_DDL = '''
CREATE TABLE IF NOT EXISTS alpha_datasets (
  dataset_id TEXT PRIMARY KEY, schema_version TEXT NOT NULL, dataset_kind TEXT NOT NULL,
  source TEXT NOT NULL, source_version TEXT NOT NULL, calendar_version TEXT NOT NULL,
  start_at TEXT NOT NULL, end_at TEXT NOT NULL, captured_at TEXT NOT NULL,
  availability_policy TEXT NOT NULL, price_basis TEXT NOT NULL, content_hash TEXT NOT NULL,
  manifest_path TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS alpha_dataset_partitions (
  dataset_id TEXT NOT NULL REFERENCES alpha_datasets(dataset_id), ordinal INTEGER NOT NULL,
  logical_path TEXT NOT NULL, format TEXT NOT NULL, content_hash TEXT NOT NULL,
  schema_hash TEXT NOT NULL, row_count INTEGER NOT NULL, byte_count INTEGER NOT NULL,
  PRIMARY KEY(dataset_id, ordinal), UNIQUE(dataset_id, logical_path));
CREATE TABLE IF NOT EXISTS alpha_dataset_parents (
  dataset_id TEXT NOT NULL REFERENCES alpha_datasets(dataset_id),
  parent_dataset_id TEXT NOT NULL REFERENCES alpha_datasets(dataset_id),
  PRIMARY KEY(dataset_id, parent_dataset_id));
'''


def _parse_time(value):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def _within(row, start_at, end_at):
    decision = _parse_time(row.get("decision_at") or row.get("end_at") or row.get("available_at"))
    return (start_at is None or decision >= start_at) and (end_at is None or decision < end_at)


class DatasetStore:
    def __init__(self, root: str | Path, *, defer_registration: bool = False):
        self.root = Path(root)
        self.defer_registration = defer_registration
        self.datasets = self.root / "datasets"
        self.staging = self.root / "staging"
        self.catalog_path = self.root / "catalog.sqlite3"
        self.datasets.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.staging.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connect() as connection:
            connection.executescript(_DDL)

    def _connect(self):
        connection = sqlite3.connect(self.catalog_path, timeout=30)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def publish_stock(
        self,
        request: StockDatasetRequest,
        rows: Iterable[StockBarRow | dict],
        *,
        calendar_version: str,
        source_version: str,
        data_quality: dict,
        captured_at: datetime | None = None,
        checkpoint: Callable[[float], None] | None = None,
    ) -> DatasetManifest:
        normalized = normalise_stock_rows(rows)
        if not normalized:
            raise AlphaError(AlphaErrorCode.DATA_UNAVAILABLE, "No stock rows to publish")
        if {row.instrument_id for row in normalized} != set(request.symbols):
            raise AlphaError(AlphaErrorCode.DATASET_MISMATCH, "Rows do not cover the requested explicit universe")
        if any(row.source != request.source or row.source_version != source_version for row in normalized):
            raise AlphaError(AlphaErrorCode.DATASET_MISMATCH, "Mixed source/version rows")
        stage = Path(tempfile.mkdtemp(prefix="stage-", dir=self.staging))
        try:
            partitions: list[DatasetPartition] = []
            grouped = stock_rows_by_instrument(normalized)
            for ordinal, (symbol, symbol_rows) in enumerate(grouped.items()):
                if checkpoint:
                    checkpoint(ordinal / max(1, len(grouped)))
                logical = f"stock_bars/instrument_id={symbol}/part-00000.parquet"
                path = stage / logical
                file_hash, schema_hash, row_count, byte_count = write_stock_partition(path, symbol_rows)
                partitions.append(DatasetPartition(
                    ordinal=ordinal, logical_path=logical, format=FORMAT, content_hash=file_hash,
                    schema_hash=schema_hash, row_count=row_count, byte_count=byte_count,
                ))
            capture = (captured_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
            body = {
                "schema_version": "1.0",
                "dataset_kind": "stock_bars",
                "source": request.source,
                "source_version": source_version,
                "calendar_version": calendar_version,
                "universe_definition": {"mode": "explicit", **request.universe_definition, "symbols": request.symbols},
                "instrument_ids": request.symbols,
                "start_at": request.start.astimezone(timezone.utc).isoformat(),
                "end_at": request.end.astimezone(timezone.utc).isoformat(),
                "availability_policy": request.availability_policy,
                "price_basis": request.price_basis,
                "data_quality": data_quality,
                "parent_dataset_ids": [],
            }
            identity = dataset_identity(body, partitions)
            manifest = DatasetManifest.model_validate({
                **body,
                "dataset_id": identity,
                "captured_at": capture,
                "partition_manifest": [partition.model_dump(mode="json") for partition in partitions],
                "content_hash": content_hash(partitions),
            })
            manifest_path = stage / "manifest.json"
            manifest_path.write_bytes(canonical_json(manifest.model_dump(mode="json")))
            with manifest_path.open("rb") as stream:
                os.fsync(stream.fileno())
            if checkpoint:
                checkpoint(1.0)
            destination = self.datasets / identity.removeprefix("sha256:")
            try:
                os.rename(stage, destination)
            except OSError:
                if not destination.exists():
                    raise
                existing = DatasetManifest.model_validate_json((destination / "manifest.json").read_text())
                if existing.content_hash != manifest.content_hash:
                    raise ValueError("Dataset identity collision")
            if not self.defer_registration:
                self._register(manifest)
            self._fsync_directory(self.datasets)
            return manifest
        finally:
            if stage.exists():
                shutil.rmtree(stage, ignore_errors=True)

    def publish_feature_panel(
        self,
        rows: Iterable[dict],
        *,
        parent_dataset_id: str,
        parent_dataset_ids: list[str] | None = None,
        factor_spec_hash: str,
        instrument_ids: list[str],
        start_at: datetime,
        end_at: datetime,
        calendar_version: str,
        price_basis: str,
        checkpoint: Callable[[float], None] | None = None,
    ) -> DatasetManifest:
        grouped: dict[str, list[dict]] = {}
        total = 0
        table = rows if isinstance(rows, pa.Table) else None
        if table is not None:
            factor_names = table["factor_name"].to_pylist()
            total = table.num_rows
            for factor_name in sorted(set(factor_names)):
                grouped[factor_name] = np.flatnonzero(np.asarray(factor_names) == factor_name).tolist()
        else:
            for row in rows:
                grouped.setdefault(row["factor_name"], []).append(row)
                total += 1
        if not grouped:
            raise AlphaError(AlphaErrorCode.DATA_UNAVAILABLE, "No factor rows to publish")
        stage = Path(tempfile.mkdtemp(prefix="stage-", dir=self.staging))
        try:
            partitions: list[DatasetPartition] = []
            for ordinal, (factor_name, factor_rows) in enumerate(sorted(grouped.items())):
                if checkpoint:
                    checkpoint(ordinal / max(1, len(grouped)))
                safe = re.sub(r"[^A-Za-z0-9_.-]", "_", factor_name)
                logical = f"feature_panel/factor_name={safe}/part-00000.parquet"
                payload = table.take(factor_rows) if table is not None else factor_rows
                file_hash, schema_hash, row_count, byte_count = write_feature_partition(stage / logical, payload)
                partitions.append(DatasetPartition(ordinal=ordinal, logical_path=logical, format=FORMAT, content_hash=file_hash, schema_hash=schema_hash, row_count=row_count, byte_count=byte_count))
            capture = datetime.now(timezone.utc)
            body = {
                "schema_version": "1.0", "dataset_kind": "feature_panel", "source": "factor_engine",
                "source_version": factor_spec_hash, "calendar_version": calendar_version,
                "universe_definition": {"mode": "factor_panel", "factor_spec_hash": factor_spec_hash},
                "instrument_ids": instrument_ids, "start_at": start_at.astimezone(timezone.utc).isoformat(),
                "end_at": end_at.astimezone(timezone.utc).isoformat(), "availability_policy": "decision_time_known_fields_only",
                "price_basis": price_basis, "data_quality": {"row_count": total, "factor_count": len(grouped)},
                "parent_dataset_ids": parent_dataset_ids or [parent_dataset_id],
            }
            identity = dataset_identity(body, partitions)
            manifest = DatasetManifest.model_validate({**body, "dataset_id": identity, "captured_at": capture, "partition_manifest": [p.model_dump(mode="json") for p in partitions], "content_hash": content_hash(partitions)})
            manifest_path = stage / "manifest.json"
            manifest_path.write_bytes(canonical_json(manifest.model_dump(mode="json")))
            with manifest_path.open("rb") as stream:
                os.fsync(stream.fileno())
            destination = self.datasets / identity.removeprefix("sha256:")
            try:
                os.rename(stage, destination)
            except OSError:
                if not destination.exists():
                    raise
                existing = DatasetManifest.model_validate_json((destination / "manifest.json").read_text())
                if existing.content_hash != manifest.content_hash:
                    raise ValueError("Dataset identity collision")
            if not self.defer_registration:
                self._register(manifest)
            self._fsync_directory(self.datasets)
            return manifest
        finally:
            if stage.exists():
                shutil.rmtree(stage, ignore_errors=True)

    def publish_option_panel(
        self,
        rows,
        *,
        request,
        source_version: str,
        data_quality: dict,
        checkpoint: Callable[[float], None] | None = None,
    ) -> DatasetManifest:
        grouped: dict[str, list[dict]] = {}
        for row in rows:
            grouped.setdefault(row.contract.underlying_id, []).append(row.dataset_row())
        stage = Path(tempfile.mkdtemp(prefix="stage-", dir=self.staging))
        try:
            partitions: list[DatasetPartition] = []
            for ordinal, (underlying, underlying_rows) in enumerate(sorted(grouped.items())):
                if checkpoint:
                    checkpoint(ordinal / max(1, len(grouped)))
                if any(char in underlying for char in "/\\") or underlying in {".", ".."}:
                    raise AlphaError(AlphaErrorCode.DATASET_MISMATCH, "Unsafe option underlying identifier")
                logical = f"option_quotes/underlying_id={underlying}/part-00000.parquet"
                file_hash, schema_hash, row_count, byte_count = write_option_partition(stage / logical, underlying_rows)
                partitions.append(DatasetPartition(ordinal=ordinal, logical_path=logical, format=FORMAT, content_hash=file_hash, schema_hash=schema_hash, row_count=row_count, byte_count=byte_count))
            capture = datetime.now(timezone.utc)
            body = {
                "schema_version": "1.0", "dataset_kind": "option_quotes", "source": request.source,
                "source_version": source_version, "calendar_version": "US-OPTION-EVENT-TIME-V1",
                "universe_definition": {"mode": "explicit", "underlyings": request.underlyings, "quote_policy": request.quote_policy},
                "instrument_ids": sorted(grouped), "start_at": request.start.astimezone(timezone.utc).isoformat(),
                "end_at": request.end.astimezone(timezone.utc).isoformat(), "availability_policy": "observed" if request.quote_policy == "observed_snapshot" else "historical_quote_at",
                "price_basis": request.price_basis, "data_quality": data_quality, "parent_dataset_ids": [],
            }
            identity = dataset_identity(body, partitions)
            manifest = DatasetManifest.model_validate({**body, "dataset_id": identity, "captured_at": capture, "partition_manifest": [p.model_dump(mode="json") for p in partitions], "content_hash": content_hash(partitions)})
            manifest_path = stage / "manifest.json"
            manifest_path.write_bytes(canonical_json(manifest.model_dump(mode="json")))
            with manifest_path.open("rb") as stream:
                os.fsync(stream.fileno())
            destination = self.datasets / identity.removeprefix("sha256:")
            try:
                os.rename(stage, destination)
            except OSError:
                if not destination.exists():
                    raise
                existing = DatasetManifest.model_validate_json((destination / "manifest.json").read_text())
                if existing.content_hash != manifest.content_hash:
                    raise ValueError("Dataset identity collision")
            if not self.defer_registration:
                self._register(manifest)
            self._fsync_directory(self.datasets)
            return manifest
        finally:
            if stage.exists():
                shutil.rmtree(stage, ignore_errors=True)

    def publish_prediction_panel(
        self, rows, *, parent_feature_set_id: str, model_id: str, model_set_id: str,
        start_at: datetime, end_at: datetime, price_basis: str,
    ) -> DatasetManifest:
        if not rows:
            raise AlphaError(AlphaErrorCode.DATA_UNAVAILABLE, "No predictions to publish")
        stage = Path(tempfile.mkdtemp(prefix="stage-", dir=self.staging))
        try:
            logical = "prediction_panel/part-00000.parquet"
            file_hash, schema_hash, row_count, byte_count = write_prediction_partition(stage / logical, rows)
            partition = DatasetPartition(ordinal=0, logical_path=logical, format=FORMAT, content_hash=file_hash, schema_hash=schema_hash, row_count=row_count, byte_count=byte_count)
            partitions = [partition]
            body = {"schema_version": "1.0", "dataset_kind": "prediction_panel", "source": "alpha_model",
                    "source_version": model_id, "calendar_version": "decision-time-v1",
                    "universe_definition": {"mode": "frozen_predictions", "model_set_id": model_set_id},
                    "instrument_ids": sorted({row["instrument_id"] for row in rows}),
                    "start_at": start_at.astimezone(timezone.utc).isoformat(), "end_at": end_at.astimezone(timezone.utc).isoformat(),
                    "availability_policy": "decision_time_known_fields_only", "price_basis": price_basis,
                    "data_quality": {"row_count": len(rows), "model_id": model_id},
                    "parent_dataset_ids": [parent_feature_set_id]}
            identity = dataset_identity(body, partitions)
            manifest = DatasetManifest.model_validate({**body, "dataset_id": identity, "captured_at": datetime.now(timezone.utc),
                "partition_manifest": [partition.model_dump(mode="json")], "content_hash": content_hash(partitions)})
            (stage / "manifest.json").write_bytes(canonical_json(manifest.model_dump(mode="json")))
            destination = self.datasets / identity.removeprefix("sha256:")
            try:
                os.rename(stage, destination)
            except OSError:
                if not destination.exists():
                    raise
            if not self.defer_registration:
                self._register(manifest)
            self._fsync_directory(self.datasets)
            return manifest
        finally:
            if stage.exists():
                shutil.rmtree(stage, ignore_errors=True)

    def register(self, manifest: DatasetManifest, checkpoint=None) -> None:
        self._register(manifest, checkpoint=checkpoint)

    def _register(self, manifest: DatasetManifest, checkpoint=None) -> None:
        if checkpoint:
            checkpoint()
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO alpha_datasets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                (
                    manifest.dataset_id, manifest.schema_version, manifest.dataset_kind, manifest.source,
                    manifest.source_version, manifest.calendar_version, manifest.start_at.isoformat(),
                    manifest.end_at.isoformat(), manifest.captured_at.isoformat(), manifest.availability_policy,
                    manifest.price_basis, manifest.content_hash, f"datasets/{manifest.dataset_id.removeprefix('sha256:')}/manifest.json",
                ),
            )
            connection.executemany(
                "INSERT OR IGNORE INTO alpha_dataset_partitions VALUES (?,?,?,?,?,?,?,?)",
                [(manifest.dataset_id, p.ordinal, p.logical_path, p.format, p.content_hash, p.schema_hash, p.row_count, p.byte_count)
                 for p in manifest.partition_manifest],
            )
            connection.executemany(
                "INSERT OR IGNORE INTO alpha_dataset_parents VALUES (?,?)",
                [(manifest.dataset_id, parent) for parent in manifest.parent_dataset_ids],
            )
            connection.commit()
        if checkpoint:
            checkpoint()

    def load_manifest(self, dataset_id: str) -> DatasetManifest:
        if not isinstance(dataset_id, str) or not _DATASET_ID.fullmatch(dataset_id):
            raise ValueError("Invalid dataset ID")
        with self._connect() as connection:
            row = connection.execute("SELECT manifest_path FROM alpha_datasets WHERE dataset_id=?", (dataset_id,)).fetchone()
        if row is None:
            raise KeyError(dataset_id)
        path = self._safe_root_path(row[0])
        manifest = DatasetManifest.model_validate_json(path.read_text())
        expected = dataset_identity(manifest.model_dump(mode="json"), manifest.partition_manifest)
        if manifest.dataset_id != dataset_id or expected != dataset_id:
            raise AlphaError(AlphaErrorCode.DATASET_MISMATCH, "Manifest identity mismatch")
        return manifest

    def load_published_manifest(self, dataset_id: str) -> DatasetManifest:
        """Load a manifest that was atomically published by a deferred job before catalog registration."""
        if not isinstance(dataset_id, str) or not _DATASET_ID.fullmatch(dataset_id):
            raise ValueError("Invalid dataset ID")
        path = self.datasets / dataset_id.removeprefix("sha256:") / "manifest.json"
        manifest = DatasetManifest.model_validate_json(path.read_text())
        expected = dataset_identity(manifest.model_dump(mode="json"), manifest.partition_manifest)
        if manifest.dataset_id != dataset_id or expected != dataset_id:
            raise AlphaError(AlphaErrorCode.DATASET_MISMATCH, "Published manifest identity mismatch")
        return manifest

    def validate_dataset(self, dataset_id: str) -> DataQualityReport:
        issues: list[dict] = []
        row_count = partition_count = 0
        try:
            manifest = self.load_manifest(dataset_id)
            root = self.datasets / dataset_id.removeprefix("sha256:")
            expected_identity = dataset_identity(manifest.model_dump(mode="json"), manifest.partition_manifest)
            if expected_identity != dataset_id:
                issues.append({"code": "DATASET_MISMATCH", "path": "manifest.json", "message": "dataset identity mismatch"})
            partition_count = len(manifest.partition_manifest)
            for partition in manifest.partition_manifest:
                path = root / partition.logical_path
                if not path.is_file():
                    issues.append({"code": "DATASET_MISMATCH", "path": partition.logical_path, "message": "partition missing"})
                    continue
                from .partitions import file_sha256
                if path.stat().st_size != partition.byte_count or file_sha256(path) != partition.content_hash:
                    issues.append({"code": "DATASET_MISMATCH", "path": partition.logical_path, "message": "partition hash/byte mismatch"})
                row_count += partition.row_count
            if content_hash(manifest.partition_manifest) != manifest.content_hash:
                issues.append({"code": "DATASET_MISMATCH", "path": "manifest.json", "message": "content hash mismatch"})
        except Exception as error:
            issues.append({"code": "DATASET_MISMATCH", "path": "manifest.json", "message": str(error)})
        return DataQualityReport(
            dataset_id=dataset_id, valid=not issues, row_count=row_count, partition_count=partition_count,
            coverage=1.0 if not issues else 0.0, issues=issues,
        )

    def read_rows(self, dataset_id: str, columns: Sequence[str] | None = None, start_at=None, end_at=None) -> list[dict]:
        report = self.validate_dataset(dataset_id)
        if not report.valid:
            raise AlphaError(AlphaErrorCode.DATASET_MISMATCH, "Dataset integrity validation failed", {"issues": report.issues})
        manifest = self.load_manifest(dataset_id)
        root = self.datasets / dataset_id.removeprefix("sha256:")
        wanted = list(columns) if columns is not None else None
        rows: list[dict] = []
        bounded = False
        for partition in manifest.partition_manifest:
            partition_rows = read_partition(root / partition.logical_path, wanted)
            if start_at is not None or end_at is not None:
                bounded = True
                start_at = _parse_time(start_at) if start_at is not None else None
                end_at = _parse_time(end_at) if end_at is not None else None
                partition_rows = [row for row in partition_rows if _within(row, start_at, end_at)]
            rows.extend(partition_rows)
        if bounded and not rows:
            return []
        return rows

    def _safe_root_path(self, value: str) -> Path:
        if not isinstance(value, str) or value.startswith("/") or ".." in Path(value).parts or "\\" in value:
            raise ValueError("Unsafe catalog path")
        candidate = (self.root / value).resolve()
        root = self.root.resolve()
        if root not in candidate.parents:
            raise ValueError("Catalog path escapes dataset root")
        return candidate

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
