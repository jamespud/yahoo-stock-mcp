from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from stock_quant.alpha.capabilities import AlphaCapabilities
from stock_quant.alpha.contracts import (
    DatasetKind,
    DatasetManifest,
    DatasetPartition,
    StockDatasetRequest,
)
from stock_quant.alpha.errors import AlphaError, AlphaErrorCode


def test_dataset_contracts_are_strict_and_stable():
    partition = DatasetPartition(
        ordinal=0,
        logical_path="bars/symbol=AAPL/part-00000.parquet",
        format="parquet-v1",
        content_hash="sha256:" + "a" * 64,
        schema_hash="sha256:" + "b" * 64,
        row_count=10,
        byte_count=1024,
    )
    manifest = DatasetManifest(
        dataset_id="sha256:" + "c" * 64,
        schema_version="1.0",
        dataset_kind="stock_bars",
        source="yahoo",
        source_version="local-v1",
        calendar_version="XNYS-2026.1",
        universe_definition={"mode": "explicit", "symbols": ["AAPL"]},
        instrument_ids=["AAPL"],
        start_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        end_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        captured_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
        availability_policy="modeled_bar_end_plus_delay",
        price_basis="price_basis_unverified",
        data_quality={"coverage": 1.0},
        partition_manifest=[partition],
        content_hash="sha256:" + "d" * 64,
        parent_dataset_ids=[],
    )
    assert manifest.dataset_kind == "stock_bars"
    assert set(DatasetKind.__args__) == {"stock_bars", "option_quotes", "feature_panel", "prediction_panel"}
    with pytest.raises(ValidationError):
        DatasetManifest.model_validate({**manifest.model_dump(), "unexpected": True})


def test_stock_dataset_request_rejects_bad_period_and_price_basis():
    request = StockDatasetRequest(
        source="yahoo",
        symbols=["AAPL", "MSFT"],
        start=datetime(2024, 1, 1, tzinfo=timezone.utc),
        end=datetime(2025, 1, 1, tzinfo=timezone.utc),
        price_basis="price_basis_unverified",
    )
    assert request.symbols == ["AAPL", "MSFT"]
    with pytest.raises(ValidationError):
        StockDatasetRequest.model_validate({**request.model_dump(), "end": request.start})
    with pytest.raises(ValidationError):
        StockDatasetRequest.model_validate({**request.model_dump(), "price_basis": "guessed"})


def test_alpha_error_serializes_stable_code_and_context():
    error = AlphaError(AlphaErrorCode.DATA_UNAVAILABLE, "No option history", {"provider": "thetadata"})
    assert error.to_dict() == {
        "code": "DATA_UNAVAILABLE",
        "message": "No option history",
        "context": {"provider": "thetadata"},
    }
    assert str(error) == "DATA_UNAVAILABLE: No option history"


def test_capabilities_fail_closed_without_optional_dependencies():
    capabilities = AlphaCapabilities.detect(
        modules={"polars": False, "duckdb": False, "lightgbm": False, "sklearn": False, "scipy": False, "QuantLib": False, "httpx": False},
        provider_coverage={"thetadata": {"status": "UNAVAILABLE", "reason": "credentials not configured"}},
    )
    assert capabilities.stock_alpha_enabled is True
    assert capabilities.option_snapshot_enabled is True
    assert capabilities.historical_options_enabled is False
    assert capabilities.option_pricing_enabled is False
    assert capabilities.option_backtesting_enabled is False
    assert capabilities.lightgbm_enabled is False
    assert capabilities.supported_model_types == []
    assert capabilities.provider_coverage["thetadata"]["status"] == "UNAVAILABLE"
    assert capabilities.supported_factor_operators
    assert capabilities.supported_label_types == ["stock_forward_return", "option_forward_return", "option_future_iv_change"]


def test_capabilities_enable_only_verified_provider_and_installed_runtime():
    capabilities = AlphaCapabilities.detect(
        modules={"polars": True, "duckdb": True, "lightgbm": True, "sklearn": True, "scipy": True, "QuantLib": True, "httpx": True},
        provider_coverage={"thetadata": {"status": "AVAILABLE", "coverage": ["EOD_OPTIONS"]}},
    )
    assert capabilities.historical_options_enabled is True
    assert capabilities.option_pricing_enabled is True
    assert capabilities.option_backtesting_enabled is True
    assert capabilities.lightgbm_enabled is True
    assert capabilities.supported_model_types == ["lightgbm_regressor"]


