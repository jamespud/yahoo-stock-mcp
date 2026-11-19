from datetime import datetime, timezone

from stock_quant.alpha.data.price_attestation import PriceReference, attest_price_dataset

START = datetime(2024, 1, 1, tzinfo=timezone.utc)
END = datetime(2024, 1, 10, tzinfo=timezone.utc)


def _row(day, open_price, *, adjustment="raw"):
    return {
        "instrument_id": "AAPL", "start_at": datetime(2024, 1, day, 14, 30, tzinfo=timezone.utc),
        "end_at": datetime(2024, 1, day, 21, 0, tzinfo=timezone.utc),
        "available_at": datetime(2024, 1, day, 21, 5, tzinfo=timezone.utc),
        "open": open_price, "high": open_price + 1, "low": open_price - 1,
        "close": open_price, "volume": 100, "adjustment": adjustment,
        "source": "yahoo", "source_version": "fixture", "currency": "USD",
    }


def _reference(day, reference_open, *, basis="raw", evidence_id="ref-1"):
    at = datetime(2024, 1, day, 14, 30, tzinfo=timezone.utc)
    return PriceReference(
        instrument_id="AAPL", start_at=at,
        declared_basis=basis,
        reference_open=reference_open,
        reference_high=reference_open + 1,
        reference_low=reference_open - 1,
        reference_close=reference_open,
        source="independent-reference",
        source_version="v1",
        evidence_id=evidence_id,
    )


def test_adjusted_price_cannot_be_attested_as_raw():
    attestation = attest_price_dataset(
        "sha256:" + "1" * 64,
        [_row(2, 101.0, adjustment="split")],
        [_reference(2, 101.0, basis="adjusted")],
        start_at=START,
        end_at=END,
        source="yahoo",
        source_version="fixture",
        calendar_identity={"calendar_name": "XNYS", "calendar_version": "v1"},
    )
    assert attestation.attestation_status == "BLOCKED"
    assert attestation.price_basis == "price_basis_unverified"
    assert attestation.verified_symbols == []


def test_unverified_price_stays_blocked():
    attestation = attest_price_dataset(
        "sha256:" + "1" * 64,
        [_row(2, 100.0)],
        [],
        start_at=START,
        end_at=END,
        source="yahoo",
        source_version="fixture",
        calendar_identity={"calendar_name": "XNYS", "calendar_version": "v1"},
    )
    assert attestation.attestation_status == "BLOCKED"
    assert attestation.unverified_symbols == ["AAPL"]
    assert attestation.reference_evidence_ids == []


def test_raw_reference_verifies_scoped_symbols():
    attestation = attest_price_dataset(
        "sha256:" + "1" * 64,
        [_row(2, 100.0), _row(3, 101.0)],
        [_reference(2, 100.0), _reference(3, 101.0, evidence_id="ref-2")],
        start_at=START,
        end_at=END,
        source="yahoo",
        source_version="fixture",
        calendar_identity={"calendar_name": "XNYS", "calendar_version": "v1"},
    )
    assert attestation.attestation_status == "VERIFIED"
    assert attestation.price_basis == "raw_attested"
    assert attestation.verified_symbols == ["AAPL"]
    assert attestation.verified_scopes[0]["start_at"] == "2024-01-02T14:30:00+00:00"
    assert attestation.reference_evidence_ids == ["ref-1", "ref-2"]


def test_declared_raw_basis_does_not_attest_local_dataset(tmp_path):
    import sqlite3
    from stock_quant.alpha.contracts import StockDatasetRequest
    from stock_quant.alpha.data.yahoo_stock_adapter import YahooStockAdapter

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
    batch = YahooStockAdapter(database).read(StockDatasetRequest(
        source="yahoo", symbols=["AAPL"], start=START, end=END, price_basis="raw_attested",
    ))
    assert batch.quality["price_basis_declared"] == "raw_attested"
    assert batch.quality["price_basis_unverified"] is True
    assert batch.quality["price_attestation_status"] == "BLOCKED"
