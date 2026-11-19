"""Executable labels with mandatory maturity and quality timestamps."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from pydantic import AwareDatetime, Field, model_validator

from ...contracts import StrictModel
from ..options.quotes import OptionQuote
from .session_label_index import SessionLabelIndex


class LabelConfig(StrictModel):
    horizon_days: int = Field(default=5, ge=1, le=252, strict=True)
    fee_per_contract: float = Field(default=0.65, ge=0, strict=True)


class LabelRow(StrictModel):
    feature_decision_at: AwareDatetime
    entry_at: AwareDatetime
    exit_at: AwareDatetime
    label_available_at: AwareDatetime
    label_value: float | None = None
    label_quality: list[str] = Field(default_factory=list)
    instrument_id: str = Field(min_length=1, max_length=128)
    label_name: str = Field(min_length=1, max_length=128)
    label_version: str = Field(default="observed-record-v1", min_length=1, max_length=64)

    @model_validator(mode="after")
    def ordered(self):
        if not self.feature_decision_at < self.entry_at <= self.exit_at <= self.label_available_at:
            raise ValueError("Invalid label timeline")
        if self.label_value is None and not self.label_quality:
            raise ValueError("Unavailable labels require quality diagnostics")
        return self


def _observed_record_forward_labels(bars: list[dict], horizon: int = 5) -> list[LabelRow]:
    """Legacy observed-record labels retained for readable historical datasets."""
    grouped = defaultdict(list)
    for row in bars:
        grouped[row["instrument_id"]].append(row)
    labels = []
    for symbol, rows in sorted(grouped.items()):
        rows = sorted(rows, key=lambda row: (row["start_at"], row["end_at"]))
        for index, decision_row in enumerate(rows):
            entry_index = index + 1
            exit_index = entry_index + horizon
            decision_at = decision_row["available_at"]
            if exit_index >= len(rows):
                entry_at = rows[entry_index]["start_at"] if entry_index < len(rows) else decision_at + timedelta(microseconds=1)
                exit_at = entry_at + timedelta(microseconds=1)
                quality = ["INSUFFICIENT_HISTORY"]
                value = None
                available_at = exit_at
            else:
                entry = rows[entry_index]
                exit_row = rows[exit_index]
                gap_days = (exit_row["start_at"].date() - entry["start_at"].date()).days
                if gap_days > horizon + 2:
                    entry_at = entry["start_at"]
                    exit_at = exit_row["start_at"]
                    available_at = max(entry["available_at"], exit_row["available_at"])
                    labels.append(LabelRow(
                        feature_decision_at=decision_at, entry_at=entry_at, exit_at=exit_at,
                        label_available_at=available_at, label_value=None,
                        label_quality=["MISSING_LABEL_SESSIONS"], instrument_id=symbol,
                        label_name="stock_forward_return", label_version="observed-record-v1",
                    ))
                    continue
                entry_at = entry["start_at"]
                exit_at = exit_row["start_at"]
                available_at = max(entry["available_at"], exit_row["available_at"])
                value = float(exit_row["open"] / entry["open"] - 1)
                quality = []
            labels.append(LabelRow(
                feature_decision_at=decision_at, entry_at=entry_at, exit_at=exit_at,
                label_available_at=available_at, label_value=value, label_quality=quality,
                instrument_id=symbol, label_name="stock_forward_return", label_version="observed-record-v1",
            ))
    return labels


def _invalid_session_label(*, symbol, decision_at, entry_at, exit_at, available_at, reason):
    return LabelRow(
        feature_decision_at=decision_at,
        entry_at=entry_at,
        exit_at=exit_at,
        label_available_at=max(available_at, exit_at),
        label_value=None,
        label_quality=[reason],
        instrument_id=symbol,
        label_name="stock_forward_return",
        label_version="session-aligned-v2",
    )


def _session_forward_labels(bars: list[dict], horizon: int, calendar) -> list[LabelRow]:
    grouped = defaultdict(list)
    for row in bars:
        grouped[row["instrument_id"]].append(row)
    index = SessionLabelIndex(calendar.sessions)
    labels: list[LabelRow] = []
    for symbol, rows in sorted(grouped.items()):
        ordered = sorted(rows, key=lambda row: (row["start_at"], row["end_at"]))
        by_index = {}
        for row in ordered:
            session_index = index.index_for_bar(row)
            if session_index is not None:
                by_index[session_index] = row
        if not by_index:
            continue
        last_observed_index = max(by_index)
        for decision_row in ordered:
            decision_at = decision_row["available_at"]
            decision_index = index.index_for_bar(decision_row)
            if decision_index is None:
                labels.append(_invalid_session_label(
                    symbol=symbol, decision_at=decision_at,
                    entry_at=decision_at + timedelta(microseconds=1),
                    exit_at=decision_at + timedelta(microseconds=2),
                    available_at=decision_at + timedelta(microseconds=2),
                    reason="UNVERIFIED_CALENDAR",
                ))
                continue
            entry_index = decision_index + 1
            exit_index = entry_index + horizon
            entry_point = index.point(entry_index) if index.has(entry_index) else None
            exit_point = index.point(exit_index) if index.has(exit_index) else None
            if entry_point is None:
                labels.append(_invalid_session_label(
                    symbol=symbol, decision_at=decision_at,
                    entry_at=decision_at + timedelta(microseconds=1),
                    exit_at=decision_at + timedelta(microseconds=2),
                    available_at=decision_at + timedelta(microseconds=2),
                    reason="NO_ENTRY_SESSION",
                ))
                continue
            if exit_point is None:
                labels.append(_invalid_session_label(
                    symbol=symbol, decision_at=decision_at, entry_at=entry_point.start_at,
                    exit_at=entry_point.start_at + timedelta(microseconds=1),
                    available_at=entry_point.end_at,
                    reason="INSUFFICIENT_FUTURE_HISTORY",
                ))
                continue
            entry = by_index.get(entry_index)
            exit_row = by_index.get(exit_index)
            invalid_reason = None
            if entry is None:
                invalid_reason = "INSUFFICIENT_FUTURE_HISTORY" if entry_index > last_observed_index else "MISSING_ENTRY_PRICE"
            elif entry.get("open") is None:
                invalid_reason = "MISSING_ENTRY_PRICE"
            elif any(position not in by_index for position in range(entry_index + 1, exit_index)):
                invalid_reason = "MISSING_INTERMEDIATE_SESSION"
            elif exit_row is None:
                invalid_reason = "INSUFFICIENT_FUTURE_HISTORY" if exit_index > last_observed_index else "MISSING_EXIT_PRICE"
            elif exit_row.get("open") is None:
                invalid_reason = "MISSING_EXIT_PRICE"
            if invalid_reason is not None:
                available_at = max(
                    decision_at,
                    entry.get("available_at") if entry is not None else decision_at,
                    exit_row.get("available_at") if exit_row is not None else decision_at,
                    exit_point.end_at,
                )
                labels.append(_invalid_session_label(
                    symbol=symbol, decision_at=decision_at, entry_at=entry_point.start_at,
                    exit_at=exit_point.start_at, available_at=available_at, reason=invalid_reason,
                ))
                continue
            value = float(exit_row["open"] / entry["open"] - 1)
            labels.append(LabelRow(
                feature_decision_at=decision_at,
                entry_at=entry_point.start_at,
                exit_at=exit_point.start_at,
                label_available_at=max(entry["available_at"], exit_row["available_at"]),
                label_value=value,
                label_quality=[],
                instrument_id=symbol,
                label_name="stock_forward_return",
                label_version="session-aligned-v2",
            ))
    return labels


def stock_forward_labels(bars: list[dict], horizon: int = 5, *, calendar=None) -> list[LabelRow]:
    """Build stock labels under an explicit calendar or the legacy record contract.

    New jobs with a versioned calendar use session-aligned-v2: missing expected
    sessions invalidate the label and never shift to a later observed bar.
    Datasets without calendar evidence remain readable under observed-record-v1.
    """
    if calendar is None:
        return _observed_record_forward_labels(bars, horizon)
    return _session_forward_labels(bars, horizon, calendar)


def option_forward_labels(quotes: list[OptionQuote], horizon_days: int = 5, fee_per_contract: float = 0.65):
    grouped = defaultdict(list)
    for quote in quotes:
        grouped[quote.contract.contract_id].append(quote)
    returns, iv_changes = [], []
    for contract_id, contract_quotes in sorted(grouped.items()):
        ordered = sorted(contract_quotes, key=lambda quote: quote.quote_at or quote.observed_at)
        for index, decision in enumerate(ordered):
            decision_at = decision.quote_at or decision.observed_at
            entry = next((quote for quote in ordered[index + 1:] if quote.ask and quote.ask > 0), None)
            if entry is None:
                returns.append(LabelRow(
                    feature_decision_at=decision_at, entry_at=decision_at + timedelta(microseconds=1),
                    exit_at=decision_at + timedelta(microseconds=2), label_available_at=decision_at + timedelta(microseconds=2),
                    label_value=None, label_quality=["NO_ENTRY_QUOTE"], instrument_id=contract_id,
                    label_name="option_forward_return",
                ))
                continue
            entry_at = entry.quote_at or entry.observed_at
            exit_candidate = next((quote for quote in ordered[index + 2:] if (quote.quote_at or quote.observed_at) >= entry_at + timedelta(days=horizon_days) and quote.bid and quote.bid > 0), None)
            if exit_candidate is None:
                exit_at = entry_at + timedelta(days=horizon_days)
                available_at = exit_at
                quality = ["NO_EXIT_QUOTE"]
                value = None
            else:
                exit_at = exit_candidate.quote_at or exit_candidate.observed_at
                available_at = max(entry.available_at, exit_candidate.available_at)
                multiplier = entry.contract.multiplier
                gross_entry = entry.ask * multiplier
                gross_exit = exit_candidate.bid * multiplier
                value = (gross_exit - gross_entry - 2 * fee_per_contract) / gross_entry
                quality = []
            returns.append(LabelRow(
                feature_decision_at=decision_at, entry_at=entry_at, exit_at=exit_at,
                label_available_at=available_at, label_value=value, label_quality=quality,
                instrument_id=contract_id, label_name="option_forward_return",
            ))
            if exit_candidate is not None and decision.implied_volatility is not None and exit_candidate.implied_volatility is not None:
                iv_changes.append(LabelRow(
                    feature_decision_at=decision_at, entry_at=entry_at, exit_at=exit_at,
                    label_available_at=available_at,
                    label_value=exit_candidate.implied_volatility - decision.implied_volatility,
                    label_quality=[], instrument_id=contract_id, label_name="option_future_iv_change",
                ))
    return returns, iv_changes


def historical_option_labels(option_dataset, store, horizon_days: int = 5, fee_per_contract: float = 0.65):
    from ..data.option_dataset import validate_historical_quotes
    from ..options.quotes import quote_from_dataset_row
    validate_historical_quotes(option_dataset)
    quotes = [quote_from_dataset_row(row) for row in store.read_rows(option_dataset.dataset_id)]
    return option_forward_labels(quotes, horizon_days=horizon_days, fee_per_contract=fee_per_contract)
