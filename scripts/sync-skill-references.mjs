#!/usr/bin/env node
import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, readFileSync, readdirSync, statSync, writeFileSync } from 'node:fs';
import { dirname, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const quantSkills = ['quant-strategy-research', 'quant-research-reliability'];
const sha256 = bytes => createHash('sha256').update(bytes).digest('hex');
const skillNames = () => readdirSync(resolve(root, 'skills')).filter(name => name !== 'references' && statSync(resolve(root, 'skills', name)).isDirectory() && existsSync(resolve(root, 'skills', name, 'SKILL.md'))).sort();
function files(dir) {
  if (!existsSync(dir)) return [];
  return readdirSync(dir).sort().flatMap(name => {
    const path = resolve(dir, name);
    return statSync(path).isDirectory() ? files(path) : [path];
  });
}
function treeHash(dir) {
  if (!existsSync(dir)) return null;
  return sha256(JSON.stringify(files(dir).map(path => [relative(dir, path).split('\\').join('/'), sha256(readFileSync(path))])));
}
function referenceEntries() {
  return skillNames().flatMap(name => ['data-policy.md', ...(quantSkills.includes(name) ? ['quant-research-policy.md', 'strategy-examples.json'] : [])].map(ref => {
    const source = `skills/references/${ref}`;
    const path = `skills/${name}/references/${ref}`;
    const expected = sha256(readFileSync(resolve(root, source)));
    const actual = existsSync(resolve(root, path)) ? sha256(readFileSync(resolve(root, path))) : null;
    return {source, path, source_sha256: expected, copy_sha256: actual, matches: expected === actual};
  }));
}
// Content fingerprints prevent an old successful observation from certifying edited tools/tests.
function validationFingerprint() {
  const paths = ['src/cli.ts', 'src/config.ts', 'src/mcp/server.ts', 'scripts/test-skills.ts',
    ...files(resolve(root, 'src/quant')).map(p => relative(root, p)),
    ...files(resolve(root, 'quant/src/stock_quant')).filter(p => p.endsWith('.py')).map(p => relative(root, p))];
  return sha256(JSON.stringify(paths.sort().map(path => [path, sha256(readFileSync(resolve(root, path)))])));
}
export function refreshSkillReport(observations = {}) {
  const jsonPath = resolve(root, 'reports/quant-skills-sync.json');
  let prior = {};
  if (existsSync(jsonPath)) prior = JSON.parse(readFileSync(jsonPath, 'utf8'));
  const fingerprint = validationFingerprint();
  const entries = referenceEntries();
  const rows = skillNames().map(name => {
    const sourceDir = resolve(root, 'skills', name);
    const bundledDir = resolve(root, 'plugins/yahoo-stock-mcp/skills', name);
    const source_hash = treeHash(sourceDir);
    const bundled_hash = treeHash(bundledDir);
    const previous = prior.skill_results?.find(row => row.name === name);
    const same = previous?.source_hash === source_hash && prior.validation_fingerprint === fingerprint;
    const raw = readFileSync(resolve(sourceDir, 'SKILL.md'), 'utf8');
    const section = raw.split('## MCP tools used')[1]?.split(/\n## /)[0] ?? '';
    const declared_tools = [...section.matchAll(/^- `([a-z][a-z0-9_]*)`/gm)].map(m => m[1]);
    const update = observations.skills?.[name] ?? {};
    return {name, source_hash, bundled_hash, declared_tools,
      resolved_tools: same ? previous.resolved_tools : [],
      schema: same ? previous.schema : 'NOT_TESTED',
      default_mode: same ? previous.default_mode : 'NOT_TESTED',
      quant_mode: same ? previous.quant_mode : 'NOT_TESTED',
      reference_integrity: same ? previous.reference_integrity : 'NOT_TESTED',
      plugin_packaging: previous?.source_hash === source_hash && previous?.bundled_hash === bundled_hash ? previous.plugin_packaging : 'NOT_TESTED',
      ...update};
  });
  const catalogs = {...(prior.validation_fingerprint === fingerprint ? prior.observed_catalogs : {}), ...observations.catalogs};
  const report = {schema_version: '2.0', algorithm: 'sha256', validation_fingerprint: fingerprint,
    skills: rows.length, quant_skills: quantSkills, copies: entries.length,
    matches: entries.every(e => e.matches), entries, observed_catalogs: catalogs, skill_results: rows};
  const md = `# Quant skill sync and validation\n\n${rows.length} independently installable skills; ${quantSkills.length} quant skills; ${entries.length} synchronized references.\n\nHash algorithm: SHA-256. Reference status: ${report.matches ? 'MATCH' : 'DRIFT'}. NOT_TESTED means no successful observation for this content fingerprint. OPT_IN_NOT_EXPOSED is the expected default quant-skill status.\n\n| Skill | Source hash | Bundled hash | Schema | Default mode | Quant mode | References | Plugin |\n| --- | --- | --- | --- | --- | --- | --- | --- |\n${rows.map(r => `| ${r.name} | ${r.source_hash} | ${r.bundled_hash ?? 'MISSING'} | ${r.schema} | ${r.default_mode} | ${r.quant_mode} | ${r.reference_integrity} | ${r.plugin_packaging} |`).join('\n')}\n\n## Tool resolution\n\n${rows.map(r => `- ${r.name}: declared ${r.declared_tools.join(', ')}; resolved ${r.resolved_tools.join(', ') || 'NOT_TESTED'}.`).join('\n')}\n\n## Shared references\n\n| Source | Copy | Source SHA-256 | Copy SHA-256 | Matches |\n| --- | --- | --- | --- | --- |\n${entries.map(e => `| ${e.source} | ${e.path} | ${e.source_sha256} | ${e.copy_sha256 ?? 'MISSING'} | ${e.matches} |`).join('\n')}\n`;
  mkdirSync(dirname(jsonPath), {recursive: true});
  writeFileSync(jsonPath, JSON.stringify(report, null, 2) + '\n');
  writeFileSync(resolve(root, 'reports/quant-skills-sync.md'), md);
  return report;
}
function main() {
  const check = process.argv.includes('--check');
  const names = skillNames();
  if (names.length !== 9) throw new Error(`expected nine skills, got ${names.length}`);
  for (const name of quantSkills) if (!names.includes(name)) throw new Error(`missing quant skill: ${name}`);
  if (!check) {
    for (const entry of referenceEntries()) {
      const path = resolve(root, entry.path);
      mkdirSync(dirname(path), {recursive: true});
      writeFileSync(path, readFileSync(resolve(root, entry.source)));
    }
  }
  const entries = referenceEntries();
  const errors = entries.filter(e => !e.matches).map(e => `reference drift: ${e.path}`);
  // --check is read-only and checks sources/copies, never mutable runtime observations.
  if (!check) refreshSkillReport();
  if (errors.length) { console.error(errors.join('\n')); console.error('Run npm run sync:skill-references.'); process.exitCode = 1; return; }
  console.log(`skill references ${check ? 'checked' : 'synced'}: ${names.length} skills, ${entries.length} SHA-256 matches`);
}
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) main();
