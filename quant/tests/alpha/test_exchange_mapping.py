import sqlite3
import warnings
from datetime import date, datetime, time, timezone
from pathlib import Path

import exchange_calendars as xc
from pandas.errors import Pandas4Warning

from stock_quant.alpha.contracts import StockDatasetRequest
from stock_quant.alpha.data.exchange_mapping import build_exchange_mapping
from stock_quant.alpha.data.yahoo_stock_adapter import YahooStockAdapter


def test_original_exchange_identity_is_preserved():
    mapping = build_exchange_mapping(
        "AAPL", "NasdaqGS", start_date=date(2024, 1, 1), end_date=date(2024, 12, 31)
    )
    assert mapping["instrument_id"] == "AAPL"
    assert mapping["source_exchange"] == "NasdaqGS"
    assert mapping["calendar_name"] == "XNYS"
    assert mapping["calendar_mapping_status"] == "PROXY_ONLY"


def test_calendar_mapping_is_versioned():
    mapping = build_exchange_mapping(
        "AAPL", "NasdaqGS", start_date=date(2024, 1, 1), end_date=date(2024, 12, 31)
    )
    assert "exchange_calendars:" in mapping["calendar_version"]
    assert mapping["mapping_version"] == "exchange-calendar-mapping-v1"
    evidence = mapping["mapping_evidence"]
    assert evidence["provider"] == "exchange_calendars"
    assert evidence["provider_version"] == "4.13.2"
    assert evidence["requested_calendar_name"] == "XNAS"
    assert evidence["resolved_calendar_name"] == "XNYS"
    assert evidence["alias_resolved"] is True


def test_unsupported_exchange_is_not_silently_mapped():
    mapping = build_exchange_mapping(
        "OTC1", "OTC Pink", start_date=date(2024, 1, 1), end_date=date(2024, 12, 31)
    )
    assert mapping["source_exchange"] == "OTC Pink"
    assert mapping["calendar_name"] is None
    assert mapping["calendar_mapping_status"] == "UNSUPPORTED"
    assert mapping["exchange_identity_status"] == "UNVERIFIED"


def test_exchange_transition_requires_explicit_evidence():
    without_evidence = build_exchange_mapping(
        "MOVE",
        "NasdaqGS",
        start_date=date(2024, 1, 1),
        end_date=date(2024, 12, 31),
        historical_exchanges=["NYSE", "NasdaqGS"],
    )
    assert without_evidence["calendar_mapping_status"] == "EXCHANGE_TRANSITION_UNVERIFIED"
    assert without_evidence["exchange_identity_status"] == "UNVERIFIED"

    with_evidence = build_exchange_mapping(
        "MOVE",
        "NasdaqGS",
        start_date=date(2024, 1, 1),
        end_date=date(2024, 12, 31),
        historical_exchanges=["NYSE", "NasdaqGS"],
        transition_evidence={"source": "listing-history-fixture", "effective_dates": ["2011-01-03"]},
    )
    assert with_evidence["calendar_mapping_status"] == "PROXY_ONLY"
    assert with_evidence["exchange_identity_status"] == "VERIFIED_WITH_TRANSITION_EVIDENCE"
    assert with_evidence["mapping_evidence"]["transition_evidence"]["source"] == "listing-history-fixture"


def test_holiday_and_early_close_schedule_matches_oracle():
    mapping = build_exchange_mapping(
        "JPM", "NYSE", start_date=date(2024, 1, 1), end_date=date(2024, 12, 31)
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", Pandas4Warning)
        oracle = xc.get_calendar("XNYS", start="2024-01-01", end="2024-12-31")
        early_closes = oracle.early_closes
    assert mapping["session_dates"] == [str(day.date()) for day in oracle.sessions]
    assert mapping["early_close_dates"] == [str(day.date()) for day in early_closes]
    assert mapping["mapping_evidence"]["session_count"] == len(oracle.sessions)
    assert mapping["mapping_evidence"]["early_close_count"] == len(early_closes)


def test_adapter_reads_local_exchange_and_publishes_mapping_metadata(tmp_path):
    database = tmp_path / "stocks.db"
    connection = sqlite3.connect(database)
    connection.executescript("""
      CREATE TABLE instruments (id INTEGER PRIMARY KEY, symbol TEXT, exchange TEXT, currency TEXT);
      CREATE TABLE daily_bars (instrument_id INTEGER, trade_date TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL, source TEXT);
      INSERT INTO instruments VALUES(1,'AAPL','NasdaqGS','USD');
      INSERT INTO daily_bars VALUES(1,'2024-01-02',100,102,99,101,1000,'yahoo');
    """)
    connection.commit()
    connection.close()
    request = StockDatasetRequest(
        source="yahoo",
        symbols=["AAPL"],
        start=datetime(2024, 1, 1, tzinfo=timezone.utc),
        end=datetime(2024, 2, 1, tzinfo=timezone.utc),
        price_basis="price_basis_unverified",
    )
    batch = YahooStockAdapter(database).read(request)
    mapping = batch.exchange_mappings["AAPL"]
    assert mapping["source_exchange"] == "NasdaqGS"
    assert mapping["calendar_mapping_status"] == "PROXY_ONLY"
    assert mapping["calendar_version"] == batch.calendar_version
