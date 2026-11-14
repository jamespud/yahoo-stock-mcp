"""Prepare bounded immutable real-data experiments; financial calculations remain in MCP jobs.

Raw is an explicit vendor-OHLC execution hypothesis, not independent certification.
Dividend entitlement dates/amounts are vendor observations. Payment/publication dates are
not observed: retain entitlements as receivables through the exclusive experiment end.
"""
import json
import os
from pathlib import Path
from datetime import datetime, timedelta, timezone
from stock_quant.calendar import generate_calendar, expected_intervals
from stock_quant.jobs import JobStore

ROOT = Path(__file__).resolve().parents[2]
RUN = Path(os.environ.get('QQQ_RUN_DIR', ROOT / '.cache/qqq-real-2026-11-14-curl'))
acquisition = json.loads((RUN / 'acquisition.json').read_text())
END = datetime.fromisoformat(acquisition['requested_end_exclusive'].replace('Z', '+00:00'))
store = JobStore(RUN / 'quant.db')
prepared = {'version': 'qqq-real-snapshot-v1', 'data_kind': 'real_vendor_history', 'execution_price_basis': 'vendor_quote_raw_assumption_not_independently_certified',
 'availability_policy': 'modeled_bar_end_plus_1_second_not_historical_publication',
 'corporate_action_policy': 'vendor_ex_dates_and_amounts; unknown_pay_dates; receivables_retained_to_exclusive_end; no_reinvestment',
 'registry_path': os.path.relpath(RUN / 'quant.db', ROOT), 'datasets': []}
for dataset in acquisition['datasets']:
    iv = dataset['interval']
    if iv not in ['1m', '5m', '15m', '60m', '1d'] or not dataset['bar_count']:
        continue
    source = json.loads(Path(dataset['file']).read_text())
    if source['events']['splits']:
        raise ValueError('Split events require explicit historical raw-price verification before execution')
    tf = '1h' if iv == '60m' else iv
    begin = datetime.fromisoformat(source['bars'][0]['time'].replace('Z', '+00:00')).date()
    calendar = generate_calendar(calendar_name='XNAS', exchange='XNAS', start_date=begin, end_date=END.date())
    expected = expected_intervals(calendar, tf)
    lookup = {e.start: e for e in expected}
    sessions = {s.date: s for s in calendar.sessions}
    bars, excluded, invalid = [], [], []
    for r in source['bars']:
        stamp = datetime.fromtimestamp(r['timestamp'], timezone.utc)
        if tf == '1d':
            session = sessions.get(stamp.date())
            event = None if session is None else (session.intervals[0].start, session.intervals[-1].end)
        else:
            match = lookup.get(stamp)
            event = (match.start, match.end) if match else None
        if event is None:
            excluded.append({'time': r['time'], 'reason': 'not_a_calendar_bar_start_including_vendor_closing_marker'})
            continue
        start, end = event
        if end + timedelta(seconds=1) >= END:
            excluded.append({'time': r['time'], 'reason': 'not_published_before_experiment_end'})
            continue
        if not (r['high'] >= max(r['open'], r['close']) and r['low'] <= min(r['open'], r['close']) and r['volume'] >= 0):
            invalid.append(r)
            continue
        bars.append({'symbol':'QQQ', 'exchange':'XNAS', 'currency':'USD', 'timeframe':tf,
          'start_time':start.isoformat(), 'end_time':end.isoformat(), 'available_at':(end+timedelta(seconds=1)).isoformat(),
          **{k: float(r[k]) for k in ['open','high','low','close','volume']}, 'source':'yahoo',
          'adjustment':'raw', 'dataset_version':dataset['data_hash'], 'availability_policy':'modeled_bar_end_plus_delay'})
    actions = []
    for row in source['events']['dividends'].values():
        date = datetime.fromtimestamp(row['date'], timezone.utc).date()
        session = sessions.get(date)
        if not session:
            continue
        actions.append({'id': f'QQQ-yahoo-dividend-{date}', 'symbol':'QQQ', 'kind':'cash_dividend',
                        'effective_at': session.intervals[0].start.isoformat(), 'available_at':session.intervals[0].start.isoformat(),
                        'amount':float(row['amount']), 'currency':'USD', 'pay_at':END.isoformat()})
    if invalid:
        raise ValueError(f'Invalid vendor OHLC rows at {tf}: {invalid[:3]}')
    manifest = store.import_snapshot(bars, calendar.version, actions=actions, calendars=[calendar.model_dump(mode='json')])
    experiment = {'timeframe':tf, 'vendor_interval':iv, **manifest, 'dataset_hash':dataset['data_hash'], 'excluded_rows':excluded,
                  'first_start':bars[0]['start_time'], 'last_end':bars[-1]['end_time'], 'calendar_id':calendar.id,
                  'calendar':calendar.model_dump(mode='json'), 'bars_file':os.path.relpath(RUN / f'snapshot-bars-{tf}.json', ROOT),
                  'action_terms': actions, 'price_basis_evidence': {'field':'indicators.quote; not indicators.adjclose',
                    'returned_split_events':len(source['events']['splits']), 'independent_raw_verification':'NOT_TESTED',
                    'basis_is_execution_assumption':True}}
    (RUN / f'snapshot-bars-{tf}.json').write_text(json.dumps(bars))
    prepared['datasets'].append(experiment)
(RUN / 'prepared.json').write_text(json.dumps(prepared, indent=2)+'\n')
print(json.dumps({**{k:v for k,v in prepared.items() if k!='datasets'}, 'datasets':[{k:v for k,v in d.items() if k not in ['calendar','excluded_rows','action_terms']} for d in prepared['datasets']]}, indent=2))
