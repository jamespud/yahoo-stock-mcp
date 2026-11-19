import {readFileSync,writeFileSync,existsSync,mkdirSync} from 'node:fs';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
const root=resolve(import.meta.dirname,'..');
export function refreshSummary() {
  const dir=resolve(root,'reports');mkdirSync(dir,{recursive:true});
  const load=name=>existsSync(resolve(dir,name))?JSON.parse(readFileSync(resolve(dir,name),'utf8')):null;
  const summary=load('quant-test-summary.json')||{schema_version:'0.2'};
  const acceptance=load('quant-acceptance.json');if(acceptance)summary.acceptance=acceptance;
  const reliability=load('quant-reliability.json');
  if(reliability)summary.reliability={status:reliability.correctness_status,counts:reliability.counts,report:'quant-reliability.json'};
  if(load('quant-skills-sync.json'))summary.skills_sync={report:'quant-skills-sync.json'};
  writeFileSync(resolve(dir,'quant-test-summary.json'),JSON.stringify(summary,null,2)+'\n');
  writeFileSync(resolve(dir,'quant-test-summary.md'),'# Quant acceptance\n\n'+(acceptance||[]).map(r=>`- ${r.status}: \`${r.command}\``).join('\n')+`\n\nDeterministic reliability: ${summary.reliability?.status||'NOT_TESTED'}.\n\nSee linked JSON reports, JUnit and logs for scope and evidence.\n`);
  return summary;
}
if(import.meta.url===pathToFileURL(process.argv[1]||'').href)refreshSummary();
