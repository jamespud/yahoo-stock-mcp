"""Independent standard-library SMA5/order/cash oracle; imports no production quant module."""
from pathlib import Path
from datetime import datetime, date, timedelta
from itertools import groupby
import json
import math
import os

ROOT = Path(__file__).resolve().parents[2]
RUN = Path(os.environ.get('QQQ_RUN_DIR', ROOT / '.cache/qqq-real-2026-11-14-curl'))
END = datetime.fromisoformat('2026-11-14T00:00:00+00:00')
TOL = 1e-7

def clock(x):
    return datetime.fromisoformat(x.replace('Z', '+00:00'))

def close(actual, expected, field):
    assert math.isclose(actual, expected, rel_tol=1e-11, abs_tol=TOL), (field, actual, expected)

def weekly(rows, calendar):
    weeks = {}
    for b in rows:
        d = clock(b['start_time']).date()
        monday = d - timedelta(days=d.weekday())
        weeks.setdefault(monday, []).append(b)
    out = []
    for monday, part in sorted(weeks.items()):
        if monday < date.fromisoformat(calendar['start_date']) or monday + timedelta(days=6) > date.fromisoformat(calendar['end_date']):
            continue
        out.append({**part[0], 'start_time':part[0]['start_time'], 'end_time':part[-1]['end_time'], 'available_at':max(b['available_at'] for b in part),
                    'open':part[0]['open'], 'close':part[-1]['close'], 'high':max(b['high'] for b in part), 'low':min(b['low'] for b in part),
                    'volume':sum(b['volume'] for b in part)})
    return out

