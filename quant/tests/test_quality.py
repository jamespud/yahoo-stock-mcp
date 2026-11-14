from datetime import timedelta

from stock_quant.data import Snapshot, MarketBar
from stock_quant.quality import data_quality, resample_snapshot
from test_calendar import schedule


def fixture():
    c = schedule()
    start = c.sessions[0].intervals[0].start
    rows = [MarketBar(symbol="AAPL", timeframe="15m", exchange="XNAS", start_time=start + timedelta(minutes=15*i), end_time=start + timedelta(minutes=15*(i+1)), available_at=start + timedelta(minutes=15*(i+1)+ (10 if i == 3 else 0)), open=10.+i, high=12.+i, low=9.+i, close=11.+i, volume=100., source="fixture", dataset_version="v1", adjustment="raw") for i in range(8)]
    return Snapshot(rows, calendar_version="test", calendars=[c])


def test_calendar_changes_snapshot_identity_and_quality_asof_hides_late_values():
    snapshot = fixture()
    assert snapshot.id != Snapshot(list(snapshot.bars), calendar_version="test").id
    asof = snapshot.bars[3].end_time
    q = data_quality(snapshot, symbol="AAPL", timeframe="15m", as_of=asof)
    assert q["observed_count"] == 3
    assert q["expected_completed_count"] == 4
    assert q["unavailable_completed_count"] == 1
    assert q["coverage_ratio"] == .75
    assert q["calendar_id"] == snapshot.calendars[0].id
    assert q["latest_available_at"] < snapshot.bars[3].available_at.isoformat()


def test_resample_requires_all_components_and_preserves_availability_and_lineage():
    s = fixture()
    derived, report = resample_snapshot(s, symbol="AAPL", source_timeframe="15m", target_timeframe="1h")
    rows = [b for b in derived.bars if b.timeframe == "1h"]
    assert len(rows) == 2
    assert (rows[0].open, rows[0].high, rows[0].low, rows[0].close, rows[0].volume) == (10., 15., 9., 14., 400.)
    assert rows[0].available_at == s.bars[3].available_at
    assert rows[0].availability_policy == "derived_constituent_availability"
    assert derived.lineage.parent_snapshot_id == s.id
    assert report["emitted_count"] == 2
    incomplete = Snapshot([b for b in s.bars if b.start_time != s.bars[2].start_time], calendar_version="test", calendars=s.calendars)
    derived, report = resample_snapshot(incomplete, symbol="AAPL", source_timeframe="15m", target_timeframe="1h")
    assert len([b for b in derived.bars if b.timeframe == "1h"]) == 1
    assert report["incomplete_count"] >= 1


def test_asof_resampling_does_not_use_late_constituents_or_fill_missing_calendar():
    s = fixture()
    derived, report = resample_snapshot(s, symbol="AAPL", source_timeframe="15m", target_timeframe="1h", as_of=s.bars[3].end_time)
    assert derived is None
    assert report["emitted_count"] == 0
    q = data_quality(Snapshot(list(s.bars), calendar_version="test"), symbol="AAPL", timeframe="15m", as_of=s.bars[-1].available_at)
    assert q["calendar_id"] is None
    assert q["coverage_ratio"] is None
    assert q["unavailable_completed_count"] is None


def test_snapshot_registry_roundtrip_calendar_lineage_quality_and_resampling(tmp_path):
    from stock_quant.jobs import JobStore
    s = fixture()
    with JobStore(tmp_path / "quality.db") as store:
        original = store.import_snapshot([b.model_dump(mode="json") for b in s.bars], s.calendar_version, calendars=[c.model_dump(mode="json") for c in s.calendars])
        assert original["data_snapshot_id"] == s.id
        assert store.get_snapshot(s.id).calendars[0].id == s.calendars[0].id
        derived = store.resample(s.id, symbol="AAPL", source_timeframe="15m", target_timeframe="1h")
        new = store.get_snapshot(derived["data_snapshot_id"])
        assert new.lineage.parent_snapshot_id == s.id
        assert len(new.bars) == 10
        assert store.quality(new.id, symbol="AAPL", timeframe="1h", as_of=s.bars[-1].available_at)["observed_count"] == 2


