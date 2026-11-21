from datetime import date, timedelta

import pytest

import exchange_calendars as xc

from stock_quant.calendar import CalendarSchedule, generate_calendar, generate_long_calendar


def test_long_calendar_splits_and_merges_without_duplicates():
    start, end = date(2008, 1, 1), date(2026, 11, 18)
    calendar = generate_long_calendar(calendar_name="XNYS", exchange="XNAS", start_date=start, end_date=end)
    reference = generate_calendar(calendar_name="XNYS", exchange="XNAS", start_date=date(2011, 1, 1), end_date=date(2015, 12, 31))
    overlap = [session for session in calendar.sessions if reference.start_date <= session.date <= reference.end_date]
    assert overlap == list(reference.sessions)
    assert calendar.start_date == start
    assert calendar.end_date == end
    assert len(calendar.long_calendar_shards) > 1
    assert calendar.calendar_validation_status == "MULTI_SHARD_VERIFIED"
    assert calendar.exchange == "XNAS"
    assert calendar.provider_version == "4.13.2"


def test_calendar_builds_exactly_one_provider_schedule_per_shard(monkeypatch):
    calls = []
    provider = xc.get_calendar

    def record(name, *, start, end):
        calls.append((start, end))
        return provider(name, start=start, end=end)

    monkeypatch.setattr(xc, "get_calendar", record)
    calendar = generate_long_calendar(calendar_name="XNYS", exchange="XNAS",
                                      start_date="2002-01-02", end_date="2013-01-03")
    assert calls == [(s.start_date - timedelta(days=7), s.end_date + timedelta(days=7)) for s in calendar.shards]


def test_long_calendar_shards_do_not_overlap_or_repeat_sessions():
    start, end = date(2008, 1, 1), date(2026, 11, 18)
    calendar = generate_long_calendar(calendar_name="XNYS", exchange="XNAS", start_date=start, end_date=end)
    dates = [session.date for session in calendar.sessions]
    assert len(dates) == len(set(dates))
    shards = calendar.long_calendar_shards
    assert all(shard["start"] <= shard["end"] for shard in shards)
    for first, second in zip(shards, shards[1:]):
        assert date.fromisoformat(first["end"]) + timedelta(days=1) == date.fromisoformat(second["start"])


@pytest.mark.parametrize("start,calendar_name,exchange", [
    (date(2002, 1, 1), "XNYS", "XNYS"),
    (date(1990, 1, 1), "XNYS", "XNYS"),
    (date(2002, 1, 1), "XNAS", "XNAS"),
])
def test_yahoo_calendar_matches_provider_without_oversized_schedule(start, calendar_name, exchange, tmp_path, monkeypatch):
    import sqlite3

    from stock_quant.alpha.contracts import StockDatasetRequest
    from stock_quant.alpha.data.catalog import DatasetStore
    from stock_quant.alpha.data.session_coverage import mapped_stock_calendar
    from stock_quant.alpha.data.stock_dataset import prepare_stock_dataset
    from stock_quant.alpha.data.yahoo_stock_adapter import YahooStockAdapter

    end = date(2012, 1, 1)
    provider_factory = xc.get_calendar
    oracle = provider_factory("XNYS", start=start - timedelta(days=7), end=end + timedelta(days=7))
    expected = {label.date(): (row.open.to_pydatetime(), row.close.to_pydatetime())
                for label, row in oracle.schedule.iterrows() if start <= label.date() <= end}

    def bounded(name, *, start, end):
        assert (date.fromisoformat(str(end)[:10]) - date.fromisoformat(str(start)[:10])).days <= 3674
        return provider_factory(name, start=start, end=end)

    monkeypatch.setattr(xc, "get_calendar", bounded)
    db = tmp_path / "stocks.db"
    with sqlite3.connect(db) as conn:
        conn.executescript("""
            CREATE TABLE instruments(id INTEGER PRIMARY KEY,symbol,exchange,currency);
            CREATE TABLE daily_bars(instrument_id,trade_date,open,high,low,close,volume,source);
            INSERT INTO instruments VALUES (1,'AAPL','NASDAQ','USD');
        """)
        conn.executemany("INSERT INTO daily_bars VALUES (1,?,50,55,45,51,100,'yahoo')",
                         [(day.isoformat(),) for day in expected if day < end])
    request = StockDatasetRequest(source="yahoo", symbols=["AAPL"],
                                  start=f"{start}T00:00:00Z", end=f"{end}T00:00:00Z")
    store = DatasetStore(tmp_path / "data")
    provider = YahooStockAdapter(db, calendar_name=calendar_name, exchange=exchange)
    manifest = prepare_stock_dataset(request, store=store, provider=provider)
    calendar = mapped_stock_calendar(store.load_manifest(manifest.dataset_id))
    assert (calendar.exchange, calendar.id) == (exchange, manifest.data_quality["calendar_id"])
    actual = {s.date: (s.intervals[0].start, s.intervals[-1].end) for s in calendar.sessions}
    assert actual == expected
    assert sum(p.row_count for p in manifest.partition_manifest) == sum(day < end for day in expected)
    assert manifest.data_quality["exchange_mappings"]["AAPL"]["early_close_dates"] == [
        str(day.date()) for day in oracle.early_closes if start <= day.date() <= end]
    if start.year == 1990:
        assert not isinstance(calendar, CalendarSchedule)
        assert len(calendar.sessions) > 5000
        assert manifest.data_quality["calendar_shards"] == list(calendar.long_calendar_shards)
    else:
        assert isinstance(calendar, CalendarSchedule)
        if exchange == "XNYS":
            assert "calendar_shards" not in manifest.data_quality
        else:
            assert manifest.data_quality["calendar_shards"] == list(calendar.long_calendar_shards)


