"""Deterministic typed Parquet partitions for Alpha datasets."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Iterable

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import Field, field_validator, model_validator

from ...compiler import digest
from ...contracts import StrictModel


STOCK_BAR_SCHEMA = pa.schema([
    ("instrument_id", pa.string()),
    ("start_at", pa.timestamp("us", tz="UTC")),
    ("end_at", pa.timestamp("us", tz="UTC")),
    ("available_at", pa.timestamp("us", tz="UTC")),
    ("open", pa.float64()),
    ("high", pa.float64()),
    ("low", pa.float64()),
    ("close", pa.float64()),
    ("volume", pa.float64()),
    ("adjustment", pa.string()),
    ("source", pa.string()),
    ("source_version", pa.string()),
    ("currency", pa.string()),
])
STOCK_BAR_COLUMNS = tuple(field.name for field in STOCK_BAR_SCHEMA)

FEATURE_SCHEMA = pa.schema([
    ("decision_at", pa.timestamp("us", tz="UTC")),
    ("instrument_id", pa.string()),
    ("factor_name", pa.string()),
    ("factor_value", pa.float64()),
    ("feature_available_at", pa.timestamp("us", tz="UTC")),
    ("eligible", pa.bool_()),
    ("quality_flags", pa.list_(pa.string())),
    ("factor_version", pa.string()),
])

OPTION_QUOTE_SCHEMA = pa.schema([
    ("contract_id", pa.string()), ("underlying_id", pa.string()), ("option_type", pa.string()),
    ("strike", pa.float64()), ("expiration", pa.date32()), ("exercise_style", pa.string()),
    ("settlement_type", pa.string()), ("multiplier", pa.int64()), ("currency", pa.string()),
    ("deliverable", pa.string()), ("adjustment_version", pa.string()),
    ("observed_at", pa.timestamp("us", tz="UTC")), ("quote_at", pa.timestamp("us", tz="UTC")),
    ("available_at", pa.timestamp("us", tz="UTC")), ("bid", pa.float64()), ("ask", pa.float64()),
    ("bid_size", pa.int64()), ("ask_size", pa.int64()), ("last_price", pa.float64()),
    ("last_trade_at", pa.timestamp("us", tz="UTC")), ("volume", pa.int64()),
    ("open_interest", pa.int64()), ("implied_volatility", pa.float64()),
    ("source", pa.string()), ("source_version", pa.string()), ("quality_flags", pa.list_(pa.string())),
])

PREDICTION_SCHEMA = pa.schema([
    ("decision_at", pa.timestamp("us", tz="UTC")), ("instrument_id", pa.string()),
    ("prediction_value", pa.float64()), ("model_id", pa.string()), ("model_set_id", pa.string()), ("fold", pa.string()),
])
FORMAT = "parquet-v1"


class StockBarRow(StrictModel):
    instrument_id: str = Field(min_length=1, max_length=64)
    start_at: "AwareDatetime"
    end_at: "AwareDatetime"
    available_at: "AwareDatetime"
    open: float = Field(gt=0, strict=True)
    high: float = Field(gt=0, strict=True)
    low: float = Field(gt=0, strict=True)
    close: float = Field(gt=0, strict=True)
    volume: float = Field(ge=0, strict=True)
    adjustment: str = Field(min_length=1, max_length=32)
    source: str = Field(min_length=1, max_length=128)
    source_version: str = Field(min_length=1, max_length=128)
    currency: str = Field(pattern=r"^[A-Z]{3}$")

    @field_validator("instrument_id")
    @classmethod
    def safe_instrument(cls, value: str) -> str:
        value = value.strip().upper()
        if not value or any(char in value for char in "/\\") or value in {".", ".."}:
            raise ValueError("Unsafe instrument identifier")
        return value

    @model_validator(mode="after")
    def coherent(self):
        if not self.start_at < self.end_at <= self.available_at:
            raise ValueError("Expected start_at < end_at <= available_at")
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close) or self.low > self.high:
            raise ValueError("Inconsistent OHLC values")
        return self


# Resolve the postponed annotation without importing datetime at runtime in the public contract.
from datetime import datetime  # noqa: E402
from pydantic import AwareDatetime  # noqa: E402
StockBarRow.model_rebuild()


def normalise_stock_rows(rows: Iterable[StockBarRow | dict]) -> list[StockBarRow]:
    validated = [StockBarRow.model_validate(row.model_dump() if isinstance(row, StockBarRow) else row) for row in rows]
    validated.sort(key=lambda row: (row.instrument_id, row.start_at, row.end_at))
    seen = set()
    for row in validated:
        identity = (row.instrument_id, row.start_at, row.end_at)
        if identity in seen:
            raise ValueError("Duplicate stock bar identity")
        seen.add(identity)
    return validated


def stock_rows_by_instrument(rows: Iterable[StockBarRow]) -> dict[str, list[StockBarRow]]:
    grouped: dict[str, list[StockBarRow]] = {}
    for row in rows:
        grouped.setdefault(row.instrument_id, []).append(row)
    return {symbol: sorted(grouped[symbol], key=lambda row: (row.start_at, row.end_at)) for symbol in sorted(grouped)}


def schema_hash(schema: pa.Schema) -> str:
    return digest({"format": FORMAT, "schema": str(schema)})


def file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            hasher.update(chunk)
    return "sha256:" + hasher.hexdigest()


def write_stock_partition(path: Path, rows: list[StockBarRow]) -> tuple[str, str, int, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [row.model_dump(mode="python") for row in rows]
    table = pa.Table.from_pylist(payload, schema=STOCK_BAR_SCHEMA)
    pq.write_table(table, path, compression="zstd", write_statistics=True, data_page_version="1.0")
    with path.open("rb") as stream:
        os.fsync(stream.fileno())
    return file_sha256(path), schema_hash(STOCK_BAR_SCHEMA), table.num_rows, path.stat().st_size


def read_partition(path: Path, columns: list[str] | None = None) -> list[dict]:
    table = pq.read_table(path, columns=columns)
    return table.to_pylist()


def write_feature_partition(path: Path, rows: list[dict] | pa.Table) -> tuple[str, str, int, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    table = rows if isinstance(rows, pa.Table) else pa.Table.from_pylist(rows, schema=FEATURE_SCHEMA)
    pq.write_table(table, path, compression="zstd", write_statistics=True, data_page_version="1.0")
    with path.open("rb") as stream:
        os.fsync(stream.fileno())
    return file_sha256(path), schema_hash(FEATURE_SCHEMA), table.num_rows, path.stat().st_size


def write_option_partition(path: Path, rows: list[dict]) -> tuple[str, str, int, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(rows, schema=OPTION_QUOTE_SCHEMA)
    pq.write_table(table, path, compression="zstd", write_statistics=True, data_page_version="1.0")
    with path.open("rb") as stream:
        os.fsync(stream.fileno())
    return file_sha256(path), schema_hash(OPTION_QUOTE_SCHEMA), table.num_rows, path.stat().st_size


def write_prediction_partition(path: Path, rows: list[dict]) -> tuple[str, str, int, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(rows, schema=PREDICTION_SCHEMA)
    pq.write_table(table, path, compression="zstd", write_statistics=True, data_page_version="1.0")
    with path.open("rb") as stream:
        os.fsync(stream.fileno())
    return file_sha256(path), schema_hash(PREDICTION_SCHEMA), table.num_rows, path.stat().st_size
