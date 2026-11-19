"""Executable labels with mandatory maturity and quality timestamps."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from pydantic import AwareDatetime, Field, model_validator

from ...contracts import StrictModel
from ..options.quotes import OptionQuote


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

    @model_validator(mode="after")
    def ordered(self):
        if not self.feature_decision_at < self.entry_at <= self.exit_at <= self.label_available_at:
            raise ValueError("Invalid label timeline")
        if self.label_value is None and not self.label_quality:
            raise ValueError("Unavailable labels require quality diagnostics")
        return self


def stock_forward_labels(bars: list[dict], horizon: int = 5) -> list[LabelRow]:
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
                # Preserve a reference timeline using the next bar where possible.
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
                        label_name="stock_forward_return",
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
                instrument_id=symbol, label_name="stock_forward_return",
            ))
    return labels


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
