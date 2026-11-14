/** Actual MCP API smoke, deliberately separate from autonomous model evidence. */
import {preflight} from './scenarios.mjs';
export function multiStrategy() {
  const ref=tf=>({kind:'ref',symbol:'AAPL',field:'close',timeframe:tf});
  const op=(operator,period,tf)=>({kind:'call',operator,args:[ref(tf)],params:{period}});
  const align=value=>({kind:'align',value,timeframe:'15m',policy:'last_completed'});
  const cmp=(op,left,right)=>({kind:'binary',op,left,right});
  const c=value=>({kind:'const',value});
  const trend=cmp('gt',align(op('ema',10,'1d')),align(op('ema',30,'1d')));
  const rsi=align(op('rsi',14,'1h'));
  return {schema_version:'1.0',name:'preflight-multitimeframe',decision_timeframe:'15m',entry:cmp('and',trend,cmp('and',cmp('lt',rsi,c(50)),cmp('gt',ref('15m'),{kind:'ref',symbol:'AAPL',field:'open',timeframe:'15m'}))),exit:cmp('or',cmp('gt',rsi,c(50)),cmp('lt',align(op('ema',10,'1d')),align(op('ema',30,'1d'))))};
}
export function emaStrategy() {
  const ema=name=>({kind:'call',operator:'ema',args:[{kind:'ref',symbol:'$symbol',field:'close',timeframe:'1d'}],params:{period:{kind:'param',name}}});
  return {schema_version:'1.0',name:'preflight-walkforward',decision_timeframe:'1d',parameters:{fast:{value_type:'integer',minimum:1,maximum:40,default:5},slow:{value_type:'integer',minimum:1,maximum:40,default:20}},entry:{kind:'binary',op:'gt',left:ema('fast'),right:ema('slow')},exit:{kind:'binary',op:'lt',left:ema('fast'),right:ema('slow')}};
}
export async function runPreflight(client,snapshot,spec) {
  async function call(name,args={}) {const result=await client.callTool({name,arguments:args},undefined,{timeout:30000});if(result.isError)throw Error(`${name}: ${JSON.stringify(result.content)}`);return JSON.parse(result.content[0].text);}
  async function complete(job) {for(let i=0;i<150;i++){const r=await call('quant_get_job',{job_id:job.job_id});if(r.status==='completed')return job.job_id;if(['failed','cancelled'].includes(r.status))throw Error(JSON.stringify(r));await new Promise(r=>setTimeout(r,100));}throw Error('Preflight job deadline exceeded');}
  const capabilities=await call('quant_get_capabilities');
  if(!capabilities.mcp_execution_enabled)throw Error('Quant MCP execution disabled');
  const data_snapshot_id=snapshot.data_snapshot_id;
  const multi=await call('quant_register_strategy',{strategy:multiStrategy()});
  const plan=await call('quant_compile_strategy',{strategy_id:multi.strategy_id});
  for(const timeframe of ['1d','1h','15m'])await call('quant_get_data_quality',{data_snapshot_id,symbol:'AAPL',timeframe,as_of:spec.as_of});
  const signal=await call('quant_evaluate_signal',{data_snapshot_id,strategy_id:multi.strategy_id,symbol:'AAPL',as_of:spec.as_of});
  if(!plan.requirements?.length||!Array.isArray(plan.grid_requirements)||!Array.isArray(signal.grid_dependencies))throw Error('Missing warmup requirements');
  const mj=await complete(await call('quant_run_backtest',{data_snapshot_id,plan_id:plan.plan_id,symbol:'AAPL',timeframe:'15m',start:spec.intraday_start,config:{initial_cash:10000,quantity:10,commission_bps:10,slippage_bps:5}}));
  const fills=await call('quant_get_result',{job_id:mj,section:'fills',limit:1000});
  const attribution=await call('quant_get_result',{job_id:mj,section:'attribution'});
  if(!fills.rows?.some(f=>f.side==='buy')||!fills.rows?.some(f=>f.side==='sell')||!(attribution.commission_paid>0))throw Error('Multitimeframe preflight needs actual fills and commissions');
  const screening=await call('quant_register_strategy',{strategy:{schema_version:'1.0',name:'preflight-screen',decision_timeframe:'1d',entry:{kind:'const',value:true},exit:{kind:'const',value:false},signals:{score:{kind:'ref',symbol:'$symbol',field:'close',timeframe:'1d'}}}});
  const sj=await complete(await call('quant_run_screen',{data_snapshot_id,strategy_id:screening.strategy_id,symbols:['AAPL','MSFT','QTEST'],as_of:spec.as_of,score_signal:'score'}));
  const screen=await call('quant_get_result',{job_id:sj,section:'screen'});
  if(screen.rows?.length!==3||!screen.rows.find(r=>r.symbol==='QTEST'&&r.score===null&&r.errors.some(e=>e.code==='DATA_UNAVAILABLE')))throw Error('Missing third-asset diagnostics');
  const registered=await call('quant_register_strategy',{strategy:emaStrategy()});
  const request=preflight('walkforward',{windows:spec.windows}).request;
  const wj=await complete(await call('quant_optimize',{...request,data_snapshot_id,strategy_id:registered.strategy_id,symbol:'AAPL'}));
  const summary=await call('quant_get_result',{job_id:wj,section:'summary'});
  const folds=await call('quant_get_result',{job_id:wj,section:'walk_forward'});
  if(summary.evaluations!==36||folds.rows?.length!==4||folds.rows.some(f=>f.selected_parameters.fast>=f.selected_parameters.slow))throw Error('Incorrect four-fold EMA preflight');
  return {status:'PASS',scope:'deterministic MCP requests; not autonomous LLM acceptance',data_snapshot_id,multitimeframe:{strategy_id:multi.strategy_id,plan_id:plan.plan_id,job_id:mj,fills:fills.rows.length,buy_fills:fills.rows.filter(f=>f.side==='buy').length,sell_fills:fills.rows.filter(f=>f.side==='sell').length,commission_paid:attribution.commission_paid,trade_sample_sufficient:fills.rows.length>=30},screening:{strategy_id:screening.strategy_id,job_id:sj,symbols:screen.rows.map(r=>r.symbol),missing_symbol:'QTEST'},walkforward:{strategy_id:registered.strategy_id,job_id:wj,selected_plan_ids:folds.rows.map(f=>f.selected_plan_id),folds:4,candidates:4,evaluations:36}};
}
