from datetime import datetime, timedelta, timezone

from stock_quant.alpha.contracts import StockDatasetRequest
from stock_quant.alpha.data.catalog import DatasetStore
from stock_quant.alpha.data.stock_dataset import StockHistoryBatch, prepare_stock_dataset
from stock_quant.alpha.factors.ast import FactorSpec
from stock_quant.alpha.factors.compiler import compile_factor_spec
from stock_quant.alpha.factors.engine import compute_stock_factors


def _rows(extra_future=False):
    rows = []
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    for symbol, base in (("AAPL", 100.0), ("MSFT", 200.0)):
        for day in range(30):
            at = start + timedelta(days=day)
            close = base + day
            rows.append({"instrument_id": symbol, "start_at": at, "end_at": at.replace(hour=6), "available_at": at.replace(hour=6, minute=1), "open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 1000, "adjustment": "raw", "source": "fixture", "source_version": "v1", "currency": "USD"})
    if extra_future:
        for day in range(30, 35):
            at = start + timedelta(days=day)
            for symbol, base in (("AAPL", 100.0), ("MSFT", 200.0)):
                close = (base + day) * 1000
                rows.append({"instrument_id": symbol, "start_at": at, "end_at": at.replace(hour=6), "available_at": at.replace(hour=6, minute=1), "open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 1000, "adjustment": "raw", "source": "fixture", "source_version": "v1", "currency": "USD"})
    return rows


def test_future_mutation_does_not_change_past_features(tmp_path):
    store = DatasetStore(tmp_path / "alpha")
    request = StockDatasetRequest(source="fixture", symbols=["AAPL", "MSFT"], start=datetime(2024, 1, 1, tzinfo=timezone.utc), end=datetime(2024, 2, 1, tzinfo=timezone.utc))
    class Provider:
        def __init__(self, rows): self.rows = rows
        def read(self, request): return StockHistoryBatch(rows=self.rows, calendar_version="fixture", source_version="v1", quality={})
    dataset = prepare_stock_dataset(request, store=store, provider=Provider(_rows()))
    compiled = compile_factor_spec(FactorSpec.model_validate({"schema_version": "1.0", "timeframe": "1d", "nodes": [{"id": "ret", "operator": "pct_change", "inputs": ["close"], "params": {"period": 1}}, {"id": "rank", "operator": "cs_rank_pct", "inputs": ["ret"], "params": {}}], "outputs": ["ret", "rank"]}))
    before = compute_stock_factors(dataset, compiled, store=store, rows=_rows())
    after = compute_stock_factors(dataset, compiled, store=store, rows=[row for row in _rows(extra_future=True) if row["end_at"] < datetime(2024, 1, 25, tzinfo=timezone.utc)])
    before_rows_raw = store.read_rows(before.dataset_id)
    assert all(row["feature_available_at"] <= row["decision_at"] for row in before_rows_raw)
    before_rows = {(r["instrument_id"], r["decision_at"], r["factor_name"]): r["factor_value"] for r in before_rows_raw}
    after_rows = {(r["instrument_id"], r["decision_at"], r["factor_name"]): r["factor_value"] for r in store.read_rows(after.dataset_id)}
    for key, value in before_rows.items():
        if key[1] < datetime(2024, 1, 25, tzinfo=timezone.utc):
            assert after_rows[key] == value


def test_cross_section_does_not_include_instrument_before_it_exists(tmp_path):
    store = DatasetStore(tmp_path / "alpha")
    rows = _rows()
    late_day = datetime(2024, 1, 20, 6, tzinfo=timezone.utc)
    late = datetime(2024, 1, 20, tzinfo=timezone.utc)
    late_row = {"instrument_id": "LATE", "start_at": late, "end_at": late_day, "available_at": late_day.replace(minute=1), "open": 10, "high": 11, "low": 9, "close": 10, "volume": 1, "adjustment": "raw", "source": "fixture", "source_version": "v1", "currency": "USD"}
    request = StockDatasetRequest(source="fixture", symbols=["AAPL", "MSFT", "LATE"], start=datetime(2024, 1, 1, tzinfo=timezone.utc), end=datetime(2024, 2, 1, tzinfo=timezone.utc))
    class Provider:
        def read(self, request): return StockHistoryBatch(rows=rows + [late_row], calendar_version="fixture", source_version="v1", quality={})
    dataset = prepare_stock_dataset(request, store=store, provider=Provider())
    compiled = compile_factor_spec(FactorSpec.model_validate({"schema_version": "1.0", "timeframe": "1d", "nodes": [{"id": "rank", "operator": "cs_rank_pct", "inputs": ["close"], "params": {}}], "outputs": ["rank"]}))
    manifest = compute_stock_factors(dataset, compiled, store=store, rows=rows + [late_row])
    feature_rows = store.read_rows(manifest.dataset_id)
    early_late = [r for r in feature_rows if r["instrument_id"] == "LATE" and r["decision_at"] < late_day]
    assert early_late == []
