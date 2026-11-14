import {isDeepStrictEqual} from 'node:util';
const finite = x => typeof x==='number'?Number.isFinite(x):x===null||typeof x!=='object'?true:Object.values(x).every(finite);
function pointer(data, path) {
  if(typeof path!=='string'||!path.startsWith('/')) throw Error('Invalid evidence pointer');
  return path.slice(1).split('/').map(k=>k.replace(/~1/g,'/').replace(/~0/g,'~')).reduce((v,k)=>{
    if(v===null||typeof v!=='object'||!Object.hasOwn(v,k)) throw Error('Evidence path missing');
    return v[k];
  },data);
}
export function validateProvenance(claim,metadata) {
  if(!metadata||!isDeepStrictEqual(claim.value,metadata)||Object.keys(claim).some(k=>!['kind','value'].includes(k)))throw Error('Unsupported source or raw_attested assertion');
}
export function validateClaim(c,e) {
  if(Object.keys(c).some(k=>!['kind','evidence','value','metric','unit'].includes(k)))throw Error('Unverified claim label or narrative');
  const actual=pointer(e.data,c.evidence.path);
  let expected=actual;
  const leaf=c.evidence.path.split('/').at(-1);
  const ratios=['total_return','cagr','max_drawdown','annualized_volatility','mean_test_return','test_return_std','percentile','coverage_ratio','zscore','turnover_to_initial_equity'];
  if(c.metric!==undefined||c.unit!==undefined) {
    // Structured labels are evidence, not prose. They may disambiguate any scalar/null
    // diagnostic field, but not booleans, arrays or objects.
    if(!(typeof actual==='number'||actual===null))throw Error('Labels only permitted on scalar metrics');
    if(c.metric!==leaf)throw Error('Metric label does not match source (total vs annualized)');
    const allowed=ratios.includes(leaf)?['ratio','percent']:leaf==='sharpe'?['ratio']:(leaf==='commission_paid'||leaf==='score'||/_pnl$|_notional$/.test(leaf))?['currency']:['count'];
    if(!allowed.includes(c.unit))throw Error('Unsupported metric unit');
    if(c.unit==='percent'&&actual!==null) expected=actual*100;
  }
  if(!finite(c.value)||!isDeepStrictEqual(expected,c.value))throw Error('Claim contradicts evidence or percent scaling');
}
export function validMultitimeframe(strategy) {
  const calls=[];const refs=[];const aligned=[];
  function walk(x){if(!x||typeof x!=='object')return;if(x.kind==='signal'){walk(strategy.signals?.[x.name]);return;}if(x.kind==='ref')refs.push(x);if(x.kind==='call')calls.push(x);if(x.kind==='align')aligned.push(x);Object.values(x).forEach(v=>Array.isArray(v)?v.forEach(walk):walk(v));}
  walk(strategy?.entry);walk(strategy?.exit);
  const has=(op,period,tf)=>calls.some(c=>c.operator===op&&c.params?.period===period&&c.args?.some(a=>a.kind==='ref'&&a.timeframe===tf&&a.field==='close'));
  const targetAligned=tf=>aligned.some(a=>a.timeframe==='15m'&&a.policy==='last_completed'&&JSON.stringify(a.value).includes(`\"timeframe\":\"${tf}\"`));
  return strategy?.decision_timeframe==='15m'&&has('ema',10,'1d')&&has('ema',30,'1d')&&has('rsi',14,'1h')&&refs.some(r=>r.timeframe==='15m')&&targetAligned('1d')&&targetAligned('1h');
}
export function validateTrajectory(trace,scenario) {
  const successful=trace.filter(e=>e.origin==='agent'&&e.ok);
  const cap=trace.findIndex(e=>e.origin==='agent'&&e.ok&&e.tool==='quant_get_capabilities');
  const firstWrite=trace.findIndex(e=>e.origin==='agent'&&['quant_register_strategy','quant_run_backtest','quant_optimize','quant_run_screen'].includes(e.tool));
  if(cap<0||firstWrite<0||cap>=firstWrite)throw Error('Successful capabilities discovery must precede registration/submission');
  const name={screening:'quant_run_screen',walkforward:'quant_optimize',multitimeframe:'quant_run_backtest'}[scenario];
  const jobs=successful.filter(e=>['quant_run_backtest','quant_optimize','quant_run_screen'].includes(e.tool));
  if(jobs.length!==1||jobs[0].tool!==name||!jobs[0].data?.job_id)throw Error('Expected one successful scenario job submission');
  if(successful.some(e=>e.tool==='quant_get_result'&&e.args.job_id!==jobs[0].data.job_id))throw Error('Result claims mix job identities');
  const firstResult=trace.findIndex(e=>e.origin==='agent'&&e.ok&&e.tool==='quant_get_result');
  if(firstResult>=0&&firstResult<trace.indexOf(jobs[0]))throw Error('Result predates job submission');
}
/** Fail closed: values are checked against full trusted MCP responses, never prose keywords. */
export function grade({scenario,claims,trace,exitCode,finalReason,skipReason,metadata}={}) {
  if(skipReason) return {status:'SKIPPED',reason:skipReason,errors:[]};
  const errors=[];
  try {
    if(exitCode!==0||finalReason!=='completed') throw Error('DSH run did not complete successfully');
    if(!['screening','walkforward','multitimeframe'].includes(scenario)||!Array.isArray(claims)||!claims.length||!Array.isArray(trace)) throw Error('Missing scenario, structured claims or trace');
    validateTrajectory(trace,scenario);
    const ids=new Set(); const used=[];
    for(const e of trace) {if(ids.has(e.callId)) throw Error('Duplicate evidence ID'); ids.add(e.callId);}
    for(const c of claims) {
      if(c.kind==='limitation') {if(Object.keys(c).some(k=>!['kind','value','evidence'].includes(k)))throw Error('Unsupported limitation fields');if(c.evidence){const e=trace.find(e=>e.callId===c.evidence.callId);if(!e||e.origin!=='agent'||!e.ok||!e.tool?.startsWith('quant_'))throw Error('Limitation has no successful agent evidence');}continue;}
      if(c.kind==='provenance') {validateProvenance(c,metadata);continue;}
      if(!['metric','ranking','fold','quality','diagnostic','fills'].includes(c.kind)) throw Error('Unsupported claim kind');
      const e=trace.find(e=>e.callId===c.evidence?.callId);
      if(!e||e.origin!=='agent'||!e.ok||!e.tool?.startsWith('quant_')) throw Error('Claim has no successful agent evidence');
      validateClaim(c,e);
      used.push({c,e});
    }
    if(!claims.some(c=>c.kind==='provenance'))throw Error('Missing source provenance');
    const limitation={screening:'caller_selected_universe',walkforward:'independent_fresh_capital_folds',multitimeframe:'higher_timeframe_availability'}[scenario];
    if(!claims.some(c=>c.kind==='limitation'&&c.value===limitation)) throw Error('Required limitation missing');
    const section=(x,s)=>x.e.tool==='quant_get_result'&&x.e.args.section===s&&!x.e.data.next_cursor;
    if(scenario==='screening') {
      const r=used.find(x=>x.c.kind==='ranking'&&x.c.evidence.path==='/rows'&&section(x,'screen'));
      const rows=r?.c.value;
      if(!Array.isArray(rows)||rows.length!==3||new Set(rows.map(r=>r.symbol)).size!==3||!['AAPL','MSFT','QTEST'].every(s=>rows.some(r=>r.symbol===s))||rows.slice(0,2).some(r=>typeof r.score!=='number'||!Number.isFinite(r.score)||r.errors?.length)||rows[0].score<rows[1].score||rows[2].symbol!=='QTEST'||rows[2].score!==null||rows[2].rank!=null||!rows[2].errors?.some(e=>e.code==='DATA_UNAVAILABLE')) throw Error('Incomplete or invalid screening ranking');
    }
    if(scenario==='walkforward') {
      const optimization=trace.find(e=>e.ok&&e.tool==='quant_optimize');
      const reg=trace.find(e=>e.ok&&e.tool==='quant_register_strategy'&&e.data.strategy_id===optimization?.args.strategy_id);
      if(!reg||!JSON.stringify(reg.args.strategy).includes('ema'))throw Error('Missing EMA strategy registration');
      const folds=used.filter(x=>x.c.kind==='fold'&&section(x,'walk_forward')&&/^\/rows\/[0-3]$/.test(x.c.evidence.path));
      if(new Set(folds.map(x=>x.c.evidence.path)).size!==4||new Set(folds.map(x=>x.e.args.job_id)).size!==1) throw Error('Need four distinct folds from one job');
      let end=-Infinity;
      for(const {c} of folds.sort((a,b)=>a.c.value.window-b.c.value.window)) {
        const f=c.value; const b=f.boundaries;
        const t=['train_start','train_end','validation_start','validation_end','test_start','test_end'].map(k=>Date.parse(b?.[k]));
        if(!(f.selected_parameters?.fast<f.selected_parameters?.slow)||t.some(x=>!Number.isFinite(x))||!(t[0]<t[1]&&t[1]<=t[2]&&t[2]<t[3]&&t[3]<=t[4]&&t[4]<t[5])||t[4]<end||!f.selected_plan_id||!f.selected_parameters||!f.train||!f.validation||!f.test||f.cost_sensitivity?.length!==2) throw Error('Invalid fold separation or missing fold metrics');
        end=t[5];
      }
      if(!used.some(x=>x.c.kind==='diagnostic'&&section(x,'diagnostics')&&x.e.args.job_id===folds[0].e.args.job_id&&x.c.evidence.path==='/out_of_sample_used_for_selection'&&x.c.value===false)) throw Error('No evidence of held-out test selection');
    }
    if(scenario==='multitimeframe') {
      if(!used.some(x=>x.c.kind==='quality'&&x.e.tool==='quant_get_data_quality'&&x.c.evidence.path==='/observed_count'&&x.c.value>0)||!used.some(x=>x.e.tool==='quant_evaluate_signal'&&x.c.evidence.path==='/signals')||!used.some(x=>section(x,'summary')&&x.c.evidence.path==='/total_return'&&typeof x.c.value==='number')) throw Error('Missing quality, signal or backtest evidence');
      const registered=trace.filter(x=>x.origin==='agent'&&x.ok&&x.tool==='quant_register_strategy');
      const submitted=trace.find(x=>x.ok&&x.tool==='quant_run_backtest');
      const compiled=trace.find(x=>x.ok&&x.tool==='quant_compile_strategy'&&x.data.plan_id===submitted?.args.plan_id);
      if(!used.some(x=>x.e===compiled&&x.c.evidence.path==='/requirements')||!used.some(x=>x.e===compiled&&x.c.evidence.path==='/grid_requirements')||!used.some(x=>x.e.tool==='quant_evaluate_signal'&&x.c.evidence.path==='/grid_dependencies'))throw Error('Missing warmup/grid requirements evidence');
      if(!registered.some(x=>x.data.strategy_id===compiled?.args.strategy_id&&validMultitimeframe(x.args.strategy))) throw Error('Missing daily EMA10/30, hourly RSI14 or 15m timing');
      const quality=used.filter(x=>x.c.kind==='quality'&&x.e.tool==='quant_get_data_quality');
      if(!['1d','1h','15m'].every(t=>quality.some(x=>x.e.args.timeframe===t)))throw Error('Quality required for all three timeframes');
      const fills=used.find(x=>x.c.kind==='fills'&&section(x,'fills')&&x.c.evidence.path==='/rows');
      if(!Array.isArray(fills?.c.value)||!fills.c.value.some(f=>f.side==='buy')||!fills.c.value.some(f=>f.side==='sell'))throw Error('Need actual buy and sell fills');
      if(fills.c.value.length<30&&!claims.some(c=>c.kind==='limitation'&&c.value==='insufficient_trade_sample'))throw Error('Insufficient trade sample must be disclosed');
      if(!used.some(x=>section(x,'attribution')&&x.e.args.job_id===fills.e.args.job_id&&x.c.evidence.path==='/commission_paid'&&x.c.value>0))throw Error('No paid commission evidence');
      const submit=trace.find(x=>x.origin==='agent'&&x.ok&&x.tool==='quant_run_backtest'&&x.data.job_id===fills.e.args.job_id);
      if(!(submit?.args.config?.commission_bps>0&&submit?.args.config?.slippage_bps>0))throw Error('Missing positive fees/slippage');
    }
  } catch(e) {errors.push(e.message);}
  return {status:errors.length?'FAIL':'PASS',errors,checkedClaims:claims?.length??0};
}