@pytest.mark.parametrize("day,open_utc,close_utc", [
    ("1990-12-27", "16:00", "21:00"),
    ("1991-01-17", "14:31", "21:00"),
    ("1991-02-25", "14:31", "21:00"),
    ("1994-02-11", "14:30", "19:30"),
    ("1995-12-18", "15:30", "21:00"),
    ("1996-01-08", "16:00", "19:00"),
    ("1997-10-27", "14:30", "20:30"),
    ("1999-12-31", "14:30", "18:00"),
    ("2001-09-17", "13:33", "20:00"),
    ("2002-09-11", "16:00", "20:00"),
    ("2005-06-01", "13:30", "19:56"),
    ("2009-07-02", "13:30", "20:00"),
])
def test_xnys_daily_outer_bounds_from_exchange_records(day, open_utc, close_utc):
    kwargs = dict(calendar_name="XNYS", exchange="XNYS", start_date=day, end_date=day)
    legacy = generate_long_calendar(**kwargs)
    revised = generate_long_calendar(**kwargs, xnys_daily_bounds=True)
    session, = revised.sessions
    assert (session.intervals[0].start.strftime("%H:%M"), session.intervals[-1].end.strftime("%H:%M")) == (open_utc, close_utc)
    assert revised.id != legacy.id
    assert revised.version == legacy.version + "|xnys-daily-bounds-v1"
    assert generate_calendar(**kwargs).id == legacy.id


@pytest.mark.parametrize("calendar_name,exchange", [("XNYS", "XNAS"), ("XNAS", "XNYS")])
def test_xnys_bounds_reject_provider_aliases(calendar_name, exchange):
    with pytest.raises(ValueError, match="requires an exact XNYS mapping"):
        generate_long_calendar(calendar_name=calendar_name, exchange=exchange,
                               start_date="2002-09-11", end_date="2002-09-11", xnys_daily_bounds=True)


@pytest.mark.parametrize("start,offset", [("1990-01-01", "Z"), ("2002-01-01", "Z"), ("2002-09-11", "+08:00")])
def test_yahoo_versioned_xnys_bounds_round_trip(start, offset, tmp_path):
    import sqlite3

    from stock_quant.alpha.contracts import StockDatasetRequest
    from stock_quant.alpha.data.catalog import DatasetStore
    from stock_quant.alpha.data.session_coverage import mapped_stock_calendar
    from stock_quant.alpha.data.stock_dataset import prepare_stock_dataset
    from stock_quant.alpha.data.yahoo_stock_adapter import YahooStockAdapter
    from stock_quant.alpha.errors import AlphaError

    db = tmp_path / "stocks.db"
    with sqlite3.connect(db) as conn:
        conn.executescript("""
            CREATE TABLE instruments(id INTEGER PRIMARY KEY,symbol,exchange,currency);
            CREATE TABLE daily_bars(instrument_id,trade_date,open,high,low,close,volume,source);
            INSERT INTO instruments VALUES (1,'XNY','NYSE','USD'), (2,'XNA','NASDAQ','USD');
            INSERT INTO daily_bars VALUES (1,'2002-09-11',50,55,45,51,100,'yahoo'),
                (1,'2005-06-01',50,55,45,51,100,'yahoo'), (2,'2002-09-11',50,55,45,51,100,'yahoo');
        """)
    request = StockDatasetRequest(symbols=["XNY"], start=f"{start}T00:00:00{offset}", end=f"2012-01-01T00:00:00{offset}")
    provider = YahooStockAdapter(db)
    store = DatasetStore(tmp_path / "data")
    legacy = prepare_stock_dataset(request, store=store, provider=provider)
    opt_in = request.model_copy(update={"calendar_version": "xnys-daily-bounds-v1"})
    revised = prepare_stock_dataset(opt_in, store=store, provider=provider)
    assert revised.dataset_id != legacy.dataset_id
    before, after = (mapped_stock_calendar(store.load_manifest(m.dataset_id)) for m in (legacy, revised))
    assert [s.date for s in before.sessions] == [s.date for s in after.sessions]
    assert after.id == revised.data_quality["calendar_id"]
    assert (after.start_date, after.end_date) == (date.fromisoformat(start), date(2012, 1, 1))
    assert after.version == revised.calendar_version
    assert all(s.version == after.version for s in getattr(after, "shards", (after,)))
    corrupt = revised.model_copy(update={"data_quality": {**revised.data_quality, "calendar_id": before.id}})
    with pytest.raises(AlphaError, match="calendar identity"):
        mapped_stock_calendar(corrupt)
    rows = {row.start_at.date().isoformat(): row for row in provider.read(opt_in).rows}
    assert rows["2002-09-11"].start_at.strftime("%H:%M") == "16:00"
    assert rows["2005-06-01"].end_at.strftime("%H:%M") == "19:56"
    assert rows["2005-06-01"].available_at.strftime("%H:%M") == "20:01"
    assert "2005-06-01" in revised.data_quality["exchange_mappings"]["XNY"]["early_close_dates"]
    assert "2005-06-01" not in legacy.data_quality["exchange_mappings"]["XNY"]["early_close_dates"]
    if start == "1990-01-01":
        assert len(after.long_calendar_shards) == 23
        proxy = prepare_stock_dataset(request, store=store, provider=YahooStockAdapter(db, exchange="XNAS"))
        proxy_calendar = mapped_stock_calendar(store.load_manifest(proxy.dataset_id))
        assert proxy_calendar.exchange == "XNAS"
        assert proxy_calendar.id == proxy.data_quality["calendar_id"]
    for symbols in (["XNA"], ["XNY", "XNA"]):
        with pytest.raises(AlphaError, match="requires an exact XNYS mapping"):
            provider.read(opt_in.model_copy(update={"symbols": symbols}))
