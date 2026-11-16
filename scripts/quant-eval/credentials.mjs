/** Read launch credentials without touching source profiles or serializing keys. */
import {readFileSync,existsSync} from 'node:fs';
import {join,resolve} from 'node:path';
import {createRequire} from 'node:module';
import {spawnSync} from 'node:child_process';
import {parseEnv} from 'node:util';
function yamlParser(env) {
  const candidates=[env.QUANT_EVAL_DSH_PACKAGE_JSON,join(env.HOME||'','./.local/share/dsh-npm/node_modules/@deepseek-ai/dsh/package.json')];
  const global=spawnSync('npm',['root','-g'],{env,encoding:'utf8',timeout:10000});
  if(global.status===0)candidates.push(join(global.stdout.trim(),'@deepseek-ai/dsh/package.json'));
  for(const path of candidates.filter(Boolean))try{return createRequire(resolve(path))('yaml');}catch{}
  throw Error('Cannot read saved DSH credentials: installed DSH YAML parser unavailable; export DEEPSEEK_API_KEY');
}
export function credentials(env) {
  const provider=env.QUANT_EVAL_PROVIDER||'sub2api-go';
  let apiKey=env.QUANT_EVAL_API_KEY?.trim();let source=apiKey?'QUANT_EVAL_API_KEY launch environment':undefined;
  const home=env.QUANT_EVAL_HEADLESS_HOME||env.DSH_HOME||join(env.HOME||'','.dsh');
  if(!apiKey&&existsSync(join(home,'.env'))){apiKey=parseEnv(readFileSync(join(home,'.env'),'utf8'))[provider.toUpperCase().replaceAll('-','_')+'_API_KEY']?.trim();if(apiKey)source='DSH home '+provider+' .env';}
  const store=env.QUANT_EVAL_CREDENTIALS_FILE||join(home,'.credentials.yaml');
  if(!apiKey&&existsSync(store)) {const data=yamlParser(env).parse(readFileSync(store,'utf8'));apiKey=data?.refs?.[provider.toUpperCase().replaceAll('-','_')+'_API_KEY'];if(typeof apiKey!=='string')apiKey=undefined;if(apiKey)source='saved '+provider+' credential reference';}
  let baseURL=env.QUANT_EVAL_BASE_URL;
  let model=env.QUANT_EVAL_MODEL;
  const reasoningEffort=env.QUANT_EVAL_REASONING_EFFORT;
  // Route copying is explicit. Copy only the endpoint/model selection, never the GUI composition.
  let providerConfig=null;
  if(env.QUANT_EVAL_PROFILE_SOURCE){const rows=yamlParser(env).parseDocument(readFileSync(env.QUANT_EVAL_PROFILE_SOURCE,'utf8')).toJS();const llm=rows.find(r=>r.id==='llm-pi-ai')?.config;providerConfig=llm?.providers?.[provider]||null;baseURL??=providerConfig?.baseURL;model??=rows.find(r=>r.id==='agent-default-model')?.config?.model;}
  return {apiKey,baseURL,model,reasoningEffort,provider,providerConfig,source};
}