def _stock_rows(symbols: list[str], periods: int = 20, source_version: str = "local-v1"):
    rows = []
    for symbol_index, symbol in enumerate(symbols):
        start = datetime(2024, 1, 1, 14, 30, tzinfo=timezone.utc)
        for day in range(periods):
            at = start.fromtimestamp(start.timestamp() + day * 86400, tz=timezone.utc)
            close = 100.0 + symbol_index + day / 100
            rows.append({
                "instrument_id": symbol,
                "start_at": at,
                "end_at": at.replace(hour=21, minute=0),
                "available_at": at.replace(hour=21, minute=5),
                "open": close - 0.2,
                "high": close + 0.5,
                "low": close - 0.5,
                "close": close,
                "volume": 1000 + day,
                "adjustment": "raw",
                "source": "yahoo",
                "source_version": source_version,
                "currency": "USD",
            })
    return rows


def test_dataset_publication_is_stable_atomic_and_integrity_checked(tmp_path):
    from stock_quant.alpha.data.catalog import DatasetStore

    request = StockDatasetRequest(
        source="yahoo", symbols=["AAPL", "MSFT"],
        start=datetime(2024, 1, 1, tzinfo=timezone.utc), end=datetime(2025, 1, 1, tzinfo=timezone.utc),
        price_basis="price_basis_unverified",
    )
    rows = _stock_rows(request.symbols)
    store = DatasetStore(tmp_path / "alpha")
    first = store.publish_stock(request, rows, calendar_version="XNYS-2024", source_version="local-v1",
                                data_quality={"coverage": 1.0})
    second = store.publish_stock(request, list(reversed(rows)), calendar_version="XNYS-2024", source_version="local-v1",
                                 data_quality={"coverage": 1.0})
    assert first.dataset_id == second.dataset_id
    assert first.content_hash == second.content_hash
    loaded = store.load_manifest(first.dataset_id)
    assert loaded.dataset_id == first.dataset_id
    assert len(loaded.partition_manifest) == 2
    assert store.validate_dataset(first.dataset_id).valid is True

    partition = store.root / "datasets" / first.dataset_id.removeprefix("sha256:") / first.partition_manifest[0].logical_path
    with partition.open("ab") as stream:
        stream.write(b"corruption")
    report = store.validate_dataset(first.dataset_id)
    assert report.valid is False
    assert any(issue["code"] == "DATASET_MISMATCH" for issue in report.issues)


