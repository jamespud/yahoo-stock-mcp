from datetime import date, datetime, timedelta, timezone

import pytest

from stock_quant.alpha.models.labels import (
    LabelConfig,
    LabelRow,
    option_forward_labels,
    stock_forward_labels,
)
from stock_quant.alpha.models.temporal_split import TemporalSplit, split_matured_labels


def _bars(symbol="AAPL", days=12):
    rows = []
    start = datetime(2024, 1, 1, 14, 30, tzinfo=timezone.utc)
    for day in range(days):
        at = start + timedelta(days=day)
        rows.append({"instrument_id": symbol, "start_at": at, "end_at": at.replace(hour=21, minute=0), "available_at": at.replace(hour=21, minute=1),
                     "open": 100 + day, "high": 104 + day, "low": 99 + day, "close": 102 + day, "volume": 1000, "adjustment": "raw",
                     "source": "fixture", "source_version": "v1", "currency": "USD"})
    return rows


def test_stock_label_uses_next_open_and_holds_five_sessions():
    labels = stock_forward_labels(_bars(), horizon=5)
    first = labels[0]
    assert first.feature_decision_at == datetime(2024, 1, 1, 21, 1, tzinfo=timezone.utc)
    assert first.entry_at == datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)
    assert first.exit_at == datetime(2024, 1, 7, 14, 30, tzinfo=timezone.utc)
    assert first.label_value == pytest.approx((106 / 101) - 1)
    assert first.label_available_at >= first.exit_at
    assert all(label.label_quality == [] for label in labels[:-6])
    assert all(label.label_value is None and "INSUFFICIENT_HISTORY" in label.label_quality for label in labels[-6:])


def test_temporal_split_purges_immature_and_overlapping_labels():
    split = TemporalSplit(
        train_start=datetime(2024, 1, 1, tzinfo=timezone.utc),
        train_end=datetime(2024, 2, 1, tzinfo=timezone.utc),
        validation_start=datetime(2024, 2, 1, tzinfo=timezone.utc),
        validation_end=datetime(2024, 3, 1, tzinfo=timezone.utc),
        test_start=datetime(2024, 3, 1, tzinfo=timezone.utc),
        test_end=datetime(2024, 4, 1, tzinfo=timezone.utc),
    )
    rows = [
        LabelRow(feature_decision_at=datetime(2024, 1, 20, tzinfo=timezone.utc), entry_at=datetime(2024, 1, 21, tzinfo=timezone.utc), exit_at=datetime(2024, 1, 30, tzinfo=timezone.utc), label_available_at=datetime(2024, 1, 30, tzinfo=timezone.utc), label_value=0.1, label_quality=[], instrument_id="AAPL", label_name="stock_forward_return"),
        LabelRow(feature_decision_at=datetime(2024, 1, 30, tzinfo=timezone.utc), entry_at=datetime(2024, 1, 31, tzinfo=timezone.utc), exit_at=datetime(2024, 2, 7, tzinfo=timezone.utc), label_available_at=datetime(2024, 2, 7, tzinfo=timezone.utc), label_value=0.2, label_quality=[], instrument_id="AAPL", label_name="stock_forward_return"),
        LabelRow(feature_decision_at=datetime(2024, 2, 5, tzinfo=timezone.utc), entry_at=datetime(2024, 2, 6, tzinfo=timezone.utc), exit_at=datetime(2024, 2, 12, tzinfo=timezone.utc), label_available_at=datetime(2024, 2, 12, tzinfo=timezone.utc), label_value=0.3, label_quality=[], instrument_id="AAPL", label_name="stock_forward_return"),
    ]
    selected = split_matured_labels(rows, split)
    assert [row.label_value for row in selected["train"]] == [0.1]
    assert [row.label_value for row in selected["validation"]] == [0.3]
    assert selected["test"] == []


def test_option_return_uses_ask_entry_bid_exit_and_multiplier():
    from stock_quant.alpha.options.contracts import OptionContract, option_contract_id
    from stock_quant.alpha.options.quotes import OptionQuote
    from stock_quant.alpha.models.labels import option_forward_labels

    fields = dict(underlying_id="AAPL", option_type="CALL", strike=200.0, expiration=date(2027, 1, 15),
                  exercise_style="american", settlement_type="physical", multiplier=100,
                  currency="USD", deliverable="100 shares AAPL", adjustment_version="standard")
    contract = OptionContract(contract_id=option_contract_id(**fields), **fields)
    def quote(at, bid, ask, iv):
        return OptionQuote(contract=contract, observed_at=at, quote_at=at, available_at=at + timedelta(seconds=1),
                           bid=bid, ask=ask, bid_size=10, ask_size=10, last_price=(bid + ask) / 2,
                           last_trade_at=None, volume=10, open_interest=100, implied_volatility=iv,
                           source="thetadata", source_version="v3", quality_flags=[])
    start = datetime(2026, 1, 1, 15, tzinfo=timezone.utc)
    returns, iv_changes = option_forward_labels([
        quote(start, 4.8, 5.0, 0.30),
        quote(start + timedelta(days=1), 5.0, 5.2, 0.31),
        quote(start + timedelta(days=6), 5.8, 6.0, 0.35),
    ], horizon_days=5, fee_per_contract=0.65)
    first = returns[0]
    assert first.entry_at == start + timedelta(days=1)
    assert first.exit_at == start + timedelta(days=6)
    assert first.label_value == pytest.approx(((5.8 - 5.2) * 100 - 1.3) / (5.2 * 100))
    assert iv_changes[0].label_value == pytest.approx(0.35 - 0.30)
