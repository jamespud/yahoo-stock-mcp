"""Stock dataset preparation and provider boundary."""
from __future__ import annotations

from typing import Callable, Protocol

from pydantic import Field

from ...contracts import StrictModel
from ..contracts import DatasetManifest, StockDatasetRequest
from ..errors import AlphaError, AlphaErrorCode
from .catalog import DatasetStore
from .partitions import StockBarRow


class StockHistoryBatch(StrictModel):
    rows: list[StockBarRow]
    calendar_version: str = Field(min_length=1, max_length=128)
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
    return {
        **batch.quality,
        "requested_symbols": len(request.symbols),
        "observed_symbols": sum(1 for count in counts.values() if count),
        "row_count": total,
        "rows_by_symbol": counts,
        "missing_symbols": missing,
        "coverage": (sum(1 for count in counts.values() if count) / len(counts)) if counts else 0.0,
        "price_basis": request.price_basis,
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
