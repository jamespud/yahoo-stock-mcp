from datetime import datetime, timedelta, timezone

import pytest

from stock_quant.calendar import CalendarSchedule, expected_intervals


def schedule():
    return CalendarSchedule.model_validate({"exchange": "XNAS", "timezone": "America/New_York", "version": "declared-2026", "start_date": "2026-03-06", "end_date": "2026-03-09", "sessions": [
        {"date": "2026-03-06", "intervals": [{"start": "2026-03-06T09:30:00-05:00", "end": "2026-03-06T16:00:00-05:00"}]},
        {"date": "2026-03-09", "intervals": [{"start": "2026-03-09T09:30:00-04:00", "end": "2026-03-09T13:00:00-04:00"}]}]})


def test_dst_early_close_weekend_and_exact_daily_boundaries():
    calendar = schedule()
    expected = expected_intervals(calendar, "1h")
    assert len(expected) == 11
    assert expected[0].start.hour == 14
    assert expected[7].start.hour == 13
    assert (expected[-1].end - expected[-1].start).total_seconds() == 1800
    assert len(expected_intervals(calendar, "1d")) == 2
    equivalent = calendar.model_dump(mode="json")
    equivalent["sessions"] = list(reversed(equivalent["sessions"]))
    assert CalendarSchedule.model_validate(equivalent).id == calendar.id


def test_breaks_and_invalid_schedules():
    raw = schedule().model_dump(mode="json")
    raw["sessions"][0]["intervals"] = [
        {"start": "2026-03-06T09:30:00-05:00", "end": "2026-03-06T11:30:00-05:00"},
        {"start": "2026-03-06T13:00:00-05:00", "end": "2026-03-06T16:00:00-05:00"}]
    assert len(expected_intervals(CalendarSchedule.model_validate(raw), "1h")) == 9
    for change in ({"timezone": "No/SuchZone"}, {"end_date": "2026-03-05"}, {"sessions": [raw["sessions"][0], raw["sessions"][0]]}):
        with pytest.raises(ValueError):
            CalendarSchedule.model_validate({**raw, **change})
    raw["sessions"][0]["intervals"][1]["start"] = "2026-03-06T10:30:00-05:00"
    with pytest.raises(ValueError):
        CalendarSchedule.model_validate(raw)


def test_generated_calendar_holiday_early_close_dst_aliases_and_breaks():
    from stock_quant.calendar import generate_calendar
    # NYSE official 2026 schedule: Thanksgiving closed, following Friday 13:00 ET.
    c = generate_calendar(calendar_name="XNYS", exchange="XNAS", start_date="2026-11-26", end_date="2026-11-27")
    assert c.provider == "exchange_calendars" and c.provider_version
    assert len(c.sessions) == 1
    assert c.sessions[0].intervals[0].end.isoformat() == "2026-11-27T18:00:00+00:00"
    empty = generate_calendar(calendar_name="XNYS", exchange="XNYS", start_date="2026-11-26", end_date="2026-11-26")
    assert empty.sessions == ()
    dst = generate_calendar(calendar_name="XNYS", exchange="XNYS", start_date="2026-03-06", end_date="2026-03-09")
    assert [s.intervals[0].start.hour for s in dst.sessions] == [14, 13]
    hong_kong = generate_calendar(calendar_name="XHKG", exchange="XHKG", start_date="2026-11-03", end_date="2026-11-03")
    assert len(hong_kong.sessions[0].intervals) == 2
    assert hong_kong.sessions[0].intervals[0].end < hong_kong.sessions[0].intervals[1].start
    with pytest.raises(ValueError):
        generate_calendar(calendar_name="XNYS", exchange="XHKG", start_date="2026-11-03", end_date="2026-11-03")


def test_imported_generated_calendar_cannot_alias_another_exchange_or_provider_version(monkeypatch):
    from stock_quant.calendar import generate_calendar
    import importlib.metadata
    generated = generate_calendar(calendar_name="XHKG", exchange="XHKG", start_date="2026-11-03", end_date="2026-11-03")
    raw = generated.model_dump(mode="json")
    with pytest.raises(ValueError, match="exchange mapping"):
        CalendarSchedule.model_validate({**raw, "exchange": "XNAS"})
    with pytest.raises(ValueError, match="provider version"):
        CalendarSchedule.model_validate({**raw, "provider_version": "4.12.0"})
    version = importlib.metadata.version
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "4.12.0" if name == "exchange-calendars" else version(name))
    with pytest.raises(ValueError, match="provider version differs"):
        generate_calendar(calendar_name="XNYS", exchange="XNAS", start_date="2026-11-03", end_date="2026-11-03")
