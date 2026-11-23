import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import type { ChildProcessWithoutNullStreams } from "node:child_process";
import { once } from "node:events";
import { existsSync, readFileSync, readdirSync, statSync, mkdtempSync, rmSync, cpSync } from "node:fs";
import { dirname, isAbsolute, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import readline from "node:readline";
// @ts-ignore JavaScript report helper shared with plugin validation.
import { refreshSkillReport } from "./sync-skill-references.mjs";
import { tmpdir } from "node:os";

/**
 * Structural checks for the skills/ tree. Behavioural evaluation needs a model, so this script
 * covers what is deterministic: frontmatter, reference integrity, and - most importantly - that
 * every MCP tool a skill tells the agent to call actually exists in the running server's
 * `tools/list`. A skill must not be able to reference a tool the server does not expose.
 */

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const skillsDir = resolve(root, "skills");
const sharedPolicy = "skills/references/data-policy.md";
const dataPolicyLink = "references/data-policy.md";

interface Skill {
  dir: string;
  name: string;
  description: string;
  body: string;
  tools: string[];
}

/** Minimal frontmatter parser: a `---` fence, `key: value` lines, optional nested blocks ignored. */
function parseFrontmatter(file: string, raw: string): Record<string, string> {
  const lines = raw.split(/\r?\n/);
  assert.equal(lines[0], "---", `${file}: SKILL.md must start with a YAML frontmatter fence`);
  const end = lines.indexOf("---", 1);
  assert.ok(end > 1, `${file}: frontmatter is not closed`);
  const fields: Record<string, string> = {};
  for (const line of lines.slice(1, end)) {
    if (!line.trim() || /^\s/.test(line)) continue; // blank line or nested block
    const match = /^([A-Za-z0-9_-]+):\s*(.*)$/.exec(line);
    assert.ok(match, `${file}: unsupported frontmatter line ${JSON.stringify(line)}`);
    fields[match![1]] = match![2].trim();
  }
  return fields;
}

function splitSkill(raw: string): { frontmatter: string; body: string } {
  const lines = raw.split(/\r?\n/);
  const end = lines.indexOf("---", 1);
  return { frontmatter: lines.slice(0, end + 1).join("\n"), body: lines.slice(end + 1).join("\n") };
}

/** Collects relative markdown link targets from the body. */
function relativeLinks(body: string): string[] {
  const out = new Set<string>();
  for (const m of body.matchAll(/\]\(([^)\s]+)\)/g)) {
    const target = m[1];
    if (/^[a-z][a-z0-9+.-]*:/i.test(target) || target.startsWith("#") || target.startsWith("/")) continue;
    out.add(target.split("#")[0]);
  }
  return [...out];
}

/** Every relative markdown link in `body` must resolve against `baseDir`. */
function checkRelativeLinks(displayPath: string, baseDir: string, body: string): string[] {
  const links = relativeLinks(body);
  for (const link of links) {
    assert.ok(
      existsSync(resolve(baseDir, link)),
      `${displayPath} links ${link}, which does not exist`
    );
  }
  return links;
}

function checkStandaloneLinks(displayPath: string, baseDir: string, body: string): void {
  for (const link of relativeLinks(body)) {
    const target = resolve(baseDir, link);
    const rel = relative(baseDir, target);
    assert.ok(
      rel !== ".." && !rel.startsWith(`..${sep}`) && !isAbsolute(rel),
      `${displayPath} links outside its install directory: ${link}`
    );
  }
}