def test_resampling_handles_breaks_short_final_buckets_and_full_week_coverage():
    from stock_quant.calendar import generate_calendar, expected_intervals
    from datetime import date
    def populated(c, period):
        return Snapshot([MarketBar(symbol="TEST", timeframe=period, exchange=c.exchange, start_time=i.start, end_time=i.end, available_at=i.end,
                                  open=100., high=102., low=99., close=101., volume=10., source="golden", dataset_version="v1", adjustment="raw")
                         for i in expected_intervals(c, period)], calendar_version="sessions", calendars=[c])
    calendar = generate_calendar(calendar_name="XHKG", exchange="XHKG", start_date="2026-11-03", end_date="2026-11-03")
    original = populated(calendar, "15m")
    hours, report = resample_snapshot(original, symbol="TEST", source_timeframe="15m", target_timeframe="1h")
    assert report["emitted_count"] == 6
    assert any((b.end_time - b.start_time).total_seconds() == 1800 for b in hours.bars if b.timeframe == "1h")
    day, report = resample_snapshot(original, symbol="TEST", source_timeframe="15m", target_timeframe="1d")
    daily = next(b for b in day.bars if b.timeframe == "1d")
    assert daily.volume == 220
    assert (daily.end_time - daily.start_time).total_seconds() == 6.5 * 3600  # Lunch is not traded.
    us = generate_calendar(calendar_name="XNYS", exchange="XNAS", start_date="2026-11-23", end_date="2026-11-29")
    original = populated(us, "1d")
    week, report = resample_snapshot(original, symbol="TEST", source_timeframe="1d", target_timeframe="1w")
    assert report["emitted_count"] == 1
    assert next(b for b in week.bars if b.timeframe == "1w").volume == 40
    clipped = us.model_copy(update={"end_date": date(2026, 11, 27)})
    original = Snapshot(list(original.bars), calendar_version="clipped", calendars=[clipped])
    assert resample_snapshot(original, symbol="TEST", source_timeframe="1d", target_timeframe="1w")[0] is None


def test_internal_splits_and_wrong_grid_observations_cannot_be_silently_aggregated():
    import pytest
    s = fixture()
    split = {"kind": "split", "id": "intraday", "symbol": "AAPL", "ratio": 2., "available_at": s.bars[0].start_time,
             "effective_at": s.bars[1].start_time}
    revised = Snapshot(list(s.bars), calendar_version=s.calendar_version, calendars=s.calendars, actions=[split])
    result, report = resample_snapshot(revised, symbol="AAPL", source_timeframe="15m", target_timeframe="1h")
    assert report["emitted_count"] == 1 and report["split_blocked_count"] == 1
    for target in ("5m", "15m"):
        with pytest.raises(ValueError, match="TIMEFRAME_MISMATCH"):
            resample_snapshot(s, symbol="AAPL", source_timeframe="15m", target_timeframe=target)
    shifted = [*s.bars[:-1], s.bars[-1].model_copy(update={"start_time": s.bars[-1].start_time + timedelta(minutes=1),
                                                        "end_time": s.bars[-1].end_time + timedelta(minutes=1),
                                                        "available_at": s.bars[-1].available_at + timedelta(minutes=1)})]
    revised = Snapshot(shifted, calendar_version=s.calendar_version, calendars=s.calendars)
    result, report = resample_snapshot(revised, symbol="AAPL", source_timeframe="15m", target_timeframe="1h")
    assert report["emitted_count"] == 1 and report["unrecognized_source_count"] == 1
    quality = data_quality(revised, symbol="AAPL", timeframe="15m", as_of=shifted[-1].available_at)
    assert quality["out_of_schedule_count"] == 1 and quality["unavailable_completed_count"] == 1
