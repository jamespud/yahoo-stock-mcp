import test from 'node:test';
import assert from 'node:assert/strict';

const load = () => import('../../scripts/quant-eval/semantic-grader.mjs');
function evidence(section, data, id = 'r1') {
  return {callId: id, origin: 'agent', tool: 'quant_get_result', args: {job_id: 'j1', section}, data, ok: true};
}
const claim = (kind, callId, path, value) => ({kind, evidence: {callId, path}, value});
const metadata={data_kind:'synthetic',source:'synthetic_seeded',price_basis:'raw',raw_attested:false};
const run = (claims, trace, scenario = 'screening') => ({scenario, claims:[...claims,{kind:'provenance',value:metadata}], trace:[{callId:'cap',origin:'agent',ok:true,tool:'quant_get_capabilities',args:{},data:{}},{callId:'reg',origin:'agent',ok:true,tool:'quant_register_strategy',args:{},data:{strategy_id:'s1'}},{callId:'submit',origin:'agent',ok:true,tool:{screening:'quant_run_screen',walkforward:'quant_optimize',multitimeframe:'quant_run_backtest'}[scenario],args:{},data:{job_id:'j1'}},...trace], metadata,exitCode: 0, finalReason: 'completed'});

test('rejects unsupported assertions and fabricated evidence IDs', async () => {
  const {grade} = await load();
  assert.equal(grade(run([claim('ranking', 'invented', '/rows', ['AAPL'])], [])).status, 'FAIL');
});
test('accepts complete screening evidence; rejects inverted rankings and bootstrap evidence', async () => {
  const {grade} = await load();
  const rows = [{symbol: 'MSFT', score: 20, errors: []}, {symbol: 'AAPL', score: 10, errors: []},{symbol:'QTEST',score:null,errors:[{code:'DATA_UNAVAILABLE'}]}];
  const trace = [evidence('screen', {rows, next_cursor: null})];
  const claims = [claim('ranking', 'r1', '/rows', rows), claim('limitation', 'r1', '/rows/0/score', 20)];
  // The statement must carry the caller-selected-universe limitation explicitly.
  claims[1] = {kind: 'limitation', value: 'caller_selected_universe'};
  assert.equal(grade(run(claims, trace)).status, 'PASS');
  assert.equal(grade(run([claim('ranking', 'r1', '/rows', [...rows].reverse()), claims[1]], trace)).status, 'FAIL');
  assert.equal(grade(run(claims, [{...trace[0], origin: 'bootstrap'}])).status, 'FAIL');
});
test('rejects failed, paginated, NaN and duplicate evidence', async () => {
  const {grade} = await load();
  for (const data of [{rows: [], next_cursor: 'more'}, {rows: [{symbol:'AAPL',score:NaN,errors:[]}]}]) {
    assert.equal(grade(run([claim('ranking','r1','/rows',data.rows)], [evidence('screen', data)])).status,'FAIL');
  }
  const e = evidence('screen', {rows: []});
  assert.equal(grade(run([], [e, e])).status, 'FAIL');
  assert.equal(grade(run([], [{...e,ok:false}])).status, 'FAIL');
});
test('no credentials and unsuccessful terminal status cannot become PASS', async () => {
  const {grade} = await load();
  assert.equal(grade({skipReason:'missing DEEPSEEK_API_KEY'}).status, 'SKIPPED');
  assert.equal(grade({...run([],[]),exitCode:1}).status, 'FAIL');
});
test('preflight derives four-fold evaluation cost and refuses oversized Cartesian searches', async () => {
  const {preflight} = await import('../../scripts/quant-eval/scenarios.mjs');
  assert.equal(preflight('walkforward').evaluations, 36);
  assert.throws(() => preflight('walkforward', {search_space:{x:Array.from({length:101},(_,i)=>i)}}), /budget|trials/i);
});