/** Reads the `## MCP tools used` section and returns the tool names listed there. */
function declaredTools(file: string, body: string): string[] {
  const lines = body.split(/\r?\n/);
  const start = lines.findIndex((l) => l.trim() === "## MCP tools used");
  assert.ok(start >= 0, `${file}: missing a "## MCP tools used" section`);
  const rest = lines.slice(start + 1);
  const end = rest.findIndex((l) => /^##\s/.test(l));
  const section = (end === -1 ? rest : rest.slice(0, end)).filter((l) => l.trim());
  assert.ok(section.length > 0, `${file}: "## MCP tools used" is empty`);
  const tools = section.map((line, idx) => {
    const match = /^- `([a-z][a-z0-9_]*)`/.exec(line.trim());
    assert.ok(
      match,
      `${file}: tool entry ${idx + 1} must look like "- tool_name - purpose", got ${JSON.stringify(line)}`
    );
    return match![1];
  });
  assert.equal(new Set(tools).size, tools.length, `${file}: duplicate tool entries`);
  return tools;
}

function loadSkills(): Skill[] {
  assert.ok(existsSync(skillsDir), "skills/ directory is missing");
  const entries = readdirSync(skillsDir)
    .filter((name) => statSync(join(skillsDir, name)).isDirectory())
    .filter((name) => name !== "references")
    .sort();
  assert.ok(entries.length > 0, "no skills found under skills/");

  return entries.map((name) => {
    const dir = join(skillsDir, name);
    const skillFile = join(dir, "SKILL.md");
    assert.ok(existsSync(skillFile), `${name}/SKILL.md is missing`);
    const raw = readFileSync(skillFile, "utf8");
    const { body } = splitSkill(raw);
    const fields = parseFrontmatter(`skills/${name}/SKILL.md`, raw);

    assert.equal(fields.name, name, `skills/${name}/SKILL.md: frontmatter name must match the directory`);
    assert.match(
      name,
      /^[a-z0-9]+(-[a-z0-9]+)*$/,
      `skills/${name}: directory name must be lowercase words separated by hyphens`
    );
    assert.ok(fields.description, `skills/${name}/SKILL.md: frontmatter description is required`);
    assert.ok(
      !fields.description.includes("\n") && fields.description.trim() === fields.description,
      `skills/${name}/SKILL.md: description must be a single trimmed line`
    );
    assert.ok(
      fields.description.length <= 1024,
      `skills/${name}/SKILL.md: description is ${fields.description.length} chars (max 1024)`
    );

    return { dir, name, description: fields.description, body, tools: declaredTools(name, body) };
  });
}

class McpProbe {
  private nextId = 1;
  private pending = new Map<number, (msg: any) => void>();
  private exitInfo: [number | null, string | null] | null = null;

  constructor(private child: ChildProcessWithoutNullStreams) {
    child.on("exit", (code, signal) => {
      this.exitInfo = [code, signal];
    });
  }

  start() {
    readline.createInterface({ input: this.child.stdout }).on("line", (line) => {
      let msg: any;
      try {
        msg = JSON.parse(line);
      } catch {
        return;
      }
      if (msg?.id !== undefined && this.pending.has(msg.id)) {
        const resolvePending = this.pending.get(msg.id)!;
        this.pending.delete(msg.id);
        resolvePending(msg);
      }
    });
    this.child.stderr.on("data", (d) => process.stderr.write(d));
  }

  request(method: string, params: unknown, timeoutMs = 10_000): Promise<any> {
    const id = this.nextId++;
    const p = new Promise<any>((resolvePending) => this.pending.set(id, resolvePending));
    this.child.stdin.write(JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n");
    return new Promise((resolvePromise, reject) => {
      const timer = setTimeout(() => reject(new Error(`timeout waiting for ${method}`)), timeoutMs);
      p.then(
        (v) => {
          clearTimeout(timer);
          resolvePromise(v);
        },
        (e) => {
          clearTimeout(timer);
          reject(e);
        }
      );
    });
  }

  notify(method: string, params?: unknown) {
    this.child.stdin.write(JSON.stringify({ jsonrpc: "2.0", method, params }) + "\n");
  }

  waitExit(ms: number): Promise<[number | null, string | null]> {
    if (this.exitInfo) return Promise.resolve(this.exitInfo);
    return new Promise((resolveExit, reject) => {
      const timer = setTimeout(() => reject(new Error("timeout waiting for exit")), ms);
      once(this.child, "exit").then(value => {
        clearTimeout(timer);
        resolveExit(value as [number | null, string | null]);
      }, reject);
    });
  }
}

/** The real, running tool surface - the skills are checked against this, not against a list here. */
async function liveToolNames(enabled: boolean, skills: Skill[]): Promise<string[]> {
  const tsxBin = resolve(root, "node_modules/.bin/tsx" + (process.platform === "win32" ? ".cmd" : ""));
  const temp = mkdtempSync(join(tmpdir(), "skills-mcp-"));
  const child = spawn(tsxBin, ["src/cli.ts", "server"], { cwd: root,
    env: {...process.env, QUANT_ENABLED: enabled ? "1" : "0", QUANT_SANDBOX_IMAGE: "",
      QUANT_PYTHON: enabled ? (process.env.QUANT_PYTHON || resolve(root, "quant/.venv/bin/python")) : join(temp, "python-must-not-run"),
      QUANT_REGISTRY_PATH: join(temp, "quant.db"), YAHOO_STOCK_MCP_SQLITE_PATH: join(temp, "data.db")},
    stdio: ["pipe", "pipe", "pipe"] });
  const probe = new McpProbe(child);
  probe.start();
  try {
    const init = await probe.request("initialize", {
      protocolVersion: "2025-11-25",
      capabilities: {},
      clientInfo: { name: "skills-probe", version: "1.0.0" },
    });
    assert.ok(init.result, `initialize failed: ${JSON.stringify(init)}`);
    probe.notify("notifications/initialized");
    const list = await probe.request("tools/list");
    assert.ok(list.result?.tools, JSON.stringify(list));
    const names: string[] = list.result.tools.map((t: any) => t.name).sort();
    if (!enabled) {
      assert.equal(names.some(name => name.startsWith("quant_")), false);
      assert.equal(existsSync(join(temp, "quant.db")), false);
    }
    else {
      async function call(name: string, args: unknown = {}) {
        const response = await probe.request("tools/call", {name, arguments: args}, 40_000);
        assert.ok(!response.error && !response.result?.isError, JSON.stringify(response));
        return JSON.parse(response.result.content[0].text);
      }
      const caps = await call("quant_get_capabilities");
      assert.equal(caps.mcp_execution_enabled, true);
      assert.ok(caps.dsl_schema && caps.strategy_modes.includes("dsl"));
      assert.equal(caps.python_sandbox, false);
      assert.equal(caps.sandbox.available, false);
      assert.ok(caps.research.methods.includes("walk_forward"));
      for (const section of ["reproducibility", "artifacts", "analytics", "trials", "walk_forward"])
        assert.ok(caps.result_sections.includes(section));
      const quantToolSources = ["src/quant/tools.ts", "src/quant/alpha-tools.ts"].map(path => readFileSync(resolve(root, path), "utf8")).join("\n");
      const registeredNames = [...quantToolSources.matchAll(/server\.(?:tool|registerTool)\(\s*"(quant_[a-z_]+)"/g)].map(m => m[1]).sort();
      assert.deepEqual(names.filter(n => n.startsWith("quant_")), registeredNames);
      for (const skill of skills.filter(s => s.name === "quant-strategy-research")) {
        const examples = resolve(skill.dir, "references/strategy-examples.json");
        for (const example of JSON.parse(readFileSync(examples, "utf8"))) {
          const result = await call("quant_validate_strategy", example.request);
          assert.equal(result.valid, example.valid, `${skill.name}/${example.name}: ${JSON.stringify(result)}`);
          if (example.error_code) assert.ok(result.errors.some((e: any) => e.code === example.error_code));
        }
      }
      const markdown = readFileSync(resolve(skillsDir, "quant-strategy-research/references/dsl-examples.md"), "utf8");
      const blocks = [...markdown.matchAll(/```json\n([\s\S]*?)\n```/g)];
      assert.equal(blocks.length, 4, "expected four complete Markdown validation requests");
      for (const [index, block] of blocks.entries()) {
        const result = await call("quant_validate_strategy", JSON.parse(block[1]));
        assert.equal(result.valid, index !== blocks.length - 1, JSON.stringify(result));
      }
      console.log("quant capabilities and 12 DSL validation requests OK (9 positive, 3 negative)");
    }
    return names;
  } finally {
    child.stdin.end();
    try {
      const [code, signal] = await probe.waitExit(5000);
      assert.equal(signal, null, `server killed by ${signal}`);
      assert.equal(code, 0, `server exit code ${code}`);
    } finally {
      if (child.exitCode === null && child.signalCode === null) child.kill("SIGKILL");
      rmSync(temp, {recursive: true, force: true});
    }
  }
}

async function main() {
  const skills = loadSkills();
  const names = skills.map((s) => s.name);
  for (const name of ["quant-strategy-research", "quant-research-reliability", "quant-alpha-research"]) assert.ok(names.includes(name), `missing ${name}`);
  assert.equal(names.length, 10);
  const requiredRefs: Record<string, string[]> = {
    "quant-strategy-research": ["data-policy", "quant-research-policy", "workflow", "dsl-examples", "result-interpretation"],
    "quant-research-reliability": ["data-policy", "quant-research-policy", "reliability-checklist", "benchmark-policy", "report-template"],
    "quant-alpha-research": ["data-policy", "quant-research-policy", "factor-policy", "option-policy", "model-validation", "report-template"]
  };
  for (const [name, refs] of Object.entries(requiredRefs)) {
    const skill = skills.find(s => s.name === name)!;
    for (const ref of refs) {
      assert.ok(existsSync(resolve(skill.dir, `references/${ref}.md`)), `${name}: missing ${ref}.md`);
      assert.ok(skill.body.includes(`references/${ref}.md`), `${name}: unlinked ${ref}.md`);
    }
  }
  console.log(`skills found: ${names.join(", ")}`);

  // The shared policy is the one file every skill must point at.
  assert.ok(existsSync(resolve(root, sharedPolicy)), `${sharedPolicy} is missing`);
  const policy = readFileSync(resolve(root, sharedPolicy), "utf8");
  assert.ok(policy.trim().length > 0, `${sharedPolicy} is empty`);

  for (const skill of skills) {
    const where = `skills/${skill.name}`;
    assert.ok(
      skill.body.includes(dataPolicyLink),
      `${where}/SKILL.md must link the shared data policy (${dataPolicyLink})`
    );
    checkRelativeLinks(`${where}/SKILL.md`, skill.dir, skill.body);
    checkStandaloneLinks(`${where}/SKILL.md`, skill.dir, skill.body);
    console.log(`  ${skill.name}: ${skill.tools.length} tools -> ${skill.tools.join(", ")}`);
  }

  // Copy each directory alone: no sibling or repository file may be needed at install time.
  const isolated = mkdtempSync(join(tmpdir(), "skills-install-"));
  try {
    for (const skill of skills) {
      const dest = join(isolated, skill.name);
      cpSync(skill.dir, dest, {recursive: true});
      function verify(dir: string) {
        for (const name of readdirSync(dir)) {
          const file = join(dir, name);
          if (statSync(file).isDirectory()) verify(file);
          else if (name.endsWith(".md")) {
            const body = readFileSync(file, "utf8");
            checkRelativeLinks(file, dir, body);
            for (const link of relativeLinks(body)) {
              const rel = relative(dest, resolve(dir, link));
              assert.ok(rel !== ".." && !rel.startsWith(`..${sep}`) && !isAbsolute(rel), `${file}: outside install`);
            }
          }
        }
      }
      verify(dest);
    }
  } finally { rmSync(isolated, {recursive: true, force: true}); }
  const defaultTools = await liveToolNames(false, skills);
  const quantMode = process.argv.includes("--quant");
  const tools = quantMode ? await liveToolNames(true, skills) : defaultTools;
  for (const name of defaultTools) assert.ok(tools.includes(name), `quant mode lost ${name}`);
  for (const skill of skills.filter(s => !s.name.startsWith("quant-"))) {
    for (const tool of skill.tools) assert.ok(defaultTools.includes(tool), `${skill.name}: ${tool} needs opt-in`);
  }
  const known = new Set(tools);
  const referenced = [...new Set(skills.filter(s => quantMode || !s.name.startsWith("quant-")).flatMap((s) => s.tools))].sort();
  const unknown = referenced.filter((name) => !known.has(name));
  assert.deepEqual(
    unknown,
    [],
    `skills reference tools the server does not expose: ${unknown.join(", ")} (server has ${tools.length} tools)`
  );
  console.log(`all ${referenced.length} referenced tools exist in the live tools/list (${tools.length} tools)`);

  const readme = resolve(skillsDir, "README.md");
  assert.ok(existsSync(readme), "skills/README.md is missing");
  const readmeBody = readFileSync(readme, "utf8");
  checkRelativeLinks("skills/README.md", skillsDir, readmeBody);
  assert.ok(
    readmeBody.includes("references/data-policy.md"),
    "skills/README.md must point at the shared data policy"
  );
  for (const skill of skills) {
    assert.ok(
      readmeBody.includes(`${skill.name}/SKILL.md`),
      `skills/README.md does not list ${skill.name}`
    );
  }

  refreshSkillReport({skills: Object.fromEntries(skills.map(skill => [skill.name, {
    schema: "PASS", reference_integrity: "PASS",
    default_mode: skill.name.startsWith("quant-") ? "OPT_IN_NOT_EXPOSED" : "PASS",
    ...(!skill.name.startsWith("quant-") || quantMode ? {resolved_tools: skill.tools.filter(t => known.has(t))} : {}),
    ...(quantMode ? {quant_mode: "PASS"} : {})
  }])), catalogs: {default_mode: defaultTools, ...(quantMode ? {quant_mode: tools} : {})}});
  console.log("skills tests OK");
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