def test_dataset_publication_rejects_traversal_and_hides_aborted_stage(tmp_path):
    from stock_quant.alpha.data.catalog import DatasetStore

    store = DatasetStore(tmp_path / "alpha")
    request = StockDatasetRequest(
        source="yahoo", symbols=["AAPL"],
        start=datetime(2024, 1, 1, tzinfo=timezone.utc), end=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    rows = _stock_rows(["AAPL"])
    def cancelled(_progress):
        raise RuntimeError("cancel")
    with pytest.raises(RuntimeError, match="cancel"):
        store.publish_stock(request, rows, calendar_version="XNYS-2024", source_version="local-v1",
                            data_quality={}, checkpoint=cancelled)
    assert list((store.root / "datasets").glob("sha256-*")) == []

    with pytest.raises(ValidationError):
        StockDatasetRequest.model_validate({**request.model_dump(), "symbols": ["../escape"]})
    with pytest.raises(ValueError):
        store.load_manifest("../escape")


def test_dataset_supports_more_than_legacy_ten_thousand_bars(tmp_path):
    from stock_quant.alpha.data.catalog import DatasetStore

    symbols = [f"S{i:03d}" for i in range(100)]
    request = StockDatasetRequest(
        source="yahoo", symbols=symbols,
        start=datetime(2024, 1, 1, tzinfo=timezone.utc), end=datetime(2027, 1, 1, tzinfo=timezone.utc),
    )
    rows = _stock_rows(symbols, periods=756)
    store = DatasetStore(tmp_path / "alpha")
    manifest = store.publish_stock(request, rows, calendar_version="XNYS-2024", source_version="local-v1", data_quality={})
    assert sum(part.row_count for part in manifest.partition_manifest) == 75_600
    assert len(store.read_rows(manifest.dataset_id, columns=["instrument_id"])) == 75_600


def test_prepare_stock_dataset_preserves_quality_and_rejects_missing_symbol(tmp_path):
    from stock_quant.alpha.data.catalog import DatasetStore
    from stock_quant.alpha.data.stock_dataset import StockHistoryBatch, prepare_stock_dataset
    from stock_quant.alpha.errors import AlphaError, AlphaErrorCode

    class Provider:
        def read(self, request):
            rows = _stock_rows(request.symbols, source_version="fixture-v1")
            return StockHistoryBatch(
                rows=rows, calendar_version="XNYS-test", source_version="fixture-v1",
                quality={"missing_symbols": [], "source": "fixture"},
            )

    class MissingProvider(Provider):
        def read(self, request):
            rows = _stock_rows([request.symbols[0]], source_version="fixture-v1")
            return StockHistoryBatch(
                rows=rows, calendar_version="XNYS-test", source_version="fixture-v1",
                quality={"missing_symbols": request.symbols[1:]},
            )

    request = StockDatasetRequest(
        source="yahoo", symbols=["AAPL", "MSFT"],
        start=datetime(2024, 1, 1, tzinfo=timezone.utc), end=datetime(2025, 1, 1, tzinfo=timezone.utc),
        price_basis="price_basis_unverified",
    )
    store = DatasetStore(tmp_path / "alpha")
    manifest = prepare_stock_dataset(request, store=store, provider=Provider())
    assert manifest.dataset_kind == "stock_bars"
    assert manifest.data_quality["missing_symbols"] == []
    with pytest.raises(AlphaError) as info:
        prepare_stock_dataset(request, store=store, provider=MissingProvider())
    assert info.value.code == AlphaErrorCode.DATA_UNAVAILABLE


def test_yahoo_stock_adapter_reads_local_database_without_http(tmp_path):
    import sqlite3
    from stock_quant.alpha.data.yahoo_stock_adapter import YahooStockAdapter

    database = tmp_path / "market.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript('''
      CREATE TABLE instruments(id INTEGER PRIMARY KEY, symbol TEXT, exchange TEXT, currency TEXT, yahoo_symbol TEXT);
      CREATE TABLE daily_bars(instrument_id INTEGER, trade_date TEXT, open TEXT, high TEXT, low TEXT, close TEXT, adj_close TEXT, volume INTEGER, source TEXT);
      INSERT INTO instruments VALUES(1,'AAPL','NMS','USD','AAPL');
      INSERT INTO daily_bars VALUES(1,'2024-01-02','100','102','99','101','101','1000','yahoo');
      INSERT INTO daily_bars VALUES(1,'2024-01-03','101','103','100','102','102','1100','yahoo');
    ''')
    connection.commit()
    connection.close()
    adapter = YahooStockAdapter(database, publication_delay_seconds=300)
    request = StockDatasetRequest(
        source="yahoo", symbols=["AAPL"],
        start=datetime(2024, 1, 1, tzinfo=timezone.utc), end=datetime(2024, 2, 1, tzinfo=timezone.utc),
        price_basis="price_basis_unverified",
    )
    batch = adapter.read(request)
    assert len(batch.rows) == 2
    assert batch.rows[0].instrument_id == "AAPL"
    assert batch.rows[0].available_at > batch.rows[0].end_at
    assert batch.quality["price_basis_unverified"] is True


def test_manifest_tampering_changes_dataset_identity_and_fails_validation(tmp_path):
    from stock_quant.alpha.data.catalog import DatasetStore
    store = DatasetStore(tmp_path / "alpha")
    request = StockDatasetRequest(source="yahoo", symbols=["AAPL"], start=datetime(2024, 1, 1, tzinfo=timezone.utc), end=datetime(2024, 1, 10, tzinfo=timezone.utc))
    manifest = store.publish_stock(request, _stock_rows(["AAPL"]), calendar_version="fixture", source_version="local-v1", data_quality={})
    path = store.datasets / manifest.dataset_id.removeprefix("sha256:") / "manifest.json"
    payload = __import__("json").loads(path.read_text())
    payload["source"] = "forged"
    path.write_text(__import__("json").dumps(payload))
    report = store.validate_dataset(manifest.dataset_id)
    assert report.valid is False
    assert any("Manifest identity mismatch" in issue["message"] for issue in report.issues)


def test_deferred_dataset_is_hidden_until_explicit_publication(tmp_path):
    from stock_quant.alpha.data.catalog import DatasetStore
    store = DatasetStore(tmp_path / "alpha", defer_registration=True)
    request = StockDatasetRequest(source="yahoo", symbols=["AAPL"], start=datetime(2024, 1, 1, tzinfo=timezone.utc), end=datetime(2024, 1, 10, tzinfo=timezone.utc))
    manifest = store.publish_stock(request, _stock_rows(["AAPL"]), calendar_version="fixture", source_version="local-v1", data_quality={})
    with __import__("pytest").raises(KeyError):
        store.load_manifest(manifest.dataset_id)
    store.register(manifest)
    assert store.load_manifest(manifest.dataset_id).dataset_id == manifest.dataset_id
