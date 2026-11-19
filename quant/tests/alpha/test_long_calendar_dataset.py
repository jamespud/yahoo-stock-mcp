from datetime import date, timezone

import pytest

from stock_quant.calendar import generate_calendar, generate_long_calendar


def test_long_calendar_splits_and_merges_without_duplicates():
    start, end = date(2008, 1, 1), date(2026, 11, 18)
    calendar = generate_long_calendar(calendar_name="XNYS", exchange="XNAS", start_date=start, end_date=end)
    reference = generate_calendar(calendar_name="XNYS", exchange="XNAS", start_date=date(2011, 1, 1), end_date=date(2015, 12, 31))
    overlap = [session for session in calendar.sessions if reference.start_date <= session.date <= reference.end_date]
    assert overlap == list(reference.sessions)
    assert calendar.start_date == start
    assert calendar.end_date == end
    assert len(calendar.long_calendar_shards) > 1
    assert calendar.calendar_validation_status == "MULTI_SHARD_MERGED"
    assert calendar.exchange == "XNAS"
    assert calendar.provider_version == "4.13.2"


def test_long_calendar_shards_do_not_overlap_or_repeat_sessions():
    start, end = date(2008, 1, 1), date(2026, 11, 18)
    calendar = generate_long_calendar(calendar_name="XNYS", exchange="XNAS", start_date=start, end_date=end)
    dates = [session.date for session in calendar.sessions]
    assert len(dates) == len(set(dates))
    shards = calendar.long_calendar_shards
    assert all(shard["start"] <= shard["end"] for shard in shards)
    for first, second in zip(shards, shards[1:]):
        assert first["end"] < second["start"]
