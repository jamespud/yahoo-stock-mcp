import {readFileSync,writeFileSync,existsSync,mkdirSync} from 'node:fs';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
const root=resolve(import.meta.dirname,'..');
export function refreshSummary(dshReport) {
  const dir=resolve(root,'reports');mkdirSync(dir,{recursive:true});
  const load=name=>existsSync(resolve(dir,name))?JSON.parse(readFileSync(resolve(dir,name),'utf8')):null;
  const summary=load('quant-test-summary.json')||{schema_version:'0.2'};
  const acceptance=load('quant-acceptance.json');if(acceptance)summary.acceptance=acceptance;
  const reliability=load('quant-reliability.json');
  if(reliability)summary.reliability={status:reliability.correctness_status,counts:reliability.counts,report:'quant-reliability.json'};
  if(load('quant-skills-sync.json'))summary.skills_sync={report:'quant-skills-sync.json'};
  const preflight=load('quant-mcp-preflight.json');if(preflight)summary.mcp_preflight={status:preflight.status,report:'quant-mcp-preflight.json',scope:preflight.scope};
  const dsh=dshReport||load('dsh/dsh-result.json');
  if(dsh) {delete summary.dsh;summary.real_dsh={status:dsh.status,provider:dsh.provider,model:dsh.model,reasoning_effort:dsh.reasoning_effort,scenarios_attempted:dsh.scenarios_attempted,scenarios_passed:dsh.scenarios_passed,success_rate:dsh.success_rate,scenario_reports:dsh.scenario_reports,job_ids:dsh.job_ids,report:'dsh/dsh-result.json'};}
  if(reliability&&dsh) {
    reliability.semantic_grader_results.real_llm_evaluation={status:dsh.status,report:'dsh/dsh-result.json',scope:'Autonomous run and claim-evidence grading; separate from deterministic grader tests.'};
    writeFileSync(resolve(dir,'quant-reliability.json'),JSON.stringify(reliability,null,2)+'\n');
  }
  writeFileSync(resolve(dir,'quant-test-summary.json'),JSON.stringify(summary,null,2)+'\n');
  writeFileSync(resolve(dir,'quant-test-summary.md'),'# Quant acceptance\n\n'+(acceptance||[]).map(r=>`- ${r.status}: \`${r.command}\``).join('\n')+`\n\nDeterministic reliability: ${summary.reliability?.status||'NOT_TESTED'}. Real DSH: ${summary.real_dsh?.status||'NOT_TESTED'}.\n\n`+Object.entries(dsh?.scenario_reports||{}).map(([scenario,report])=>`- ${scenario}: ${report.status}; ${report.runs_passed}/${report.runs_attempted} runs; ${report.report}`).join('\n')+'\n\nSee linked JSON reports, JUnit and logs for scope and evidence. A deterministic PASS does not imply an autonomous LLM PASS.\n');
  return summary;
}
if(import.meta.url===pathToFileURL(process.argv[1]||'').href)refreshSummary();
