from datetime import date, datetime, time, timedelta, timezone

import pytest

from stock_quant.calendar import generate_calendar
from stock_quant.alpha.models.labels import stock_forward_labels
from stock_quant.alpha.models.temporal_split import ModelDevelopmentSplit, split_matured_development


def _bar(session, open_price):
    start = session.intervals[0].start
    end = session.intervals[-1].end
    reference = open_price if open_price is not None else 100.0
    return {
        "instrument_id": "AAPL", "start_at": start, "end_at": end,
        "available_at": end + timedelta(minutes=5), "open": open_price,
        "high": reference + 1, "low": reference - 1, "close": reference,
        "volume": 1000.0, "adjustment": "raw", "source": "fixture",
        "source_version": "v1", "currency": "USD",
    }


def _calendar(start=date(2024, 11, 1), end=date(2025, 1, 10)):
    return generate_calendar(calendar_name="XNYS", exchange="XNYS", start_date=start, end_date=end)


def _bars(calendar, first, last, *, skip=(), null_entry=None, null_exit=None):
    rows = []
    for index, session in enumerate(calendar.sessions):
        if first <= session.date <= last and session.date not in set(skip):
            open_price = 100.0 + index
            if session.date == null_entry:
                open_price = None
            if session.date == null_exit:
                open_price = None
            rows.append(_bar(session, open_price))
    return rows


def _label(labels, decision):
    return next(row for row in labels if row.feature_decision_at.date() == decision)


def test_five_session_label_across_weekend():
    calendar = _calendar()
    labels = stock_forward_labels(
        _bars(calendar, date(2024, 11, 4), date(2024, 11, 20)), horizon=5, calendar=calendar
    )
    row = _label(labels, date(2024, 11, 8))
    assert row.entry_at.date() == date(2024, 11, 11)
    assert row.exit_at.date() == date(2024, 11, 18)
    assert row.label_value is not None
    assert row.label_version == "session-aligned-v2"


def test_five_session_label_across_thanksgiving():
    calendar = _calendar()
    labels = stock_forward_labels(
        _bars(calendar, date(2024, 11, 20), date(2024, 12, 5)), horizon=5, calendar=calendar
    )
    row = _label(labels, date(2024, 11, 22))
    assert row.entry_at.date() == date(2024, 11, 25)
    assert row.exit_at.date() == date(2024, 12, 3)
    assert row.label_value is not None


def test_five_session_label_across_christmas():
    calendar = _calendar()
    labels = stock_forward_labels(
        _bars(calendar, date(2024, 12, 18), date(2025, 1, 3)), horizon=5, calendar=calendar
    )
    row = _label(labels, date(2024, 12, 20))
    assert row.entry_at.date() == date(2024, 12, 23)
    assert row.exit_at.date() == date(2024, 12, 31)
    assert row.label_value is not None


def test_five_session_label_across_early_close():
    calendar = _calendar()
    rows = _bars(calendar, date(2024, 11, 20), date(2024, 12, 5))
    labels = stock_forward_labels(rows, horizon=5, calendar=calendar)
    row = _label(labels, date(2024, 11, 22))
    early_close = next(session for session in calendar.sessions if session.date == date(2024, 11, 29))
    assert early_close.intervals[-1].end.hour < 21
    assert row.exit_at.date() == date(2024, 12, 3)
    assert row.label_value is not None


def test_missing_trading_bar_is_not_skipped():
    calendar = _calendar()
    skipped = date(2024, 11, 27)
    labels = stock_forward_labels(
        _bars(calendar, date(2024, 11, 20), date(2024, 12, 5), skip={skipped}),
        horizon=5,
        calendar=calendar,
    )
    row = _label(labels, date(2024, 11, 22))
    assert row.label_value is None
    assert "MISSING_INTERMEDIATE_SESSION" in row.label_quality
    assert row.exit_at.date() == date(2024, 12, 3)


def test_missing_entry_and_exit_prices_have_distinct_reasons():
    calendar = _calendar()
    entry_rows = _bars(calendar, date(2024, 11, 20), date(2024, 12, 5), null_entry=date(2024, 11, 25))
    entry_label = _label(stock_forward_labels(entry_rows, horizon=5, calendar=calendar), date(2024, 11, 22))
    assert entry_label.label_value is None
    assert "MISSING_ENTRY_PRICE" in entry_label.label_quality

    exit_rows = _bars(calendar, date(2024, 11, 20), date(2024, 12, 5), null_exit=date(2024, 12, 3))
    exit_label = _label(stock_forward_labels(exit_rows, horizon=5, calendar=calendar), date(2024, 11, 22))
    assert exit_label.label_value is None
    assert "MISSING_EXIT_PRICE" in exit_label.label_quality


def test_label_maturity_prevents_split_leakage():
    calendar = _calendar(start=date(2024, 12, 1), end=date(2025, 1, 10))
    labels = stock_forward_labels(
        _bars(calendar, date(2024, 12, 16), date(2025, 1, 3)), horizon=5, calendar=calendar
    )
    split = ModelDevelopmentSplit(
        train_start=datetime(2024, 1, 1, tzinfo=timezone.utc),
        train_end=datetime(2024, 12, 1, tzinfo=timezone.utc),
        validation_start=datetime(2024, 12, 1, tzinfo=timezone.utc),
        validation_end=datetime(2024, 12, 27, tzinfo=timezone.utc),
        embargo_days=0,
    )
    matured = split_matured_development(labels, split)
    assert all(row.label_available_at < datetime(2024, 12, 27, tzinfo=timezone.utc) for row in matured["validation"])
    assert not any(row.feature_decision_at.date() == date(2024, 12, 23) for row in matured["validation"])


def test_last_observation_without_exit_is_invalid():
    calendar = _calendar()
    labels = stock_forward_labels(
        _bars(calendar, date(2024, 11, 20), date(2024, 11, 26)), horizon=5, calendar=calendar
    )
    last = labels[-1]
    assert last.label_value is None
    assert any(reason in last.label_quality for reason in {"INSUFFICIENT_FUTURE_HISTORY", "MISSING_EXIT_PRICE"})


def test_no_entry_session_is_explicit():
    calendar = _calendar()
    last_session = calendar.sessions[-1]
    labels = stock_forward_labels([_bar(last_session, 100.0)], horizon=5, calendar=calendar)
    assert labels[0].label_value is None
    assert "NO_ENTRY_SESSION" in labels[0].label_quality


def test_legacy_observed_calendar_remains_readable():
    session = generate_calendar(calendar_name="XNYS", exchange="XNYS", start_date=date(2024, 1, 1), end_date=date(2024, 1, 3)).sessions[0]
    labels = stock_forward_labels([_bar(session, 100.0)], horizon=5, calendar=None)
    assert labels[0].label_version == "observed-record-v1"
