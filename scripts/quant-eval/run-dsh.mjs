import {refreshSummary} from '../refresh-quant-summary.mjs';
import {spawn,spawnSync} from 'node:child_process';
import {existsSync,mkdirSync,mkdtempSync,writeFileSync,readFileSync,createWriteStream} from 'node:fs';
import {resolve,dirname,join} from 'node:path';
import {tmpdir} from 'node:os';
import {fileURLToPath} from 'node:url';
import {names,preflight,fixtureSpec,prompt} from './scenarios.mjs';
import {grade} from './semantic-grader.mjs';
import {credentials} from './credentials.mjs';
import {runPreflight} from './deterministic-preflight.mjs';
const here=dirname(fileURLToPath(import.meta.url));
const root=resolve(here,'../..');
async function capture(command,args,options) {
  const child=spawn(command,args,{...options,stdio:['ignore','pipe','pipe']});let out='',err='';
  child.stdout.on('data',x=>out+=x);child.stderr.on('data',x=>err+=x);
  const timer=setTimeout(()=>child.kill('SIGTERM'),30000);
  try {return await new Promise((resolve,reject)=>{child.once('error',reject);child.once('close',code=>code===0?resolve(out):reject(Error('Fixture generator failed: '+err.slice(-1000))));});}finally{clearTimeout(timer);}
}
const write=(path,value)=>writeFileSync(path,JSON.stringify(value,null,2)+'\n',{mode:0o600});
export function profilePatch({server,proxy,python,registry,database,trace,scenario,model,baseURL,reasoningEffort,provider='sub2api-go',providerConfig}) {
  // JSON is valid YAML. No credential is serialized into this overlay.
  return JSON.stringify([
    {id:'agent-default-model',config:{provider,model,reasoningEffort}},
    {id:'llm-pi-ai',name:'@deepseek-ai/dsh-llm-pi-ai',config:{providers:{[provider]:providerConfig||{displayName:provider,api:'openai-responses',baseURL,apiKeyEnv:provider.toUpperCase()+'_API_KEY',models:[{id:model,name:model,input:['text','image'],reasoningEfforts:{off:'none',low:'low',medium:'medium',high:'high',max:'max'}}]}}}},
    {insert:[{id:'eval-quant',name:'@deepseek-ai/dsh-mcp-client',config:{serverName:'quant',transport:'stdio',command:process.execPath,args:[proxy],env:{QUANT_ENABLED:'1',QUANT_PYTHON:python,QUANT_REGISTRY_PATH:registry,YAHOO_STOCK_MCP_SQLITE_PATH:database,QUANT_EVAL_SERVER:server,QUANT_EVAL_TRACE:trace,QUANT_EVAL_SCENARIO:scenario}}}]} ,
    // Disable native execution tools: model work must traverse the captured MCP bridge.
    ...['tool-bash','tool-pwsh','tool-fs','tool-fs-search','tool-web','tool-subagent','tool-subagent-fork','tool-subagent-control','tool-plugin-manager','tool-working-directory','tool-skill','tool-jobs','tool-workflow','tool-todo','tool-goal','mcp-resources','tool-mcp-resources'].map(id=>({id,disabled:true}))
  ],null,2);
}
export async function supervise(command,args,{cwd,env,timeoutMs,stdoutPath,stderrPath,input}) {
  const child=spawn(command,args,{cwd,env,detached:process.platform!=='win32',stdio:['pipe','pipe','pipe']});
  const out=createWriteStream(stdoutPath,{mode:0o600}); const err=createWriteStream(stderrPath,{mode:0o600});
  child.stdout.pipe(out);child.stderr.pipe(err);child.stdin.end(input);
  let timedOut=false;let killTimer;
  const kill=signal=>{try {if(process.platform!=='win32')process.kill(-child.pid,signal);else child.kill(signal);} catch(e){if(e.code!=='ESRCH')throw e;}};
  const timer=setTimeout(()=>{timedOut=true;kill('SIGTERM');killTimer=setTimeout(()=>kill('SIGKILL'),2000);},timeoutMs);
  const interrupt=()=>kill('SIGTERM');
  process.once('SIGINT',interrupt);process.once('SIGTERM',interrupt);
  let result;
  try {result=await new Promise(resolve=>{child.once('error',e=>resolve({exitCode:null,error:e.message}));child.once('close',(code,signal)=>resolve({exitCode:code,signal}));});}
  finally {clearTimeout(timer);clearTimeout(killTimer);process.off('SIGINT',interrupt);process.off('SIGTERM',interrupt);}
  await Promise.all([new Promise(r=>out.end(r)),new Promise(r=>err.end(r))]);
  return {...result,timedOut};
}
export async function seed(env,server,fixture,verify=false) {
  const {Client}=await import('@modelcontextprotocol/sdk/client/index.js');
  const {StdioClientTransport}=await import('@modelcontextprotocol/sdk/client/stdio.js');
  const client=new Client({name:'quant-eval-bootstrap',version:'1'});
  const transport=new StdioClientTransport({command:process.execPath,args:[server,'server'],env,stderr:'pipe'});
  // Drain diagnostics to avoid pipe backpressure; no credentials are passed to this child.
  transport.stderr?.resume();
  try {
    await client.connect(transport,{timeout:30000});
    const result=await client.callTool({name:'quant_import_snapshot',arguments:fixture.snapshot},undefined,{timeout:30000});
    if(result.isError) throw Error(JSON.stringify(result));
    const snapshot=JSON.parse(result.content[0].text);
    if(verify)snapshot.preflight=await runPreflight(client,snapshot,fixtureSpec(fixture));
    return snapshot;
  } finally {await client.close();}
}
/** Parent report runner can embed this return value without parsing console output. */
export async function runDSH(options={}) {
  const env=options.env??process.env;
  const auth=credentials(env);
  const selected=options.scenarios??names;
  const repeats=Number(env.QUANT_EVAL_REPEATS||1);
  if(!Number.isInteger(repeats)||repeats<1||repeats>10)throw Error('QUANT_EVAL_REPEATS must be 1..10');
  const output=resolve(options.outputDir??env.QUANT_EVAL_OUTPUT_DIR??mkdtempSync(join(tmpdir(),'quant-dsh-')));
  mkdirSync(output,{recursive:true,mode:0o700});
  const dsh=env.QUANT_EVAL_DSH_BIN||'dsh';
  const python=env.QUANT_PYTHON||join(root,'quant/.venv/bin/python');
  const server=options.server??join(root,'dist/cli.js');
  const timeoutMs=Number(env.QUANT_EVAL_TIMEOUT_MS||(Number(env.QUANT_EVAL_BUDGET_SECONDS||600)*1000));
  if(!Number.isFinite(timeoutMs)||timeoutMs<100||timeoutMs>1800000) throw Error('QUANT_EVAL_TIMEOUT_MS must be 100..1800000');
  const results=[];
  for(const scenario of Array.from({length:repeats},()=>selected).flat()) {
    const budget=preflight(scenario); // always runs, even when credentials are absent
    const dir=mkdtempSync(join(output,`${scenario}-`));
    write(join(dir,'spec.json'),{scenario,budget,synthetic:true});
    const missing=[];
    try {import.meta.resolve('@modelcontextprotocol/sdk/client/index.js');}catch{missing.push('Node MCP SDK dependencies (npm ci)');}
    if(!auth.model)missing.push('QUANT_EVAL_MODEL (explicit model supported by the configured provider)');
    if(!auth.apiKey)missing.push(auth.provider.toUpperCase()+'_API_KEY (launch env, headless .env, or saved DSH reference)');
    if(!existsSync(join(root,'scripts/export-quant-eval-fixture.py')))missing.push('scripts/export-quant-eval-fixture.py');
    if(!existsSync(server))missing.push('dist/cli.js (npm run build)');
    const py=spawnSync(python,['-c','import stock_quant, pydantic, pyarrow'],{timeout:15000,encoding:'utf8'});
    if(py.status!==0)missing.push('QUANT_PYTHON with stock_quant, pydantic and pyarrow');
    const home=join(dir,'dsh-home');mkdirSync(home,{mode:0o700});
    const workspace=join(dir,'workspace');mkdirSync(workspace,{mode:0o700});
    // Fresh cwd and DSH_HOME prevent fallback to either GUI credentials or repo .env.
    const launch={PATH:env.PATH,HOME:env.HOME,LANG:env.LANG||'C.UTF-8',DSH_HOME:home,[auth.provider.toUpperCase().replaceAll('-','_')+'_API_KEY']:auth.apiKey};
    const check=spawnSync(dsh,['--version'],{cwd:workspace,env:launch,timeout:15000,encoding:'utf8'});
    if(check.status!==0)missing.push('DSH executable (QUANT_EVAL_DSH_BIN)');
    let outcome;
    if(missing.length) outcome=grade({skipReason:`Missing dependencies: ${missing.join(', ')}`});
    else {
      const registry=join(dir,'quant.db'),database=join(dir,'market.db'),trace=join(dir,'trace.jsonl');
      const quantEnv={PATH:env.PATH,HOME:env.HOME,QUANT_ENABLED:'1',QUANT_PYTHON:python,QUANT_REGISTRY_PATH:registry,YAHOO_STOCK_MCP_SQLITE_PATH:database};
      try {
        const generated=await capture(python,[join(root,'scripts/export-quant-eval-fixture.py')],{cwd:workspace,env:quantEnv});
        const fixture=JSON.parse(generated);const spec=fixtureSpec(fixture);
        write(join(dir,'spec.json'),{scenario,budget:preflight(scenario,{windows:spec.windows}),...spec});
        write(join(dir,'fixture-metadata.json'),{...spec,bar_count:fixture.bar_count,seed:fixture.seed});
        const snapshot=await seed(quantEnv,server,fixture,true);
        write(join(dir,'preflight.json'),snapshot.preflight);
        write(join(dir,'bootstrap.json'),snapshot);
        const task=prompt(scenario,snapshot.data_snapshot_id,spec);
        writeFileSync(join(dir,'prompt.txt'),task,{mode:0o600});
        const patch=join(dir,'eval.patch.json');
        writeFileSync(patch,profilePatch({server,proxy:join(here,'mcp-proxy.mjs'),python,registry,database,trace,scenario,model:auth.model,baseURL:auth.baseURL,provider:auth.provider,providerConfig:auth.providerConfig,reasoningEffort:auth.reasoningEffort}),{mode:0o600});
        const execution=await supervise(dsh,['--profile','headless','--patch',patch,'--json','--approval-policy','never','-'],{cwd:workspace,env:launch,timeoutMs,stdoutPath:join(dir,'transcript.jsonl'),stderrPath:join(dir,'stderr.log'),input:task});
        write(join(dir,'execution.json'),execution);
        const events=readFileSync(join(dir,'transcript.jsonl'),'utf8').trim().split('\n').filter(Boolean).map(JSON.parse);
        const final=events.at(-1);
        if(execution.timedOut)throw Error(`DSH timeout after ${timeoutMs}ms`);
        if(final?.type!=='final'||!events.some(e=>e.type==='session'&&e.sessionId===final.sessionId))throw Error('Missing matching session/final event');
        if(execution.exitCode!==0||final.reason?.kind!=='completed')throw Error(`DSH ${final.reason?.error?.code||'INCOMPLETE'}: ${final.reason?.error?.message||'run did not complete'}`);
        const answer=JSON.parse(final.text.replace(/^```(?:json)?\s*|\s*```$/g,''));
        const captured=existsSync(trace)?readFileSync(trace,'utf8').trim().split('\n').filter(Boolean).map(JSON.parse):[];
        write(join(dir,'claims.json'),answer);
        outcome=grade({scenario,claims:answer.claims,trace:captured,exitCode:execution.exitCode,finalReason:final.reason?.kind,metadata:spec.metadata});
      } catch(e) {outcome={status:'FAIL',errors:[e.message]};}
    }
    const result={scenario,...outcome,credentialSource:auth.source??null,provider:auth.provider,model:auth.model,artifacts:dir};write(join(dir,'result.json'),result);results.push(result);
  }
  const summary={phase:'phase3-dsh',status:results.some(r=>r.status==='FAIL')?'FAIL':results.every(r=>r.status==='PASS')?'PASS':'SKIPPED',results,outputDir:output};
  write(join(output,'dsh-result.json'),summary);return summary;
}
if(process.argv[1]===fileURLToPath(import.meta.url)) {
  const args=process.argv.slice(2);const scenarios=args.filter(x=>!x.startsWith('--'));
  const outputDir=process.env.QUANT_EVAL_OUTPUT_DIR;
  try {const result=await runDSH({scenarios:scenarios.length?scenarios:undefined,outputDir});refreshSummary(result);console.log(JSON.stringify(result,null,2));process.exitCode=result.status==='FAIL'?1:result.status==='SKIPPED'?77:0;}
  catch(e){console.error(e.message);process.exitCode=1;}
}
