/** Live Yahoo acquisition only. Full responses remain local; never fabricates unavailable history. */
import {mkdirSync, writeFileSync, readFileSync} from 'node:fs';
import {resolve, join, relative} from 'node:path';
import {createHash} from 'node:crypto';
import {applyRateLimit} from '../../dist/providers/http.js';
import {config} from '../../dist/config.js';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
const execute = promisify(execFile);
async function download(url) {
  await applyRateLimit();
  const args=['--silent','--show-error','--connect-timeout','8','--max-time','25','--user-agent',config.userAgent,'--write-out','\n%{http_code}',...(config.proxyUrl?['--proxy',config.proxyUrl]:[]),url];
  const {stdout}=await execute('curl',args,{maxBuffer:16*1024*1024,timeout:27000});
  const index=stdout.lastIndexOf('\n'),body=stdout.slice(0,index),status=Number(stdout.slice(index+1));
  if(status!==200)throw Object.assign(Error(`Yahoo HTTP ${status}: ${body.slice(0,500)}`),{status,body});
  return JSON.parse(body);
}
const root = resolve(import.meta.dirname, '../..');
const runDir = resolve(process.env.QQQ_RUN_DIR || join(root, '.cache', 'qqq-real-2026-11-14-curl'));
mkdirSync(join(runDir, 'downloads'), {recursive: true});
const start = Date.parse('2025-11-14T00:00:00Z') / 1000;
const end = Date.parse('2026-11-14T00:00:00Z') / 1000;
const intervals = process.env.QQQ_INTERVALS?.split(',') || ['1m','5m','15m','30m','60m','1d','1wk','1mo'];
const hash = x => 'sha256:' + createHash('sha256').update(JSON.stringify(x)).digest('hex');
const prior = process.env.QQQ_RESUME === '1' ? JSON.parse(readFileSync(join(runDir,'acquisition.json'),'utf8')) : null;
const report = prior || {version:'qqq-live-acquisition-v1', symbol:'QQQ', requested_start:new Date(start*1000).toISOString(), requested_end_exclusive:new Date(end*1000).toISOString(), fetched_at:new Date().toISOString(), provider:'Yahoo Finance chart', run_directory:relative(root,runDir), queries:[], datasets:[]};
let sequence = Math.max(0,...report.queries.map(q=>q.id));
async function query(interval, from, to, purpose) {
  const url = new URL('https://query1.finance.yahoo.com/v8/finance/chart/QQQ');
  for (const [k,v] of Object.entries({interval,period1:from,period2:to,includePrePost:false,events:'div,splits'})) url.searchParams.set(k,String(v));
  const began=Date.now(), id=++sequence;
  try {
    const response = await download(url.href);
    writeFileSync(join(runDir,'downloads',`${id}-${interval}.json`),JSON.stringify(response));
    const result = response.chart?.result?.[0];
    if (!result || response.chart?.error) throw Error(JSON.stringify(response.chart?.error || 'No result'));
    report.queries.push({id,interval,purpose,url:url.href,status:'PASSED',elapsed_ms:Date.now()-began,bytes:Buffer.byteLength(JSON.stringify(response)),response_hash:hash(response),bar_count:result.timestamp?.length||0});
    return result;
  } catch (e) {
    const error={id,interval,purpose,url:url.href,status:'FAILED',elapsed_ms:Date.now()-began,http_status:e.status??null,error:e.message,body:e.body??null};
    report.queries.push(error);writeFileSync(join(runDir,'downloads',`${id}-${interval}-error.json`),JSON.stringify(error,null,2));
    return null;
  } finally {writeFileSync(join(runDir,'acquisition.json'),JSON.stringify(report,null,2)+'\n');}
}
if(process.env.QQQ_PROBE_OLDEST_1M==='1'){
  await query('1m',start,start+7*86400,'oldest_requested_week_retention_probe');
  writeFileSync(join(root,'reports/qqq-real-data/acquisition.json'),JSON.stringify(report,null,2)+'\n');
  process.exit(0);
}
for (const interval of intervals) {
  report.datasets=report.datasets.filter(d=>d.interval!==interval);
  const results=[];
  const full=await query(interval,start,end,'requested_full_year');
  if (full) results.push(full);
  else if (['1m','5m','15m','30m'].includes(interval) && [400,422].includes(report.queries.at(-1).http_status)) {
    // Retention fallbacks are distinct explicit intervals after a provider range error.
    // 1m: last 29 days, <=7 days per request; others: last 59 days.
    const begin=Math.max(start,end-(interval==='1m'?29:59)*86400);
    const step=(interval==='1m'?7:15)*86400;
    for(let t=begin;t<end;t+=step){const r=await query(interval,t,Math.min(end,t+step),'available_retention_window');if(r)results.push(r);}
  }
  const rows=new Map(),events={dividends:{},splits:{}};
  let nullCount=0;
  for(const r of results){
    const q=r.indicators?.quote?.[0]||{}, adj=r.indicators?.adjclose?.[0]?.adjclose;
    for(let i=0;i<(r.timestamp?.length||0);i++){
      const row={timestamp:r.timestamp[i],time:new Date(r.timestamp[i]*1000).toISOString(),open:q.open?.[i],high:q.high?.[i],low:q.low?.[i],close:q.close?.[i],volume:q.volume?.[i],...(adj?{adj_close:adj[i]}:{})};
      if(!['open','high','low','close','volume'].every(k=>typeof row[k]==='number'&&Number.isFinite(row[k]))){nullCount++;continue;}
      if(row.timestamp>=start&&row.timestamp<end)rows.set(row.timestamp,row);
    }
    Object.assign(events.dividends,r.events?.dividends);Object.assign(events.splits,r.events?.splits);
  }
  const bars=[...rows.values()].sort((a,b)=>a.timestamp-b.timestamp);
  const dataset={symbol:'QQQ',interval,source:'yahoo',data_kind:'real_vendor_history',price_basis:'vendor_quote_OHLC_not_adjclose',historical_publication_timestamps:'not_observed',bars,events,meta:results[0]?.meta??null};
  const file=join(runDir,`QQQ-${interval}.json`);writeFileSync(file,JSON.stringify(dataset));
  report.datasets.push({interval,file:relative(root,file),data_hash:hash(dataset),bar_count:bars.length,first_bar:bars[0]?.time??null,last_bar:bars.at(-1)?.time??null,discarded_null_rows:nullCount,full_year_request_status:full?'PASSED':'FAILED',data_status:bars.length?'PASSED':'BLOCKED',dividend_events:Object.keys(events.dividends).length,split_events:Object.keys(events.splits).length});
  writeFileSync(join(runDir,'acquisition.json'),JSON.stringify(report,null,2)+'\n');
  console.log(JSON.stringify(report.datasets.at(-1)));
}
writeFileSync(join(root,'reports/qqq-real-data/acquisition.json'),JSON.stringify(report,null,2)+'\n');
