import {spawnSync} from 'node:child_process';
import {mkdtempSync,rmSync,writeFileSync,mkdirSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join,resolve} from 'node:path';
import {seed} from './quant-eval/run-dsh.mjs';
const root=resolve(import.meta.dirname,'..');
const temp=mkdtempSync(join(tmpdir(),'quant-eval-preflight-'));
const python=process.env.QUANT_PYTHON||join(root,'quant/.venv/bin/python');
try {
  const generated=spawnSync(python,['scripts/export-quant-eval-fixture.py'],{cwd:root,encoding:'utf8',maxBuffer:16*1024*1024,timeout:30000});
  if(generated.status!==0)throw Error(generated.stderr||'Fixture generation failed');
  const fixture=JSON.parse(generated.stdout);
  const result=await seed({PATH:process.env.PATH,HOME:process.env.HOME,QUANT_ENABLED:'1',QUANT_PYTHON:python,QUANT_REGISTRY_PATH:join(temp,'registry.db'),YAHOO_STOCK_MCP_SQLITE_PATH:join(temp,'data.db')},join(root,'dist/cli.js'),fixture,true);
  const report={...result.preflight,data_kind:'synthetic',data_snapshot_id:result.data_snapshot_id,fixture_hash:fixture.data_hash,fixture_version:fixture.fixture_version};
  mkdirSync(join(root,'reports'),{recursive:true});
  writeFileSync(join(root,'reports/quant-mcp-preflight.json'),JSON.stringify(report,null,2)+'\n');
  console.log(JSON.stringify(report));
} finally {rmSync(temp,{recursive:true,force:true});}
