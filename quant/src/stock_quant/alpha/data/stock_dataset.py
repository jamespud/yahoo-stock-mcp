"""Stock dataset preparation and provider boundary."""
from __future__ import annotations

from datetime import date
from typing import Callable, Protocol

from pydantic import Field

from ...contracts import StrictModel
from ..contracts import DatasetManifest, StockDatasetRequest
from ..errors import AlphaError, AlphaErrorCode
from .catalog import DatasetStore
from .partitions import StockBarRow
from .session_coverage import aggregate_session_coverage, audit_session_coverage


class StockHistoryBatch(StrictModel):
    rows: list[StockBarRow]
    calendar_version: str = Field(min_length=1, max_length=128)
    calendar_id: str | None = Field(default=None, max_length=160)
    calendar_session_dates: list[date] = Field(default_factory=list)
    session_dates_by_symbol: dict[str, list[date]] = Field(default_factory=dict)
    exchange_mappings: dict[str, dict] = Field(default_factory=dict)
    source_version: str = Field(min_length=1, max_length=128)
    quality: dict = Field(default_factory=dict)


class StockHistoryProvider(Protocol):
    def read(self, request: StockDatasetRequest) -> StockHistoryBatch: ...


def _quality(request: StockDatasetRequest, batch: StockHistoryBatch) -> dict:
    counts = {symbol: 0 for symbol in request.symbols}
    for row in batch.rows:
        if row.instrument_id in counts:
            counts[row.instrument_id] += 1
    missing = sorted(symbol for symbol, count in counts.items() if count == 0)
    total = sum(counts.values())
    symbol_presence_coverage = (sum(1 for count in counts.values() if count) / len(counts)) if counts else 0.0
    volume_null_count = sum(row.volume is None for row in batch.rows)
    volume_zero_count = sum(row.volume == 0 for row in batch.rows)
    volume_positive_count = sum(row.volume is not None and row.volume > 0 for row in batch.rows)
    volume_total = volume_null_count + volume_zero_count + volume_positive_count
    row_volume_quality = {
        "volume_null_count": volume_null_count,
        "volume_zero_count": volume_zero_count,
        "volume_positive_count": volume_positive_count,
        "volume_invalid_count": 0,
        "volume_valid_coverage": ((volume_zero_count + volume_positive_count) / volume_total) if volume_total else 0.0,
    }
    session_reports = audit_session_coverage(
        batch.rows,
        requested_symbols=request.symbols,
        calendar_session_dates=batch.calendar_session_dates,
        calendar_id=batch.calendar_id or batch.calendar_version,
        session_dates_by_symbol=batch.session_dates_by_symbol,
    )
    return {
        **row_volume_quality,
        **batch.quality,
        "requested_symbols": len(request.symbols),
        "observed_symbols": sum(1 for count in counts.values() if count),
        "row_count": total,
        "rows_by_symbol": counts,
        "missing_symbols": missing,
        "coverage": symbol_presence_coverage,
        "symbol_presence_coverage": symbol_presence_coverage,
        "session_coverage": aggregate_session_coverage(session_reports),
        "session_coverage_by_symbol": session_reports,
        "calendar_id": batch.calendar_id or batch.calendar_version,
        "exchange_mappings": batch.exchange_mappings,
        "calendar_mapping_status_by_symbol": {
            symbol: mapping.get("calendar_mapping_status", "UNVERIFIED")
            for symbol, mapping in sorted(batch.exchange_mappings.items())
        },
        "price_basis": request.price_basis,
        "price_basis_declared": request.price_basis,
        "price_attestation_status": "BLOCKED",
        "execution_validity_status": "BLOCKED",
        "corporate_action_coverage_status": "UNVERIFIED",
        "missing_rows_filled": False,
    }


def prepare_stock_dataset(
    request: StockDatasetRequest,
    *,
    store: DatasetStore,
    provider: StockHistoryProvider,
    checkpoint: Callable[[float], None] | None = None,
) -> DatasetManifest:
    if checkpoint:
        checkpoint(0.0)
    try:
        batch = provider.read(request)
    except AlphaError:
        raise
    except PermissionError as error:
        raise AlphaError(AlphaErrorCode.PROVIDER_ACCESS_DENIED, str(error), {"provider": request.source}) from error
    except (KeyError, LookupError) as error:
        raise AlphaError(AlphaErrorCode.DATA_UNAVAILABLE, str(error), {"provider": request.source}) from error
    quality = _quality(request, batch)
    if quality["missing_symbols"]:
        raise AlphaError(
            AlphaErrorCode.DATA_UNAVAILABLE,
            "No observations for requested symbols",
            {"provider": request.source, "missing_symbols": quality["missing_symbols"]},
        )
    return store.publish_stock(
        request,
        batch.rows,
        calendar_version=batch.calendar_version,
        source_version=batch.source_version,
        data_quality=quality,
        checkpoint=checkpoint,
    )
