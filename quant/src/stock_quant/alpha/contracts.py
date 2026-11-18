"""Strict public contracts for the Alpha research pipeline."""
from __future__ import annotations

from datetime import datetime
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator

from ..contracts import StrictModel

DatasetKind = Literal["stock_bars", "option_quotes", "feature_panel", "prediction_panel"]
SourceStatus = Literal["AVAILABLE", "UNAVAILABLE", "DEGRADED", "BLOCKED"]
AvailabilityPolicy = Literal[
    "declared",
    "modeled_bar_end_plus_delay",
    "observed",
    "historical_quote_at",
    "decision_time_known_fields_only",
]
PriceBasis = Literal[
    "raw_attested",
    "adjusted",
    "split_adjusted",
    "total_return",
    "price_basis_unverified",
    "unknown",
]
QuotePolicy = Literal["observed_snapshot", "historical_bid_ask"]
Hash = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
Identifier = Annotated[str, Field(min_length=1, max_length=128)]

FACTOR_OPERATORS: tuple[str, ...] = (
    "lag",
    "pct_change",
    "log_return",
    "rolling_mean",
    "rolling_std",
    "rolling_min",
    "rolling_max",
    "sma",
    "ema",
    "rsi",
    "add",
    "sub",
    "mul",
    "div",
    "cs_rank_pct",
    "cs_zscore",
    "cs_winsorize",
)
LABEL_TYPES: tuple[str, ...] = ("stock_forward_return", "option_forward_return", "option_future_iv_change")


class DatasetPartition(StrictModel):
    ordinal: int = Field(ge=0, strict=True)
    logical_path: str = Field(min_length=1, max_length=512)
    format: Literal["parquet-v1"]
    content_hash: Hash
    schema_hash: Hash
    row_count: int = Field(ge=0, strict=True)
    byte_count: int = Field(gt=0, strict=True)

    @field_validator("logical_path")
    @classmethod
    def safe_relative_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or "\\" in value or any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError("Partition paths must be safe relative POSIX paths")
        return value


class DatasetManifest(StrictModel):
    dataset_id: Hash
    schema_version: Literal["1.0"]
    dataset_kind: DatasetKind
    source: Identifier
    source_version: Identifier
    calendar_version: Identifier
    universe_definition: dict[str, Any]
    instrument_ids: list[Identifier] = Field(min_length=1, max_length=10000)
    start_at: AwareDatetime
    end_at: AwareDatetime
    captured_at: AwareDatetime
    availability_policy: AvailabilityPolicy
    price_basis: PriceBasis
    data_quality: dict[str, Any]
    partition_manifest: list[DatasetPartition] = Field(min_length=1)
    content_hash: Hash
    parent_dataset_ids: list[Hash] = Field(default_factory=list, max_length=128)

    @model_validator(mode="after")
    def coherent(self):
        if self.start_at >= self.end_at:
            raise ValueError("Dataset start_at must be before end_at")
        if len(set(self.instrument_ids)) != len(self.instrument_ids):
            raise ValueError("Dataset instrument_ids must be unique")
        ordinals = [part.ordinal for part in self.partition_manifest]
        if ordinals != list(range(len(ordinals))):
            raise ValueError("Partition ordinals must be contiguous and sorted")
        paths = [part.logical_path for part in self.partition_manifest]
        if len(set(paths)) != len(paths):
            raise ValueError("Partition paths must be unique")
        return self


class StockDatasetRequest(StrictModel):
    source: Identifier = "yahoo"
    symbols: list[Identifier] = Field(min_length=1, max_length=1000)
    start: AwareDatetime
    end: AwareDatetime
    price_basis: PriceBasis = "price_basis_unverified"
    calendar_version: Identifier = "caller-supplied"
    universe_definition: dict[str, Any] = Field(default_factory=dict)
    availability_policy: AvailabilityPolicy = "modeled_bar_end_plus_delay"
    adjustment: Literal["raw", "split", "total_return", "unknown"] = "unknown"

    @field_validator("symbols")
    @classmethod
    def normalize_symbols(cls, values: list[str]) -> list[str]:
        normalized = [value.strip().upper() for value in values]
        if any(not value or any(char in value for char in "/\\") or value in {".", ".."} for value in normalized):
            raise ValueError("Symbols must be safe non-empty identifiers")
        if len(set(normalized)) != len(normalized):
            raise ValueError("Symbols must be unique")
        return normalized

    @model_validator(mode="after")
    def period(self):
        if self.start >= self.end:
            raise ValueError("start must be before end")
        return self


class OptionDatasetRequest(StrictModel):
    source: Literal["yahoo", "thetadata"]
    underlyings: list[Identifier] = Field(min_length=1, max_length=500)
    start: AwareDatetime
    end: AwareDatetime
    quote_policy: QuotePolicy
    expirations: list[str] | None = Field(default=None, max_length=100)
    provider_dataset: str | None = Field(default=None, max_length=128)
    price_basis: PriceBasis = "price_basis_unverified"

    @field_validator("underlyings")
    @classmethod
    def normalize_underlyings(cls, values: list[str]) -> list[str]:
        normalized = [value.strip().upper() for value in values]
        if any(not value or any(char in value for char in "/\\") or value in {".", ".."} for value in normalized):
            raise ValueError("Underlyings must be safe non-empty identifiers")
        if len(set(normalized)) != len(normalized):
            raise ValueError("Underlyings must be unique")
        return normalized

    @model_validator(mode="after")
    def period_and_policy(self):
        if self.start >= self.end:
            raise ValueError("start must be before end")
        if self.source == "yahoo" and self.quote_policy != "observed_snapshot":
            raise ValueError("Yahoo option data can only be used as observed snapshots")
        if self.source == "thetadata" and self.quote_policy != "historical_bid_ask":
            raise ValueError("ThetaData option datasets require historical_bid_ask")
        return self


class FeatureSetRequest(StrictModel):
    dataset_id: Hash
    factor_spec: dict[str, Any]
    pricing_config: dict[str, Any] | None = None


class PredictionSetRequest(StrictModel):
    model_set_id: Hash
    feature_set_id: Hash
    start: AwareDatetime
    end: AwareDatetime


class ProviderCoverage(StrictModel):
    provider: Identifier
    status: SourceStatus
    reason: str | None = Field(default=None, max_length=1024)
    coverage: list[str] = Field(default_factory=list)
    verified_at: datetime | None = None
    dataset_ids: list[Hash] = Field(default_factory=list)


class ModelResourceBudget(StrictModel):
    max_seconds: int = Field(default=1800, ge=1, le=86400, strict=True)
    max_rows: int = Field(default=5_000_000, ge=1, strict=True)
