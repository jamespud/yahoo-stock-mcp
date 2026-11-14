/** Transparent stdio JSON-RPC bridge with full response capture and dispatch budget. */
import {spawn} from 'node:child_process';
import {createInterface} from 'node:readline';
import {appendFileSync, readFileSync} from 'node:fs';
import {randomUUID} from 'node:crypto';
import {preflight} from './scenarios.mjs';

export function checkDispatch(name, args, state, scenario) {
  if(['quant_import_snapshot','quant_snapshot_from_local','quant_get_calendar'].includes(name))throw Error('Fixture replacement is prohibited');
  if(!name.startsWith('quant_')) throw Error('Only quant tools are allowed');
  const limit=preflight(process.env.QUANT_EVAL_SCENARIO||'screening').max_tool_calls;
  if(++state.calls>limit) throw Error(`Tool-call budget exhausted (${limit})`);
  if(['quant_run_backtest','quant_optimize','quant_run_screen'].includes(name)) {
    const expected={multitimeframe:'quant_run_backtest',screening:'quant_run_screen',walkforward:'quant_optimize'}[scenario];
    if(name!==expected||++state.jobs>1) throw Error('Job budget exceeded');
    if(name==='quant_optimize') {
      const p=preflight('walkforward',args);
      const expected=preflight('walkforward').request.windows;
      if(args.windows.length!==4||args.windows.some((w,i)=>Object.keys(expected[i]).some(k=>Date.parse(w[k])!==Date.parse(expected[i][k])))) throw Error('Fold budget mismatch');
      if(!(args.config?.commission_bps>0&&args.config?.slippage_bps>0))throw Error('Positive optimization fees/slippage required');
      const canonical=preflight('walkforward').request;
      for(const k of ['search_space','train_top_k','cost_multipliers','max_trials','max_evaluations']) if(JSON.stringify(args[k])!==JSON.stringify(canonical[k])) throw Error(`Unexpected optimization ${k}`);
    }
    if(name==='quant_run_backtest'&&!(args.config?.commission_bps>0&&args.config?.slippage_bps>0))throw Error('Positive fees and slippage required');
    if(name==='quant_run_screen'&&JSON.stringify([...args.symbols].sort())!==JSON.stringify(['AAPL','MSFT','QTEST'])) throw Error('Unexpected screening universe');
  }
}
export function bridge() {
  const trace=process.env.QUANT_EVAL_TRACE;
  const pending=new Map(); const state={calls:0,jobs:0,sequence:0};
  // DSH may probe/reconnect. Keep the budget cumulative across child restarts.
  try {for(const line of readFileSync(trace,'utf8').trim().split('\n')) {const e=JSON.parse(line);if(e.origin==='agent'){state.calls++;state.sequence++;if(['quant_run_backtest','quant_optimize','quant_run_screen'].includes(e.tool))state.jobs++;}}} catch(e) {if(e.code!=='ENOENT') throw e;}
  const child=spawn(process.execPath,[process.env.QUANT_EVAL_SERVER,'server'],{stdio:['pipe','pipe','inherit']});
  const save=e=>appendFileSync(trace,JSON.stringify(e)+'\n',{mode:0o600});
  const reply=m=>process.stdout.write(JSON.stringify(m)+'\n');
  const input=createInterface({input:process.stdin});
  input.on('line',line=>{
    let m; try {m=JSON.parse(line);} catch {return;}
    if(m.method==='tools/call') {
      const record={callId:`eval-${++state.sequence}`,origin:'agent',tool:m.params.name,args:m.params.arguments??{}};
      try {checkDispatch(record.tool,record.args,state,process.env.QUANT_EVAL_SCENARIO);}
      catch(e) {save({...record,ok:false,error:e.message});reply({jsonrpc:'2.0',id:m.id,result:{isError:true,content:[{type:'text',text:JSON.stringify({error:e.message,_eval_evidence_id:record.callId})}]}});return;}
      pending.set(m.id,record);
    }
    child.stdin.write(line+'\n');
  });
  createInterface({input:child.stdout}).on('line',line=>{
    let m; try {m=JSON.parse(line);} catch {return;}
    const record=pending.get(m.id);
    if(record) {
      pending.delete(m.id);
      let data;try {data=JSON.parse(m.result?.content?.filter(x=>x.type==='text').map(x=>x.text).join(''));} catch {data=null;}
      save({...record,ok:!m.error&&!m.result?.isError&&data!==null,data,error:m.error});
      if(m.result) m.result.content=[...(m.result.content??[]),{type:'text',text:JSON.stringify({_eval_evidence_id:record.callId})}];
    }
    reply(m);
  });
  input.on('close',()=>child.stdin.end());
  for(const signal of ['SIGTERM','SIGINT']) process.on(signal,()=>{child.kill(signal);});
  child.on('error',e=>{process.stderr.write(e.message+'\n');process.exitCode=1;input.close();});
  child.on('exit',code=>{process.exitCode=code??1;input.close();process.stdin.destroy();});
}
if(process.argv[1]===new URL(import.meta.url).pathname) bridge();
