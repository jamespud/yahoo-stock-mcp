"""Immutable option quote datasets and historical-quote admission."""
from __future__ import annotations

from typing import Callable, Protocol

from pydantic import Field

from ...contracts import StrictModel
from ..contracts import DatasetManifest, OptionDatasetRequest
from ..errors import AlphaError, AlphaErrorCode
from ..options.quotes import OptionQuote
from .catalog import DatasetStore


class OptionSnapshotBatch(StrictModel):
    rows: list[OptionQuote]
    source_version: str = Field(min_length=1, max_length=128)
    quality: dict = Field(default_factory=dict)


class OptionDataProvider(Protocol):
    def read(self, request: OptionDatasetRequest) -> OptionSnapshotBatch: ...


def validate_historical_quotes(manifest: DatasetManifest) -> bool:
    if manifest.dataset_kind != "option_quotes":
        raise AlphaError(AlphaErrorCode.DATASET_MISMATCH, "Expected an option_quotes dataset")
    quality = manifest.data_quality
    if manifest.source != "thetadata" or quality.get("quote_policy") != "historical_bid_ask":
        raise AlphaError(AlphaErrorCode.OPTION_HISTORY_UNAVAILABLE, "Dataset does not contain admitted historical option quotes", {"source": manifest.source})
    if quality.get("quote_at_missing", 0) or quality.get("historical_quote_rows", 0) < quality.get("observed_rows", 1):
        raise AlphaError(AlphaErrorCode.OPTION_HISTORY_UNAVAILABLE, "Historical quote timestamps are incomplete")
    return True


def prepare_option_dataset(
    request: OptionDatasetRequest,
    *,
    store: DatasetStore,
    provider: OptionDataProvider,
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
    rows = list(batch.rows)
    if not rows:
        raise AlphaError(AlphaErrorCode.DATA_UNAVAILABLE, "No option quotes returned", {"provider": request.source})
    identities = [(row.contract.contract_id, row.observed_at) for row in rows]
    if len(set(identities)) != len(identities):
        raise AlphaError(AlphaErrorCode.INVALID_OPTION_QUOTE, "Duplicate contract observation")
    if request.quote_policy == "historical_bid_ask":
        if any(row.quote_at is None or row.bid is None or row.ask is None or row.bid <= 0 or row.ask <= 0 for row in rows):
            raise AlphaError(AlphaErrorCode.INVALID_OPTION_QUOTE, "Historical option rows require valid positive Bid/Ask and quote_at")
    observed_underlyings = {row.contract.underlying_id for row in rows}
    if not observed_underlyings.issubset(set(request.underlyings)):
        raise AlphaError(AlphaErrorCode.DATASET_MISMATCH, "Provider returned an unrequested underlying")
    quote_at_present = sum(row.quote_at is not None for row in rows)
    historical_rows = sum(row.quote_at is not None and row.source == "thetadata" for row in rows)
    quality = {
        **batch.quality,
        "quote_policy": request.quote_policy,
        "observed_rows": len(rows),
        "historical_quote_rows": historical_rows,
        "quote_at_missing": len(rows) - quote_at_present,
        "contract_count": len({row.contract.contract_id for row in rows}),
        "underlyings": sorted(observed_underlyings),
    }
    manifest = store.publish_option_panel(
        rows,
        request=request,
        source_version=batch.source_version,
        data_quality=quality,
        checkpoint=checkpoint,
    )
    if request.quote_policy == "historical_bid_ask":
        validate_historical_quotes(manifest)
    return manifest
