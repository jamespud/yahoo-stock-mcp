import {refreshSummary} from './refresh-quant-summary.mjs';
// Record actual commands; never convert a failed or unavailable command into PASS.
import { spawn } from 'node:child_process';
import { mkdirSync, readFileSync, writeFileSync, existsSync } from 'node:fs';
import { resolve } from 'node:path';
const root = resolve(import.meta.dirname, '..');
mkdirSync(resolve(root, 'reports'), {recursive:true});
const commands = ['sync:skill-references','build:plugin','test:skills','test:skills:quant','test:plugin','test:quant','test:quant-mcp','test:quant-regression','test:quant-semantic','typecheck','build','test','verify:pack'];
const results = [];
for (const script of commands) {
  console.log(`Running npm run ${script}`);
  const start = Date.now();
  const outcome = await new Promise(resolveResult => {
    const child = spawn('npm',['run', script], {cwd:root, detached:process.platform !== 'win32', env:{...process.env, UV_CACHE_DIR:process.env.UV_CACHE_DIR || '/tmp/quant-uv-cache', npm_config_cache:process.env.npm_config_cache || '/tmp/quant-npm-cache'}, stdio:['ignore','pipe','pipe']});
    let output = '';
    child.stdout.on('data', d=>{output+=d;}); child.stderr.on('data', d=>{output+=d;});
    const timer = setTimeout(()=>{ try { if(process.platform !== 'win32') process.kill(-child.pid,'SIGTERM'); else child.kill('SIGTERM'); } catch(e) { if(e.code !== 'ESRCH') throw e; } },600_000);
    child.on('error', error=>{clearTimeout(timer); resolveResult({code:null, output:output+'\n'+error.message});});
    child.on('close',(code,signal)=>{clearTimeout(timer); resolveResult({code,signal,output});});
  });
  writeFileSync(resolve(root, `reports/acceptance-${script.replaceAll(':','-')}.log`), outcome.output);
  const row = {command:`npm run ${script}`, status:outcome.code === 0 ? 'PASSED':'FAILED', exit_code:outcome.code, signal:outcome.signal, seconds:(Date.now()-start)/1000};
  results.push(row); console.log(JSON.stringify(row));
  writeFileSync(resolve(root,'reports/quant-acceptance.json'), JSON.stringify(results,null,2)+'\n');
}
const reliabilityPath=resolve(root,'reports/quant-reliability.json');
if(existsSync(reliabilityPath)) {
  const reliability=JSON.parse(readFileSync(reliabilityPath,'utf8'));
  const semantic=results.find(r=>r.command==='npm run test:quant-semantic');
  reliability.semantic_grader_results={status:semantic?.status || 'NOT_TESTED', scope:'Deterministic grader tests; real LLM claim evaluation recorded separately.', log:'acceptance-test-quant-semantic.log'};
  writeFileSync(reliabilityPath,JSON.stringify(reliability,null,2)+'\n');
  const md=resolve(root,'reports/quant-reliability.md');
  if(existsSync(md)) writeFileSync(md,readFileSync(md,'utf8').replace(/semantic_grader_results: \w+/,`semantic_grader_results: ${semantic?.status || 'NOT_TESTED'}`));
}
const path=resolve(root,'reports/quant-test-summary.json');
const summary=existsSync(path)?JSON.parse(readFileSync(path,'utf8')):{};
summary.acceptance=results;
const skillPath=resolve(root,'reports/quant-skills-sync.json');
if(existsSync(skillPath)) summary.skills_sync={report:'quant-skills-sync.json'};
writeFileSync(path, JSON.stringify(summary,null,2)+'\n');
writeFileSync(resolve(root,'reports/quant-test-summary.md'), '# Quant acceptance\n\n'+results.map(r=>`- ${r.status}: \`${r.command}\` (${r.seconds}s)`).join('\n')+'\n\nSee quant-test-summary.json and individual logs for evidence. Real DSH outcomes are recorded separately and are not implied by deterministic results.\n');
refreshSummary();
process.exitCode=results.some(r=>r.status==='FAILED')?1:0;
