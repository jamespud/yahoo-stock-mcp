"""Compact calendar-aware raw synthetic snapshots for autonomous DSH tasks.

Bulk rows go directly to the bootstrap importer, never into model tool output.
"""
import json
import math
from datetime import timedelta
from random import Random
from stock_quant.calendar import generate_calendar
from stock_quant.compiler import digest

def build():
    calendar=generate_calendar(calendar_name='XNYS',exchange='XNAS',start_date='2025-01-02',end_date='2025-12-31')
    rng=Random(80421);rows=[];sessions=calendar.sessions[:220]
    for si,symbol in enumerate(['AAPL','MSFT','QTEST']):
        price=100.+si*30
        for i,session in enumerate(sessions):
            start,end=session.intervals[0].start,session.intervals[-1].end
            opening=price
            price=max(20.,price+[.65,-.7,0.,.65][min(i//55,3)]+math.sin(i*.8)*1.5+rng.uniform(-.4,.4))
            def add(tf,a,b,o,c,step=0):
                delay=timedelta(days=400) if symbol=='QTEST' else timedelta(seconds=30+si*30)
                if tf=='15m' and i==205 and step==3:return
                rows.append({'symbol':symbol,'exchange':'XNAS','timeframe':tf,'start_time':a.isoformat(),'end_time':b.isoformat(),
                    'available_at':(b+delay).isoformat(),'open':o,'high':max(o,c)+.5,'low':min(o,c)-.5,'close':c,'volume':10000.,
                    'source':'synthetic_seeded','adjustment':'raw','dataset_version':'dsh-complex-v1'})
            add('1d',start,end,opening,price)
            if i>=200:
                for tf,minutes in [('1h',60),('15m',15)]:
                    at=start;step=0
                    while at<end:
                        finish=min(at+timedelta(minutes=minutes),end)
                        mid=price+math.sin(i*3+step*.8)*4
                        add(tf,at,finish,mid-.3,mid+.3*math.cos(step),step)
                        at=finish;step+=1
    body={'bars':rows,'calendar_version':calendar.version,'calendars':[calendar.model_dump(mode='json')]}
    return {'fixture_version':'dsh-complex-v1','generator_version':'1','seed':80421,'data_kind':'synthetic','bar_count':len(rows),
        'data_hash':digest(body),'regimes':['uptrend','downtrend','range','volatile_reversal'],'snapshot':body}
if __name__=='__main__':print(json.dumps(build(),separators=(',',':')))