test('metric semantics reject 135% for 1.35%, total as annualized, null Sharpe as zero', async()=>{
  const {validateClaim}=await load();
  const e={data:{total_return:0.0135,cagr:0.2,sharpe:null}};
  const metric=(name,path,value,unit)=>({...claim('metric','r',path,value),metric:name,unit});
  assert.doesNotThrow(()=>validateClaim(metric('total_return','/total_return',1.35,'percent'),e));
  assert.doesNotThrow(()=>validateClaim(metric('total_return','/total_return',0.0135,'ratio'),e));
  assert.throws(()=>validateClaim(metric('total_return','/total_return',135,'percent'),e),/scaling|evidence/);
  assert.throws(()=>validateClaim(metric('cagr','/total_return',0.0135,'ratio'),e),/annualized/);
  assert.throws(()=>validateClaim(metric('sharpe','/sharpe',0,'ratio'),e),/evidence/);
  assert.doesNotThrow(()=>validateClaim(metric('sharpe','/sharpe',null,'ratio'),e));
  assert.throws(()=>validateClaim({...metric('total_return','/total_return',0.0135,'ratio'),label:'annualized 135%'},e),/label/);
});
test('provenance rejects synthetic called Yahoo and raw called raw_attested',async()=>{
  const {validateProvenance}=await load();
  const good={kind:'provenance',value:metadata};
  assert.doesNotThrow(()=>validateProvenance(good,metadata));
  for(const change of [{source:'yahoo'},{data_kind:'live'},{price_basis:'raw_attested'},{raw_attested:true}]) assert.throws(()=>validateProvenance({...good,value:{...metadata,...change}},metadata),/source|attested/);
});
test('guard rejects second job, non-EMA optimization, omitted costs and invented fixture imports',async()=>{
  const {checkDispatch}=await import('../../scripts/quant-eval/mcp-proxy.mjs');
  const {preflight}=await import('../../scripts/quant-eval/scenarios.mjs');
  const state={calls:0,jobs:0};
  checkDispatch('quant_optimize',preflight('walkforward').request,state,'walkforward');
  assert.throws(()=>checkDispatch('quant_optimize',preflight('walkforward').request,state,'walkforward'),/budget/);
  assert.throws(()=>checkDispatch('quant_run_backtest',{config:{}},{calls:0,jobs:0},'multitimeframe'),/fees/);
  assert.throws(()=>checkDispatch('quant_import_snapshot',{}, {calls:0,jobs:0},'screening'),/replacement/);
  assert.throws(()=>checkDispatch('quant_optimize',{...preflight('walkforward').request,search_space:{fast:[30],slow:[10]}},{calls:0,jobs:0},'walkforward'),/fast/);
});
test('multi-timeframe structure requires daily EMA10/30 hourly RSI14 and 15m dependency',async()=>{
  const {validMultitimeframe}=await load();
  const ref=tf=>({kind:'ref',timeframe:tf,field:'close',symbol:'AAPL'});
  const op=(operator,period,tf)=>({kind:'call',operator,params:{period},args:[ref(tf)]});
  const align=value=>({kind:'align',value,timeframe:'15m',policy:'last_completed'});
  const s={decision_timeframe:'15m',entry:{kind:'binary',left:align(op('ema',10,'1d')),right:align(op('ema',30,'1d'))},exit:{kind:'binary',left:align(op('rsi',14,'1h')),right:ref('15m')}};
  assert.equal(validMultitimeframe(s),true);
  for(const changed of [{...s,decision_timeframe:'1d'},{...s,entry:{kind:'const',value:true}},{...s,exit:{kind:'const',value:false}}])assert.equal(validMultitimeframe(changed),false);
});
test('requires capability discovery before registration and rejects mixed job results',async()=>{
  const {validateTrajectory}=await load();
  const cap={origin:'agent',ok:true,tool:'quant_get_capabilities'};
  const reg={origin:'agent',ok:true,tool:'quant_register_strategy'};
  const job={origin:'agent',ok:true,tool:'quant_run_screen',data:{job_id:'j1'}};
  const result={origin:'agent',ok:true,tool:'quant_get_result',args:{job_id:'j1'}};
  assert.doesNotThrow(()=>validateTrajectory([cap,reg,job,result],'screening'));
  assert.throws(()=>validateTrajectory([reg,job,result],'screening'),/capabilities/);
  assert.throws(()=>validateTrajectory([reg,cap,job,result],'screening'),/capabilities/);
  assert.throws(()=>validateTrajectory([{...cap,ok:false},reg,job,result],'screening'),/capabilities/);
  assert.throws(()=>validateTrajectory([cap,reg,job,{...result,args:{job_id:'j2'}}],'screening'),/identities/);
});
test('ratio and count labels follow the observed quant quality schema',async()=>{
  const {validateClaim}=await load();
  const e={data:{coverage_ratio:0,observed_count:0,net_pnl:-1,gross_traded_notional:10,rows:[{zscore:1}]}};
  assert.doesNotThrow(()=>validateClaim({kind:'quality',evidence:{callId:'r',path:'/coverage_ratio'},metric:'coverage_ratio',unit:'ratio',value:0},e));
  assert.doesNotThrow(()=>validateClaim({kind:'quality',evidence:{callId:'r',path:'/observed_count'},metric:'observed_count',unit:'count',value:0},e));
  assert.doesNotThrow(()=>validateClaim({kind:'metric',evidence:{callId:'r',path:'/rows/0/zscore'},metric:'zscore',unit:'ratio',value:1},e));
  assert.doesNotThrow(()=>validateClaim({kind:'metric',evidence:{callId:'r',path:'/net_pnl'},metric:'net_pnl',unit:'currency',value:-1},e));
  assert.doesNotThrow(()=>validateClaim({kind:'metric',evidence:{callId:'r',path:'/gross_traded_notional'},metric:'gross_traded_notional',unit:'currency',value:10},e));
});
test('diagnostic scalar labels match the observed screening schema and null cursor',async()=>{
  const {grade}=await load();
  const rows=[{symbol:'MSFT',rank:1,score:162.8312436918878,percentile:1,selected:true,errors:[]},
    {symbol:'AAPL',rank:2,score:135.72031653951393,percentile:.5,selected:true,errors:[]},
    {symbol:'QTEST',rank:null,score:null,percentile:null,selected:false,errors:[{code:'DATA_UNAVAILABLE',symbol:'QTEST'}]}];
  const callId='screen-result';
  const trace=[{callId,origin:'agent',ok:true,tool:'quant_get_result',args:{job_id:'j1',section:'screen'},data:{status:'completed',total_count:3,next_cursor:null,rows}}];
  const claims=[
    {kind:'diagnostic',evidence:{callId,path:'/status'},value:'completed'},
    {kind:'diagnostic',evidence:{callId,path:'/total_count'},value:3,metric:'total_count',unit:'count'},
    {kind:'diagnostic',evidence:{callId,path:'/next_cursor'},value:null,metric:'next_cursor',unit:'count'},
    claim('ranking',callId,'/rows',rows),
    {kind:'metric',metric:'score',unit:'currency',evidence:{callId,path:'/rows/0/score'},value:rows[0].score},
    {kind:'metric',metric:'rank',unit:'count',evidence:{callId,path:'/rows/0/rank'},value:rows[0].rank},
    {kind:'metric',metric:'percentile',unit:'ratio',evidence:{callId,path:'/rows/0/percentile'},value:rows[0].percentile},
    {kind:'diagnostic',evidence:{callId,path:'/rows/2/errors/0/code'},value:'DATA_UNAVAILABLE'},
    {kind:'limitation',value:'caller_selected_universe'},
    {kind:'limitation',value:'no_historical_index_membership_or_provider_backed_coverage_claimed'},
    {kind:'limitation',value:'QTEST_not_ranked_due_to_DATA_UNAVAILABLE_no_return_implied'}
  ];
  assert.equal(grade(run(claims,trace)).status,'PASS');
  const wrong=structuredClone(claims);wrong[1].unit='currency';
  assert.equal(grade(run(wrong,trace)).status,'FAIL');
});
test('an isolated credential source without keys remains SKIPPED, never PASS',async()=>{
  const {mkdtempSync}=await import('node:fs');
  const {runDSH}=await import('../../scripts/quant-eval/run-dsh.mjs');
  const emptyHome=mkdtempSync('/tmp/quant-empty-credentials-');
  const result=await runDSH({env:{PATH:process.env.PATH,HOME:emptyHome},scenarios:['screening']});
  assert.equal(result.status,'SKIPPED');
  assert.match(result.results[0].reason,/API_KEY/);assert.equal(result.results[0].credentialSource,null);
});
