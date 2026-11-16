/** Real stdio MCP execution. Downloads/snapshots are prepared before tool calls. */
import assert from 'node:assert/strict';
import {readFileSync,writeFileSync,mkdirSync,appendFileSync} from 'node:fs';
import {resolve,join,relative} from 'node:path';
import {createHash} from 'node:crypto';
import {Client} from '@modelcontextprotocol/sdk/client/index.js';
import {StdioClientTransport} from '@modelcontextprotocol/sdk/client/stdio.js';
const root=resolve(import.meta.dirname,'../..');
const run=resolve(process.env.QQQ_RUN_DIR||join(root,'.cache/qqq-real-2026-11-14-curl'));
const prepared=JSON.parse(readFileSync(join(run,'prepared.json'),'utf8'));
const reportDir=join(root,'reports/qqq-real-data');mkdirSync(reportDir,{recursive:true});
const trace=join(run,`mcp-trace-${Date.now()}.jsonl`);
const transport=new StdioClientTransport({command:process.execPath,args:[join(root,'dist/cli.js'),'server'],stderr:'pipe',env:{...process.env,QUANT_ENABLED:'1',QUANT_PYTHON:process.env.QUANT_PYTHON||join(root,'quant/.venv/bin/python'),QUANT_REGISTRY_PATH:prepared.registry_path,YAHOO_STOCK_MCP_SQLITE_PATH:join(run,'data.db')}});
const client=new Client({name:'qqq-real-data-acceptance',version:'1.0'});
const config={signal_backend:'vector',initial_cash:10000,quantity:10,commission_bps:5,slippage_bps:5,fill_policy:'next_bar_open',currency:'USD'};
const sha=x=>'sha256:'+createHash('sha256').update(JSON.stringify(x)).digest('hex');
const report={version:'qqq-real-mcp-v1',executed_at:new Date().toISOString(),data_kind:'real_vendor_history',execution_price_basis:prepared.execution_price_basis,availability_policy:prepared.availability_policy,corporate_action_policy:prepared.corporate_action_policy,config,trace_file:relative(root,trace),results:[],unsupported:[],tool_calls:0,tool_errors:0};
async function call(name,args={}){
 const started=Date.now();const result=await client.callTool({name,arguments:args},undefined,{timeout:45000});
 report.tool_calls++;report.tool_errors+=Number(!!result.isError);
 appendFileSync(trace,JSON.stringify({name,arguments:args,result,elapsed_ms:Date.now()-started,request_bytes:Buffer.byteLength(JSON.stringify(args)),response_bytes:Buffer.byteLength(JSON.stringify(result)),at:new Date().toISOString()})+'\n');
 assert(!result.isError,JSON.stringify(result));return JSON.parse(result.content[0].text);
}
async function section(job,section){
 let page=await call('quant_get_result',{job_id:job,section,limit:1000});
 if(!Array.isArray(page.rows))return page;
 const rows=[...page.rows];while(page.next_cursor){page=await call('quant_get_result',{job_id:job,section,limit:1000,cursor:page.next_cursor});rows.push(...page.rows);}
 return rows;
}
function strategy(tf){const ref={kind:'ref',symbol:'QQQ',field:'close',timeframe:tf};const avg={kind:'call',operator:'sma',args:[ref],params:{period:5}};return {schema_version:'1.0',name:`QQQ-close-vs-SMA5-${tf}`,decision_timeframe:tf,entry:{kind:'binary',op:'gt',left:ref,right:avg},exit:{kind:'binary',op:'lt',left:ref,right:avg}};}
function persist(){writeFileSync(join(reportDir,'result.json'),JSON.stringify(report,null,2)+'\n');}
try{
 await client.connect(transport);const catalog=await client.listTools();report.tool_catalog_hash=sha(catalog.tools);writeFileSync(join(run,'tool-catalog.json'),JSON.stringify(catalog));
 const caps=await call('quant_get_capabilities');writeFileSync(join(run,'capabilities.json'),JSON.stringify(caps));report.capabilities_hash=sha(caps);assert(caps.operators.some(x=>x.name==='sma'));
 for(const tf of ['30m','1mo']){const valid=await call('quant_validate_strategy',{strategy:strategy(tf)});assert.equal(valid.valid,false);report.unsupported.push({timeframe:tf,status:'BLOCKED',reason:'UNSUPPORTED_TIMEFRAME',validation:valid});}
 const daily=prepared.datasets.find(d=>d.timeframe==='1d');
 if(daily){const weeklyCalendar=await call('quant_get_calendar',{calendar_name:'XNAS',exchange:'XNAS',start_date:'2025-11-14',end_date:'2026-11-15'});const weeklySource=await call('quant_import_snapshot',{bars:JSON.parse(readFileSync(daily.bars_file,'utf8')),calendar_version:weeklyCalendar.version,calendars:[weeklyCalendar],actions:daily.action_terms});const weekly=await call('quant_resample_snapshot',{data_snapshot_id:weeklySource.data_snapshot_id,symbol:'QQQ',source_timeframe:'1d',target_timeframe:'1w',as_of:'2026-11-14T00:00:00Z'});assert(weekly.valid);prepared.datasets.push({...daily,...weekly,timeframe:'1w',vendor_interval:'derived_from_1d',derived:true,calendar:weeklyCalendar,calendar_id:weeklyCalendar.exchange+'|'+weeklyCalendar.version});report.weekly_resampling=weekly;}
 const selected=process.env.QQQ_TIMEFRAMES?.split(',');
 for(const dataset of prepared.datasets.filter(d=>!selected||selected.includes(d.timeframe))){
  const start=Date.now(),tf=dataset.timeframe;
  try{
   assert(caps.timeframes.includes(tf));
   const definition=strategy(tf);const valid=await call('quant_validate_strategy',{strategy:definition});assert(valid.valid);
   const registered=await call('quant_register_strategy',{strategy:definition});const plan=await call('quant_compile_strategy',{strategy_id:registered.strategy_id,symbol:'QQQ'});
   const quality=await call('quant_get_data_quality',{data_snapshot_id:dataset.data_snapshot_id,symbol:'QQQ',timeframe:tf,as_of:'2026-11-14T00:00:00Z'});
   const job=await call('quant_run_backtest',{plan_id:plan.plan_id,data_snapshot_id:dataset.data_snapshot_id,symbol:'QQQ',timeframe:tf,start:dataset.first_start,end:'2026-11-14T00:00:00Z',config,random_seed:0});
   let status;do{await new Promise(r=>setTimeout(r,500));status=await call('quant_get_job',{job_id:job.job_id});}while(['queued','running'].includes(status.status)&&Date.now()-start<160000);
   assert.equal(status.status,'completed',JSON.stringify(status));
   const data={};for(const name of ['summary','analytics','attribution','final_account','diagnostics','reproducibility','orders','fills','equity_curve','cash_ledger','corporate_actions','risk_events','artifacts'])data[name]=await section(job.job_id,name);
   assert(Math.abs(data.attribution.accounting_residual)<1e-7);assert.equal(data.reproducibility.data_snapshot_id,dataset.data_snapshot_id);assert.equal(data.reproducibility.plan_hashes.QQQ,plan.plan_id);
   const resultFile=join(run,`result-${tf}.json`);writeFileSync(resultFile,JSON.stringify({dataset,definition,plan,quality,job:status,config,...data}));
   const result={timeframe:tf,status:'PASSED',job_id:job.job_id,strategy_id:registered.strategy_id,plan_id:plan.plan_id,data_snapshot_id:dataset.data_snapshot_id,dataset_hash:dataset.dataset_hash,calendar_id:quality.calendar_id,start:quality.first_start_time,end:quality.latest_end_time,bar_count:quality.observed_count,missing_bars:quality.unavailable_completed_count,out_of_schedule:quality.out_of_schedule_count,summary:data.summary,attribution:data.attribution,diagnostics:data.diagnostics,job:status,elapsed_ms:Date.now()-start,result_file:relative(root,resultFile),excluded_vendor_rows:dataset.excluded_rows?.length||0,price_basis_status:'CONDITIONAL_VENDOR_OHLC_ASSUMPTION'};
   report.results.push(result);console.log(JSON.stringify(result));
  }catch(e){report.results.push({timeframe:tf,status:'FAILED',error:e.message,elapsed_ms:Date.now()-start});console.error(tf,e.message);}
  persist();
 }
 report.status=report.results.every(r=>r.status==='PASSED')?'PASSED':'FAILED';
}finally{persist();await client.close();}