def verify(tf):
    actual = json.loads((RUN / f'result-{tf}.json').read_text())
    dataset = actual['dataset']
    rows = json.loads(Path(dataset['bars_file']).read_text())
    if tf == '1w':
        rows = weekly(rows, dataset['calendar'])
    config = actual['config']
    initial, quantity = config['initial_cash'], config['quantity']
    fee_rate, slip_rate = config['commission_bps']/10000, config['slippage_bps']/10000
    actions = dataset['action_terms']
    start = clock(dataset['first_start'])
    events = []
    for i,b in enumerate(rows):
        events += [(clock(b['start_time']),1,'open',i), (clock(b['available_at']),2,'close',i)]
    for i,a in enumerate(actions):
        events.append((clock(a['effective_at']),0,'dividend',i))
    events = sorted(e for e in events if start <= e[0] < END)
    cash, held, basis, realized, receivables, mark = initial, 0., 0., 0., 0., 0.
    mark_time = None
    pending = None
    orders, fills, curve, signals = [], [], [], []
    for now, grouped in groupby(events, key=lambda e:e[0]):
        for _, _, kind, i in grouped:
            if kind == 'dividend':
                receivables += held * actions[i]['amount']
                mark = max(0.,mark-actions[i]['amount'])
                mark_time = now
            elif kind == 'open':
                mark = rows[i]['open']; mark_time = now
                if pending is not None and clock(pending['submitted_at']) < now:
                    price = mark * (1+slip_rate if pending['side']=='buy' else 1-slip_rate)
                    qty = pending['quantity']; fee = qty*price*fee_rate
                    if pending['side']=='buy' and qty*price+fee > cash+1e-10:
                        pending.update(status='rejected',reason='INSUFFICIENT_CASH');pending=None
                        continue
                    if pending['side']=='buy':
                        cash -= qty*price+fee; held += qty; basis += qty*price+fee
                    else:
                        removed = basis*qty/held
                        cash += qty*price-fee; realized += qty*price-fee-removed
                        basis -= removed;held -= qty
                    fills.append({'order_id':pending['id'],'symbol':'QQQ','time':now.isoformat(),'side':pending['side'],'quantity':qty,'price':price,'commission':fee})
                    pending.update(status='filled',remaining=0.,filled_quantity=qty);pending=None
            else:
                b = rows[i]
                if mark_time is None or clock(b['end_time']) > mark_time:
                    mark=b['close'];mark_time=clock(b['end_time'])
                if i<4:
                    continue
                # Deliberate left-to-right summation, independent from production rolling code.
                total=0.
                for source in rows[i-4:i+1]:total+=source['close']
                average=total/5
                entry,exit_=b['close']>average,b['close']<average
                signals.append({'time':now.isoformat(),'sma':average,'entry':entry,'exit':exit_})
                if pending is not None:
                    continue
                side = 'sell' if held>0 and exit_ else 'buy' if held==0 and entry else None
                if side:
                    qty=held if side=='sell' else quantity
                    order={'id':f'order_{len(orders)+1}','symbol':'QQQ','side':side,'submitted_at':now.isoformat(),'quantity':qty,'remaining':qty,'filled_quantity':0.,'status':'accepted'}
                    estimated=mark*(1+slip_rate if side=='buy' else 1-slip_rate)
                    if side=='buy' and qty*estimated*(1+fee_rate)>cash+1e-10:
                        order.update(status='rejected',reason='INSUFFICIENT_CASH')
                    else:pending=order
                    orders.append(order)
        curve.append({'time':now.isoformat(),'cash':cash,'quantity':held,'basis':basis,'mark':mark,'realized_pnl':realized,'receivables':receivables,'equity':cash+held*mark+receivables})
    if pending:pending['status']='expired'
    assert len(orders)==len(actual['orders']), ('orders',tf,len(orders),len(actual['orders']))
    for a,e in zip(actual['orders'],orders):
        for key in ['id','symbol','side','status','reason']:
            assert a.get(key)==e.get(key),(tf,'order',key,a,e)
        assert clock(a['submitted_at'])==clock(e['submitted_at'])
        for key in ['quantity','remaining','filled_quantity']:close(a[key],e[key],key)
    assert len(fills)==len(actual['fills']), ('fills',tf,len(fills),len(actual['fills']))
    max_error=0.
    for a,e in zip(actual['fills'],fills):
        assert a['order_id']==e['order_id'] and a['side']==e['side'] and clock(a['time'])==clock(e['time'])
        assert clock(a['time'])>clock(orders[int(a['order_id'].split('_')[-1])-1]['submitted_at'])
        for key in ['quantity','price','commission']:close(a[key],e[key],key)
    assert len(curve)==len(actual['equity_curve']), ('curve',tf,len(curve),len(actual['equity_curve']))
    for a,e in zip(actual['equity_curve'],curve):
        assert clock(a['time'])==clock(e['time'])
        for key in ['cash','receivables','equity']:
            close(a[key],e[key],key);max_error=max(max_error,abs(a[key]-e[key]))
        for key in ['quantity','basis','mark','realized_pnl']:close(a['positions']['QQQ'][key],e[key],key)
    peak=initial; drawdown=0.
    for point in curve:
        peak=max(peak,point['equity']);drawdown=min(drawdown,point['equity']/peak-1)
    close(actual['summary']['total_return'],curve[-1]['equity']/initial-1,'return')
    close(actual['summary']['max_drawdown'],drawdown,'drawdown')
    close(actual['attribution']['commission_paid'],math.fsum(f['commission'] for f in fills),'commission')
    close(actual['attribution']['accounting_residual'],0.,'accounting')
    output={'timeframe':tf,'status':'PASSED','bars':len(rows),'signals_checked':len(signals),'orders_checked':len(orders),'fills_checked':len(fills),'equity_points_checked':len(curve),'max_equity_error':max_error,'absolute_tolerance':TOL,'relative_tolerance':1e-11,'checks':['SMA5','signal_to_order','first_strictly_later_open','slippage','commissions','cash','quantity','cost_basis','realized_pnl','receivables','mark','equity','total_return','max_drawdown']}
    (RUN/f'oracle-signals-{tf}.json').write_text(json.dumps(signals))
    return output

results=[]
for tf in ['1m','5m','15m','1h','1d','1w']:
    try:results.append(verify(tf))
    except Exception as error:results.append({'timeframe':tf,'status':'FAILED','error':repr(error)})
report={'oracle_version':'qqq-standard-library-v1','production_quant_imports':False,'results':results,'status':'PASSED' if all(r['status']=='PASSED' for r in results) else 'FAILED'}
(ROOT/'reports/qqq-real-data/oracle.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2))
raise SystemExit(0 if report['status']=='PASSED' else 1)
