import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import type { ChildProcessWithoutNullStreams } from "node:child_process";
import { once } from "node:events";
import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import readline from "node:readline";

/**
 * Structural checks for the skills/ tree. Behavioural evaluation needs a model, so this script
 * covers what is deterministic: frontmatter, reference integrity, and - most importantly - that
 * every MCP tool a skill tells the agent to call actually exists in the running server's
 * `tools/list`. A skill must not be able to reference a tool the server does not expose.
 */

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const skillsDir = resolve(root, "skills");
const sharedPolicy = "skills/references/data-policy.md";
const dataPolicyLink = "../references/data-policy.md";

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
    return Promise.race([
      once(this.child, "exit") as Promise<[number | null, string | null]>,
      new Promise<never>((_, reject) => setTimeout(() => reject(new Error("timeout waiting for exit")), ms)),
    ]);
  }
}

/** The real, running tool surface - the skills are checked against this, not against a list here. */
async function liveToolNames(): Promise<string[]> {
  const tsxBin = resolve(root, "node_modules/.bin/tsx" + (process.platform === "win32" ? ".cmd" : ""));
  const child = spawn(tsxBin, ["src/cli.ts", "server"], { cwd: root, stdio: ["pipe", "pipe", "pipe"] });
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
    return list.result.tools.map((t: any) => t.name).sort();
  } finally {
    child.stdin.end();
    const [code, signal] = await probe.waitExit(5000);
    assert.equal(signal, null, `server killed by ${signal}`);
    assert.equal(code, 0, `server exit code ${code}`);
  }
}

async function main() {
  const skills = loadSkills();
  const names = skills.map((s) => s.name);
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
    console.log(`  ${skill.name}: ${skill.tools.length} tools -> ${skill.tools.join(", ")}`);
  }

  const tools = await liveToolNames();
  const known = new Set(tools);
  const referenced = [...new Set(skills.flatMap((s) => s.tools))].sort();
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

  console.log("skills tests OK");
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
