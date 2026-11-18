from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from stock_quant.alpha.factors.ast import FactorSpec
from stock_quant.alpha.factors.compiler import FactorCompileError, compile_factor_spec
from stock_quant.alpha.factors.engine import compute_stock_factors
from stock_quant.alpha.factors.operators import OPERATORS, apply_operator


def _bar_rows(days=40):
    rows = []
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    for symbol, base, step in (("AAPL", 100.0, 1.0), ("MSFT", 200.0, 2.0)):
        for day in range(days):
            decision = start + timedelta(days=day)
            close = base + day * step
            rows.append({
                "instrument_id": symbol,
                "start_at": decision,
                "end_at": decision + timedelta(hours=6),
                "available_at": decision + timedelta(hours=6, minutes=1),
                "open": close - 0.5,
                "high": close + 1.0,
                "low": close - 1.0,
                "close": close,
                "volume": 1000 + day,
                "adjustment": "raw",
                "source": "fixture",
                "source_version": "v1",
                "currency": "USD",
            })
    return rows


def _spec():
    return FactorSpec.model_validate({
        "schema_version": "1.0",
        "timeframe": "1d",
        "nodes": [
            {"id": "ret_1", "operator": "pct_change", "inputs": ["close"], "params": {"period": 1}},
            {"id": "lag_2", "operator": "lag", "inputs": ["close"], "params": {"period": 2}},
            {"id": "logret_1", "operator": "log_return", "inputs": ["close"], "params": {"period": 1}},
            {"id": "mean_3", "operator": "rolling_mean", "inputs": ["close"], "params": {"period": 3}},
            {"id": "std_3", "operator": "rolling_std", "inputs": ["ret_1"], "params": {"period": 3}},
            {"id": "min_3", "operator": "rolling_min", "inputs": ["close"], "params": {"period": 3}},
            {"id": "max_3", "operator": "rolling_max", "inputs": ["close"], "params": {"period": 3}},
            {"id": "sma_3", "operator": "sma", "inputs": ["close"], "params": {"period": 3}},
            {"id": "ema_3", "operator": "ema", "inputs": ["close"], "params": {"period": 3}},
            {"id": "rsi_3", "operator": "rsi", "inputs": ["close"], "params": {"period": 3}},
            {"id": "add_1", "operator": "add", "inputs": ["close", "open"], "params": {}},
            {"id": "sub_1", "operator": "sub", "inputs": ["close", "open"], "params": {}},
            {"id": "mul_1", "operator": "mul", "inputs": ["close", "volume"], "params": {}},
            {"id": "div_1", "operator": "div", "inputs": ["high", "low"], "params": {}},
            {"id": "rank", "operator": "cs_rank_pct", "inputs": ["close"], "params": {}},
            {"id": "zscore", "operator": "cs_zscore", "inputs": ["close"], "params": {}},
            {"id": "winsor", "operator": "cs_winsorize", "inputs": ["close"], "params": {"lower": 0.0, "upper": 1.0}},
        ],
        "outputs": ["ret_1", "lag_2", "logret_1", "mean_3", "std_3", "min_3", "max_3", "sma_3", "ema_3", "rsi_3", "add_1", "sub_1", "mul_1", "div_1", "rank", "zscore", "winsor"],
    })


