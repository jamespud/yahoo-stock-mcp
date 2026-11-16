"""Bounded four-fold synthetic research through production APIs; no agent logic."""
from datetime import timedelta
import json
import math
from pathlib import Path
from random import Random
from stock_quant.calendar import generate_calendar
from stock_quant.compiler import compile_strategy, digest
from stock_quant.data import MarketBar, Snapshot
from stock_quant.research import OptimizationRequest, optimize

ROOT = Path(__file__).resolve().parents[1]
def run():
    cal = generate_calendar(calendar_name='XNYS', exchange='XNAS', start_date='2025-01-02', end_date='2025-12-31')
    rng = Random(80421)
    bars = []
    price = 100.
    for i, session in enumerate(cal.sessions[:220]):
        opening = price
        drift = [.65, -.7, 0., .1][min(i // 55, 3)]
        price = max(20., price + drift + math.sin(i * .8) * (3. if i >= 165 else 1.5) + rng.uniform(-.4, .4))
        interval = session.intervals[0]
        bars.append(MarketBar(symbol='AAPL', exchange='XNAS', timeframe='1d', start_time=interval.start, end_time=interval.end,
            available_at=interval.end + timedelta(seconds=30), open=opening, high=max(opening,price)+.5, low=min(opening,price)-.5,
            close=price, volume=10000., source='synthetic_seeded', adjustment='raw', dataset_version='research-daily-v1'))
    snapshot = Snapshot(bars, calendar_version=cal.version, calendars=[cal])
    def ema(name):
        return {'kind':'call','operator':'ema','args':[{'kind':'ref','symbol':'$symbol','field':'close','timeframe':'1d'}], 'params':{'period':{'kind':'param','name':name}}}
    strategy = {'schema_version':'1.0','name':'four-fold-ema-v1','decision_timeframe':'1d',
        'parameters': {name:{'value_type':'integer','minimum':1,'maximum':40,'default':default} for name,default in [('fast',5),('slow',20)]},
        'entry':{'kind':'binary','op':'gt','left':ema('fast'),'right':ema('slow')},
        'exit':{'kind':'binary','op':'lt','left':ema('fast'),'right':ema('slow')}}
    windows=[]
    for offset in [0,40,80,120]:
        indices=[40+offset,60+offset,60+offset,70+offset,70+offset,80+offset]
        windows.append(dict(zip(['train_start','train_end','validation_start','validation_end','test_start','test_end'],[bars[n].start_time for n in indices])))
    request=OptimizationRequest.model_validate({'strategy_id':digest(strategy),'data_snapshot_id':snapshot.id,'symbol':'AAPL',
        'search_space':{'fast':[5,10],'slow':[20,30]},'windows':windows,'train_top_k':2,'max_trials':4,'max_evaluations':40,
        'cost_multipliers':[1,2], 'random_seed':80421,'config':{'initial_cash':10000.,'quantity':10.,'commission_bps':10.,'slippage_bps':5.}})
    result=optimize(request,strategy,snapshot)
    assert result['summary']['windows']==4 and result['summary']['evaluations']==36
    assert all(w['selected_parameters']['fast']<w['selected_parameters']['slow'] for w in result['walk_forward'])
    changed=[b.model_copy(update={'open':b.open*3,'high':b.high*3,'low':b.low*3,'close':b.close*3}) if b.start_time>=windows[-1]['test_start'] else b for b in bars]
    altered=Snapshot(changed,calendar_version=cal.version,calendars=[cal])
    rerun=optimize(request.model_copy(update={'data_snapshot_id':altered.id}),strategy,altered)
    assert result['trials']==rerun['trials'], 'Test mutation changed selection trials'
    assert [w['selected_parameters'] for w in result['walk_forward']]==[w['selected_parameters'] for w in rerun['walk_forward']]
    warnings = []
    folds = result['walk_forward']
    for i, fold in enumerate(folds):
        if fold['test']['fill_count'] < 30:
            warnings.append({'code':'INSUFFICIENT_TRADE_SAMPLE','fold':i,'fill_count':fold['test']['fill_count']})
        if fold['test']['total_return'] < fold['validation']['total_return']:
            warnings.append({'code':'OOS_RETURN_DECLINE','fold':i})
        if fold['test']['max_drawdown'] < fold['validation']['max_drawdown']:
            warnings.append({'code':'DRAWDOWN_INCREASE','fold':i})
        costs = fold['cost_sensitivity']
        if costs[1]['metrics']['total_return'] < costs[0]['metrics']['total_return']:
            warnings.append({'code':'COST_SENSITIVE','fold':i})
        if i and fold['selected_parameters'] != folds[i-1]['selected_parameters']:
            warnings.append({'code':'PARAMETER_DRIFT','fold':i})
    return {'status':'PASSED','data_kind':'synthetic','fixture_version':'research-daily-v1','random_seed':80421,'bar_count':len(bars),
        'data_snapshot_hash':snapshot.id,'calendar_version':cal.version,'strategy_hash':digest(strategy),
        'plan_hashes':[compile_strategy(strategy,{'fast':f,'slow':s},'AAPL').plan_hash for f in [5,10] for s in [20,30]],
        'parameter_isolation':'PASSED','research':result,'research_warnings':warnings,
        'warnings':['Independent fresh-capital fold means are not continuous portfolio returns.', 'Synthetic outcomes have no established market predictive power.']}
if __name__=='__main__':
    report=run()
    (ROOT/'reports').mkdir(exist_ok=True)
    (ROOT/'reports/quant-research.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'status':report['status'],**report['research']['summary']}))
