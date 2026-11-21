"""Immutable, bounded Parquet objects; SQLite manifests own publication.

Rows retain canonical JSON for lossless heterogeneous domain records. Common
index columns permit offline Parquet filtering without flattening account data.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

import pyarrow as pa
import pyarrow.parquet as pq

from .pagination import fits_response_row

SCHEMA = pa.schema([("ordinal", pa.int64()), ("time", pa.string()), ("symbol", pa.string()),
                    ("kind", pa.string()), ("payload", pa.large_string())])
MAX_BYTES = 268435456
MAX_ROWS = 1000000
MAX_RESPONSE_ROW_BYTES = 2500000


def content_hash(stream):
    result = hashlib.sha256()
    stream.seek(0)
    while chunk := stream.read(1048576):
        result.update(chunk)
    stream.seek(0)
    return "sha256:" + result.hexdigest()


class ArtifactStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def resolve(self, reference: dict) -> Path:
        value = reference.get("content_hash", "")
        if not isinstance(value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
            raise ValueError("Invalid artifact identity")
        return self.root / (value[7:] + ".parquet")

    def write_rows(self, rows, *, checkpoint=None, enforce_mcp_budget=False) -> dict:
        fd, name = tempfile.mkstemp(suffix=".tmp", dir=self.root)
        os.close(fd)
        count = total = 0
        try:
            with pq.ParquetWriter(name, SCHEMA, compression="zstd") as writer:
                batch = []
                batch_bytes = 0
                for row in rows:
                    if not isinstance(row, dict):
                        raise ValueError("Artifact rows must be JSON objects")
                    payload = json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
                    size = len(payload.encode())
                    response_size = len(json.dumps(row, allow_nan=False).encode())
                    total += size
                    if response_size > MAX_RESPONSE_ROW_BYTES or total > MAX_BYTES or count >= MAX_ROWS:
                        raise ValueError("RESOURCE_LIMIT_EXCEEDED: Parquet row/byte budget exceeded")
                    if (enforce_mcp_budget
                            and (response_size + 2 > MAX_RESPONSE_ROW_BYTES or not fits_response_row(row, response_size))):
                        raise ValueError("RESOURCE_LIMIT_EXCEEDED: one artifact row exceeds the MCP response budget")
                    batch.append({"ordinal": count, "time": row.get("time") if isinstance(row.get("time"), str) else None,
                                  "symbol": row.get("symbol") if isinstance(row.get("symbol"), str) else None,
                                  "kind": row.get("kind") if isinstance(row.get("kind"), str) else None, "payload": payload})
                    count += 1
                    batch_bytes += size
                    # Bound row groups by bytes as well as count.
                    if len(batch) >= 256 or batch_bytes >= 1048576:
                        if checkpoint:
                            checkpoint()
                        writer.write_table(pa.Table.from_pylist(batch, schema=SCHEMA))
                        batch = []
                        batch_bytes = 0
                if checkpoint:
                    checkpoint()
                if batch:
                    writer.write_table(pa.Table.from_pylist(batch, schema=SCHEMA))
            with open(name, "rb") as stream:
                os.fsync(stream.fileno())
                reference = {"format": "parquet-json-v1", "content_hash": content_hash(stream),
                             "row_count": count, "byte_count": os.fstat(stream.fileno()).st_size}
            destination = self.resolve(reference)
            try:
                os.link(name, destination)
            except FileExistsError:
                with destination.open("rb") as existing:
                    if content_hash(existing) != reference["content_hash"]:
                        raise ValueError("Artifact integrity check failed")
            directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return reference
        finally:
            Path(name).unlink(missing_ok=True)

    def read_rows(self, reference: dict, offset=0, limit=1000, *, byte_limit: int | None = None) -> list[dict]:
        if reference.get("format") != "parquet-json-v1" or type(offset) is not int or type(limit) is not int or offset < 0 or not 1 <= limit <= MAX_ROWS:
            raise ValueError("Invalid artifact range/format")
        with self.resolve(reference).open("rb") as stream:
            if os.fstat(stream.fileno()).st_size != reference["byte_count"] or content_hash(stream) != reference["content_hash"]:
                raise ValueError("Artifact integrity check failed")
            parquet = pq.ParquetFile(stream)
            if parquet.metadata.num_rows != reference["row_count"] or parquet.schema_arrow != SCHEMA:
                raise ValueError("Artifact integrity check failed")
            result, start, used = [], 0, 0
            for group in range(parquet.num_row_groups):
                length = parquet.metadata.row_group(group).num_rows
                if start + length > offset and start < offset + limit:
                    table = parquet.read_row_group(group, columns=["payload"])
                    begin = max(0, offset - start)
                    stop = min(length, offset + limit - start)
                    for value in table.column("payload").slice(begin, stop - begin).to_pylist():
                        parsed = json.loads(value)
                        size = len(json.dumps(parsed, allow_nan=False).encode())
                        if byte_limit is not None and used + size > byte_limit:
                            if not result:
                                raise ValueError("RESOURCE_LIMIT_EXCEEDED: result row exceeds page budget")
                            return result
                        result.append(parsed)
                        used += size
                start += length
                if start >= offset + limit:
                    break
            return result
