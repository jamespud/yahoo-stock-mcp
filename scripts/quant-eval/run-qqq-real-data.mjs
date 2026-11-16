/** Replay the pinned, downloaded QQQ dataset through the actual stdio MCP. No network in replay. */
import {spawnSync} from 'node:child_process';
import {existsSync,readFileSync,writeFileSync} from 'node:fs';
import {resolve,join} from 'node:path';
import {createHash} from 'node:crypto';
const root=resolve(import.meta.dirname,'../..');
const run=resolve(process.env.QQQ_RUN_DIR||join(root,'.cache/qqq-real-2026-11-14-curl'));
if(!existsSync(join(run,'acquisition.json')))throw Error('Run npm run quant:qqq:fetch first; no synthetic fallback.');
const python=process.env.QUANT_PYTHON||join(root,'quant/.venv/bin/python');
const phases=[['prepare',python,['scripts/quant-eval/prepare-qqq-real-data.py']],['mcp',process.execPath,['scripts/quant-eval/test-qqq-real-data.mjs']],['oracle',python,['scripts/quant-eval/verify-qqq-real-data.py']]];
const sha=x=>'sha256:'+createHash('sha256').update(JSON.stringify(x)).digest('hex');
const financial=()=>Object.fromEntries(['1m','5m','15m','1h','1d','1w'].flatMap(tf=>{const p=join(run,`result-${tf}.json`);if(!existsSync(p))return [];const r=JSON.parse(readFileSync(p,'utf8'));return [[tf,{job_id:r.job.job_id,hash:sha({summary:r.summary,fills:r.fills,orders:r.orders,equity:r.equity_curve,account:r.final_account}),snapshot_id:r.dataset.data_snapshot_id,plan_id:r.plan.plan_id}]];}));
const before=financial();writeFileSync(join(run,`replay-baseline-${Date.now()}.json`),JSON.stringify(before,null,2));
const commands=[];
for(const [phase,command,args] of phases){
 const began=Date.now();const child=spawnSync(command,args,{cwd:root,env:{...process.env,QQQ_RUN_DIR:run,PYTHONPATH:join(root,'quant/src')},encoding:'utf8',maxBuffer:16*1024*1024,timeout:240000});
 writeFileSync(join(root,`reports/qqq-real-data/replay-${phase}.log`),(child.stdout||'')+(child.stderr||''));
 const result={phase,exit_code:child.status,elapsed_ms:Date.now()-began,error:child.error?.message??null};commands.push(result);console.log(JSON.stringify(result));
 if(child.status!==0)throw Error(`QQQ ${phase} failed; see replay-${phase}.log`);
}
const after=financial();const results=Object.entries(after).map(([tf,result])=>({timeframe:tf,status:!before[tf]?'NOT_TESTED':before[tf].hash===result.hash&&before[tf].snapshot_id===result.snapshot_id&&before[tf].plan_id===result.plan_id?'PASSED':'FAILED',before:before[tf]??null,after:result}));
writeFileSync(join(root,'reports/qqq-real-data/replay.json'),JSON.stringify({version:'qqq-financial-replay-v1',commands,results,status:results.some(r=>r.status==='FAILED')?'FAILED':results.every(r=>r.status==='PASSED')?'PASSED':'NOT_TESTED'},null,2)+'\n');
if(results.some(r=>r.status==='FAILED'))throw Error('Financial replay mismatch');
const summary=spawnSync(process.execPath,['scripts/quant-eval/summarize-qqq-real-data.mjs'],{cwd:root,env:{...process.env,QQQ_RUN_DIR:run},stdio:'inherit',timeout:30000});
if(summary.status!==0)throw Error('QQQ report generation failed');
