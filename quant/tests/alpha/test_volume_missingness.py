import sqlite3
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from stock_quant.alpha.contracts import StockDatasetRequest
from stock_quant.alpha.data.catalog import DatasetStore
from stock_quant.alpha.data.yahoo_stock_adapter import YahooStockAdapter
from stock_quant.alpha.factors.ast import FactorSpec
from stock_quant.alpha.factors.compiler import compile_factor_spec
from stock_quant.alpha.factors.engine import compute_stock_factors


def _request(symbols=("AAPL",)):
    return StockDatasetRequest(
        source="yahoo",
        symbols=list(symbols),
        start=datetime(2024, 1, 1, tzinfo=timezone.utc),
        end=datetime(2024, 1, 10, tzinfo=timezone.utc),
        price_basis="price_basis_unverified",
    )


def _adapter_database(tmp_path):
    database = tmp_path / "stocks.db"
    connection = sqlite3.connect(database)
    connection.executescript("""
      CREATE TABLE instruments (id INTEGER PRIMARY KEY, symbol TEXT, exchange TEXT, currency TEXT);
      CREATE TABLE daily_bars (instrument_id INTEGER, trade_date TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL, source TEXT);
      INSERT INTO instruments VALUES(1,'AAPL','NasdaqGS','USD');
      INSERT INTO daily_bars VALUES(1,'2024-01-02',100,102,99,101,NULL,'yahoo');
      INSERT INTO daily_bars VALUES(1,'2024-01-03',101,103,100,102,0,'yahoo');
      INSERT INTO daily_bars VALUES(1,'2024-01-04',102,104,101,103,500,'yahoo');
      INSERT INTO daily_bars VALUES(1,'2024-01-05',103,105,102,104,-1,'yahoo');
    """)
    connection.commit()
    connection.close()
    return database


def test_null_volume_remains_null(tmp_path):
    batch = YahooStockAdapter(_adapter_database(tmp_path)).read(_request())
    assert batch.rows[0].volume is None
    assert batch.quality["volume_null_count"] == 1


def test_zero_volume_remains_zero(tmp_path):
    batch = YahooStockAdapter(_adapter_database(tmp_path)).read(_request())
    assert batch.rows[1].volume == 0.0
    assert batch.quality["volume_zero_count"] == 1


def test_volume_factor_propagates_missing_values(tmp_path):
    store = DatasetStore(tmp_path / "alpha")
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    rows = []
    for day, volume in enumerate([10.0, None, 30.0]):
        at = start + timedelta(days=day)
        rows.append({
            "instrument_id": "AAPL", "start_at": at, "end_at": at + timedelta(hours=6),
            "available_at": at + timedelta(hours=6, minutes=1), "open": 100,
            "high": 101, "low": 99, "close": 100, "volume": volume,
            "adjustment": "raw", "source": "yahoo", "source_version": "v1", "currency": "USD",
        })
    dataset = store.publish_stock(
        _request(), rows, calendar_version="fixture", source_version="v1", data_quality={}
    )
    spec = FactorSpec.model_validate({
        "schema_version": "1.0", "timeframe": "1d",
        "nodes": [{"id": "vmean2", "operator": "rolling_mean", "inputs": ["volume"], "params": {"period": 2}}],
        "outputs": ["vmean2"],
    })
    feature_manifest = compute_stock_factors(dataset, compile_factor_spec(spec), store=store)
    feature_rows = store.read_rows(feature_manifest.dataset_id)
    values = [row["factor_value"] for row in feature_rows]
    assert values[0] is None
    assert values[1] is None
    assert values[2] is None


def test_volume_quality_count_matches_raw_database(tmp_path):
    batch = YahooStockAdapter(_adapter_database(tmp_path)).read(_request())
    quality = batch.quality
    assert quality["volume_null_count"] == 1
    assert quality["volume_zero_count"] == 1
    assert quality["volume_positive_count"] == 1
    assert quality["volume_invalid_count"] == 1
    assert quality["volume_valid_coverage"] == pytest.approx(0.5)
    assert [row.volume for row in batch.rows] == [None, 0.0, 500.0, None]


def test_legacy_stock_dataset_remains_readable(tmp_path):
    store = DatasetStore(tmp_path / "alpha")
    rows = [{
        "instrument_id": "AAPL",
        "start_at": datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc),
        "end_at": datetime(2024, 1, 2, 21, 0, tzinfo=timezone.utc),
        "available_at": datetime(2024, 1, 2, 21, 5, tzinfo=timezone.utc),
        "open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0, "volume": 0.0,
        "adjustment": "raw", "source": "yahoo", "source_version": "yahoo-local-sqlite-v1", "currency": "USD",
    }]
    manifest = store.publish_stock(
        _request(), rows, calendar_version="legacy", source_version="yahoo-local-sqlite-v1", data_quality={}
    )
    loaded = store.read_rows(manifest.dataset_id)
    assert loaded[0]["volume"] == 0.0
    assert pd.isna(loaded[0]["volume"]) is False
