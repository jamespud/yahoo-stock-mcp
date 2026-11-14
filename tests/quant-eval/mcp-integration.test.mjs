import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {mkdtempSync,readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {Client} from '@modelcontextprotocol/sdk/client/index.js';
import {StdioClientTransport} from '@modelcontextprotocol/sdk/client/stdio.js';
import {seed,profilePatch} from '../../scripts/quant-eval/run-dsh.mjs';
import {fixtureSpec} from '../../scripts/quant-eval/scenarios.mjs';
import {grade} from '../../scripts/quant-eval/semantic-grader.mjs';

test('real fixture importer and captured MCP proxy produce gradeable three-symbol missing-data screening', {timeout:60000},async()=>{
  const root=resolve(import.meta.dirname,'../..');const dir=mkdtempSync('/tmp/quant-eval-proxy-');
  const python=process.env.QUANT_PYTHON||root+'/quant/.venv/bin/python';
  const fixture=JSON.parse(execFileSync(python,[root+'/scripts/export-quant-eval-fixture.py'],{encoding:'utf8',maxBuffer:8e6}));
  const spec=fixtureSpec(fixture);
  const env={PATH:process.env.PATH,HOME:process.env.HOME,QUANT_ENABLED:'1',QUANT_PYTHON:python,QUANT_REGISTRY_PATH:dir+'/quant.db',YAHOO_STOCK_MCP_SQLITE_PATH:dir+'/data.db',QUANT_EVAL_TRACE:dir+'/trace.jsonl',QUANT_EVAL_SERVER:root+'/dist/cli.js',QUANT_EVAL_SCENARIO:'screening'};
  const snapshot=await seed(env,env.QUANT_EVAL_SERVER,fixture);
  assert.equal(snapshot.bar_count,2637);
  const client=new Client({name:'quant-eval-proxy-test',version:'1'});
  const transport=new StdioClientTransport({command:process.execPath,args:[root+'/scripts/quant-eval/mcp-proxy.mjs'],env,stderr:'pipe'});
  transport.stderr?.resume();
  async function call(name,args={}){const r=await client.callTool({name,arguments:args});assert.equal(r.isError,undefined,JSON.stringify(r));return {data:JSON.parse(r.content[0].text),id:JSON.parse(r.content.at(-1).text)._eval_evidence_id};}
  try {
    await client.connect(transport);
    await call('quant_get_capabilities');
    const registered=await call('quant_register_strategy',{strategy:{schema_version:'1.0',name:'proxy-screen',decision_timeframe:'1d',entry:{kind:'const',value:true},exit:{kind:'const',value:false},signals:{score:{kind:'ref',field:'close',symbol:'$symbol',timeframe:'1d'}}}});
    const job=await call('quant_run_screen',{strategy_id:registered.data.strategy_id,data_snapshot_id:snapshot.data_snapshot_id,symbols:['AAPL','MSFT','QTEST'],as_of:spec.as_of,score_signal:'score'});
    let completed=false;
    for(let i=0;i<40;i++){const status=await call('quant_get_job',{job_id:job.data.job_id});if(status.data.status==='completed'){completed=true;break;}assert.notEqual(status.data.status,'failed');await new Promise(r=>setTimeout(r,100));}
    assert.equal(completed,true);
    const result=await call('quant_get_result',{job_id:job.data.job_id,section:'screen'});
    assert.equal(result.data.rows.length,3);
    assert.equal(result.data.rows.at(-1).score,null);
    const trace=readFileSync(env.QUANT_EVAL_TRACE,'utf8').trim().split('\n').map(JSON.parse);
    const outcome=grade({scenario:'screening',exitCode:0,finalReason:'completed',trace,metadata:spec.metadata,claims:[{kind:'ranking',evidence:{callId:result.id,path:'/rows'},value:result.data.rows},{kind:'limitation',value:'caller_selected_universe'},{kind:'provenance',value:spec.metadata}]});
    assert.equal(outcome.status,'PASS',JSON.stringify(outcome));
  }finally{await client.close();}
});
