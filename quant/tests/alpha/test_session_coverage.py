from datetime import date, datetime, time, timezone

import pytest

from stock_quant.alpha.contracts import StockDatasetRequest
from stock_quant.alpha.data.catalog import DatasetStore
from stock_quant.alpha.data.session_coverage import audit_session_coverage
from stock_quant.alpha.data.stock_dataset import StockHistoryBatch, prepare_stock_dataset
from stock_quant.alpha.data.partitions import StockBarRow


def _row(symbol: str, session: date, open_price: float = 100.0) -> StockBarRow:
    start = datetime.combine(session, time(14, 30), tzinfo=timezone.utc)
    end = datetime.combine(session, time(21, 0), tzinfo=timezone.utc)
    return StockBarRow(
        instrument_id=symbol,
        start_at=start,
        end_at=end,
        available_at=datetime.combine(session, time(21, 5), tzinfo=timezone.utc),
        open=open_price,
        high=open_price + 1,
        low=open_price - 1,
        close=open_price,
        volume=1000,
        adjustment="raw",
        source="yahoo",
        source_version="fixture-v1",
        currency="USD",
    )


def _request(symbols=("AAPL",), start=date(2024, 1, 1), end=date(2024, 2, 1)):
    return StockDatasetRequest(
        source="yahoo",
        symbols=list(symbols),
        start=datetime.combine(start, time.min, tzinfo=timezone.utc),
        end=datetime.combine(end, time.min, tzinfo=timezone.utc),
        price_basis="price_basis_unverified",
    )


def _prepare(tmp_path, rows, calendar_dates, session_dates_by_symbol=None, symbols=("AAPL",)):
    class Provider:
        def read(self, request):
            return StockHistoryBatch(
                rows=rows,
                calendar_version="XNYS-fixture-v1",
                calendar_id="sha256:" + "c" * 64,
                calendar_session_dates=list(calendar_dates),
                session_dates_by_symbol=session_dates_by_symbol or {},
                source_version="fixture-v1",
            )

    store = DatasetStore(tmp_path / "alpha")
    return prepare_stock_dataset(_request(symbols), store=store, provider=Provider())


def test_symbol_presence_does_not_imply_session_completeness(tmp_path):
    sessions = [date(2024, 1, day) for day in (2, 3, 4, 5, 8)]
    rows = [_row("AAPL", session) for session in sessions if session != date(2024, 1, 4)]
    rows += [_row("MSFT", session) for session in sessions]
    manifest = _prepare(tmp_path, rows, sessions, symbols=("AAPL", "MSFT"))

    quality = manifest.data_quality
    assert quality["coverage"] == 1.0
    assert quality["symbol_presence_coverage"] == 1.0
    assert quality["session_coverage"] == pytest.approx(0.9)
    by_symbol = {row["instrument_id"]: row for row in quality["session_coverage_by_symbol"]}
    assert by_symbol["AAPL"]["session_coverage"] == pytest.approx(0.8)
    assert by_symbol["MSFT"]["session_coverage"] == pytest.approx(1.0)


def test_missing_trading_session_is_reported():
    sessions = [date(2024, 1, day) for day in (2, 3, 4, 5, 8)]
    rows = [_row("AAPL", session) for session in sessions if session != date(2024, 1, 4)]
    report = audit_session_coverage(
        rows,
        requested_symbols=["AAPL"],
        calendar_session_dates=sessions,
        calendar_id="XNYS-fixture-v1",
    )[0]
    assert report["missing_sessions"] == ["2024-01-04"]
    assert report["observed_sessions_within_observed_range"] == 4
    assert report["expected_sessions_within_observed_range"] == 5
    assert report["session_coverage"] == pytest.approx(0.8)


def test_exchange_holiday_is_not_missing_session():
    sessions = [date(2024, 1, day) for day in (2, 3, 5, 8)]
    rows = [_row("AAPL", session) for session in sessions]
    report = audit_session_coverage(
        rows,
        requested_symbols=["AAPL"],
        calendar_session_dates=sessions,
        calendar_id="XNYS-fixture-v1",
    )[0]
    assert report["missing_sessions"] == []
    assert report["session_coverage"] == 1.0


def test_outside_observed_range_is_not_assumed_pre_ipo():
    sessions = [date(2024, 1, day) for day in (2, 3, 4, 5, 8)]
    report = audit_session_coverage(
        [_row("AAPL", session) for session in sessions[2:]],
        requested_symbols=["AAPL"],
        calendar_session_dates=sessions,
        calendar_id="XNYS-fixture-v1",
    )[0]
    assert report["missing_sessions"] == []
    assert report["first_observed_session"] == "2024-01-04"
    assert report["outside_observed_range_status"] == "UNVERIFIED"
    assert "PRE_IPO" not in report


def test_duplicate_and_extra_session_are_reported():
    sessions = [date(2024, 1, day) for day in (2, 3, 4, 5, 8)]
    rows = [_row("AAPL", session) for session in sessions]
    report = audit_session_coverage(
        rows,
        requested_symbols=["AAPL"],
        calendar_session_dates=sessions,
        calendar_id="XNYS-fixture-v1",
        session_dates_by_symbol={"AAPL": [*sessions, date(2024, 1, 3), date(2024, 1, 6)]},
    )[0]
    assert report["duplicate_sessions"] == ["2024-01-03"]
    assert report["extra_sessions"] == ["2024-01-06"]
    assert report["duplicate_session_count"] == 1
    assert report["extra_session_count"] == 1
    assert report["session_status"] == "SESSION_ANOMALIES"