def test_compiler_topological_and_all_operators_have_stable_oracles():
    compiled = compile_factor_spec(_spec())
    assert set(compiled.sorted_nodes) == {node.id for node in _spec().nodes}
    frame = pd.DataFrame({
        "close": [1.0, 2.0, 3.0, 4.0], "open": [0.5, 1.5, 2.5, 3.5],
        "high": [2.0, 3.0, 4.0, 5.0], "low": [0.5, 1.0, 1.5, 2.0], "volume": [10.0, 20.0, 30.0, 40.0],
    })
    assert np.allclose(apply_operator("lag", [frame["close"]], {"period": 1}).dropna(), [1.0, 2.0, 3.0])
    assert np.allclose(apply_operator("pct_change", [frame["close"]], {"period": 1}).dropna(), [1.0, 0.5, 1 / 3])
    assert np.allclose(apply_operator("log_return", [frame["close"]], {"period": 1}).dropna(), np.log([2.0, 1.5, 4 / 3]))
    assert np.allclose(apply_operator("rolling_mean", [frame["close"]], {"period": 2}).dropna(), [1.5, 2.5, 3.5])
    assert np.allclose(apply_operator("rolling_std", [frame["close"]], {"period": 2}).dropna(), [0.7071067811865476] * 3)
    assert np.allclose(apply_operator("rolling_min", [frame["close"]], {"period": 2}).dropna(), [1.0, 2.0, 3.0])
    assert np.allclose(apply_operator("rolling_max", [frame["close"]], {"period": 2}).dropna(), [2.0, 3.0, 4.0])
    assert np.allclose(apply_operator("sma", [frame["close"]], {"period": 2}).dropna(), [1.5, 2.5, 3.5])
    assert apply_operator("ema", [frame["close"]], {"period": 2}).notna().sum() == 4
    assert apply_operator("rsi", [frame["close"]], {"period": 2}).dropna().iloc[-1] == pytest.approx(100.0)
    assert np.allclose(apply_operator("add", [frame["close"], frame["open"]], {}), [1.5, 3.5, 5.5, 7.5])
    assert np.allclose(apply_operator("sub", [frame["close"], frame["open"]], {}), [0.5, 0.5, 0.5, 0.5])
    assert np.allclose(apply_operator("mul", [frame["close"], frame["volume"]], {}), [10, 40, 90, 160])
    assert np.allclose(apply_operator("div", [frame["high"], frame["low"]], {}), [4, 3, 8 / 3, 2.5])
    assert set(OPERATORS) == {"lag", "pct_change", "log_return", "rolling_mean", "rolling_std", "rolling_min", "rolling_max", "sma", "ema", "rsi", "add", "sub", "mul", "div", "cs_rank_pct", "cs_zscore", "cs_winsorize"}


def test_compiler_rejects_cycles_unknown_refs_duplicates_and_bad_params():
    base = {
        "schema_version": "1.0", "timeframe": "1d",
        "nodes": [
            {"id": "a", "operator": "add", "inputs": ["b", "close"], "params": {}},
            {"id": "b", "operator": "add", "inputs": ["a", "close"], "params": {}},
        ],
        "outputs": ["a"],
    }
    with pytest.raises(FactorCompileError) as cycle:
        compile_factor_spec(FactorSpec.model_validate(base))
    assert cycle.value.diagnostic["code"] == "FACTOR_DEPENDENCY_CYCLE"
    unknown = {**base, "nodes": [{"id": "a", "operator": "add", "inputs": ["missing", "close"], "params": {}}]}
    with pytest.raises(FactorCompileError) as missing:
        compile_factor_spec(FactorSpec.model_validate(unknown))
    assert missing.value.diagnostic["code"] == "FACTOR_REFERENCE_MISSING"
    duplicate = {**base, "nodes": [{"id": "a", "operator": "lag", "inputs": ["close"], "params": {"period": 1}}, {"id": "a", "operator": "lag", "inputs": ["close"], "params": {"period": 2}}]}
    with pytest.raises(FactorCompileError) as dup:
        compile_factor_spec(FactorSpec.model_validate(duplicate))
    assert dup.value.diagnostic["code"] == "FACTOR_ID_DUPLICATE"
    bad = {**base, "nodes": [{"id": "a", "operator": "lag", "inputs": ["close"], "params": {"period": 0}}]}
    with pytest.raises(FactorCompileError) as params:
        compile_factor_spec(FactorSpec.model_validate(bad))
    assert params.value.diagnostic["code"] == "FACTOR_PARAMETER_INVALID"


def test_factor_engine_outputs_feature_panel_and_is_stable_to_row_order(tmp_path):
    from stock_quant.alpha.data.catalog import DatasetStore
    from stock_quant.alpha.data.stock_dataset import StockHistoryBatch, prepare_stock_dataset

    class Provider:
        def read(self, request):
            return StockHistoryBatch(rows=_bar_rows(), calendar_version="fixture", source_version="v1", quality={})

    request = __import__("stock_quant.alpha.contracts", fromlist=["StockDatasetRequest"]).StockDatasetRequest(
        source="fixture", symbols=["AAPL", "MSFT"],
        start=datetime(2024, 1, 1, tzinfo=timezone.utc), end=datetime(2024, 3, 1, tzinfo=timezone.utc),
    )
    store = DatasetStore(tmp_path / "alpha")
    dataset = prepare_stock_dataset(request, store=store, provider=Provider())
    compiled = compile_factor_spec(_spec())
    first = compute_stock_factors(dataset, compiled, store=store)
    second = compute_stock_factors(dataset, compiled, store=store)
    assert first.dataset_kind == "feature_panel"
    assert first.dataset_id == second.dataset_id
    rows = store.read_rows(first.dataset_id)
    assert {row["factor_name"] for row in rows} == set(_spec().outputs)
    assert any(row["factor_name"] == "rank" and row["factor_value"] == 1.0 for row in rows)
