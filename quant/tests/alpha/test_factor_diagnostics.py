from datetime import datetime, timedelta, timezone

import pytest

from stock_quant.alpha.data.catalog import DatasetStore
from stock_quant.alpha.factors.compiler import compile_factor_spec
from stock_quant.alpha.factors.engine import compute_stock_factors, stock_factor_diagnostics
from stock_quant.alpha.factors.ast import FactorSpec


def _rows():
    rows=[]
    start=datetime(2024,1,1,tzinfo=timezone.utc)
    for symbol, base in (("AAPL",100.0),("MSFT",200.0)):
        for day in range(10):
            at=start+timedelta(days=day)
            close=base+day
            rows.append({"instrument_id":symbol,"start_at":at,"end_at":at.replace(hour=21),
                         "available_at":at.replace(hour=21,minute=1),"open":close-0.5,"high":close+1,
                         "low":close-1,"close":close,"volume":1000+day,"adjustment":"raw",
                         "source":"fixture","source_version":"v1","currency":"USD"})
    return rows


def test_factor_diagnostics_cover_warmup_and_valid_values(tmp_path):
    store=DatasetStore(tmp_path/'alpha')
    request=__import__('stock_quant.alpha.contracts',fromlist=['StockDatasetRequest']).StockDatasetRequest(
        source='fixture',symbols=['AAPL','MSFT'],start=datetime(2024,1,1,tzinfo=timezone.utc),end=datetime(2024,1,11,tzinfo=timezone.utc))
    dataset=store.publish_stock(request,_rows(),calendar_version='fixture',source_version='v1',data_quality={})
    compiled=compile_factor_spec(FactorSpec.model_validate({
      'schema_version':'1.0','timeframe':'1d',
      'nodes':[{'id':'ret','operator':'pct_change','inputs':['close'],'params':{'period':1}}],
      'outputs':['ret']}))
    frame=__import__('pandas').DataFrame(_rows())
    node_values={'ret':frame.groupby('instrument_id')['close'].pct_change()}
    diagnostics=stock_factor_diagnostics(frame,compiled,node_values)
    assert diagnostics
    for row in diagnostics:
        assert set(row)>= {'instrument_id','factor_name','expected_observations','observed_observations','valid_values','warmup_missing','unexpected_missing','nonfinite_values','eligible_values','coverage','first_valid_at','last_valid_at'}
    assert row['warmup_missing']==1
    assert row['valid_values']==9


def test_factor_job_result_includes_diagnostics(tmp_path):
    from stock_quant.jobs import JobStore, execute_job
    from stock_quant.alpha.factors.ast import FactorSpec
    store = DatasetStore(tmp_path / "registry.alpha")
    request = __import__('stock_quant.alpha.contracts', fromlist=['StockDatasetRequest']).StockDatasetRequest(
        source='fixture', symbols=['AAPL', 'MSFT'], start=datetime(2024, 1, 1, tzinfo=timezone.utc), end=datetime(2024, 1, 11, tzinfo=timezone.utc))
    dataset = store.publish_stock(request, _rows(), calendar_version='fixture', source_version='v1', data_quality={})
    with JobStore(tmp_path / "registry.sqlite3") as jobs:
        run = jobs.submit_alpha({"kind": "compute_factors", "dataset_id": dataset.dataset_id,
                                 "factor_spec": {"schema_version": "1.0", "timeframe": "1d",
                                                 "nodes": [{"id": "ret", "operator": "pct_change", "inputs": ["close"], "params": {"period": 1}}],
                                                 "outputs": ["ret"]}})
        assert execute_job(jobs, jobs.claim()) is True
        diagnostics = jobs.get_result(run["job_id"], "factor_diagnostics")
        assert diagnostics["rows"]
        assert {"instrument_id", "factor_name", "warmup_missing", "unexpected_missing", "coverage"} <= set(diagnostics["rows"][0])
