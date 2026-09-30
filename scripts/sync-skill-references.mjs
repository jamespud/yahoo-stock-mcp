#!/usr/bin/env node
import { existsSync, mkdirSync, readFileSync, readdirSync, statSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const skillsDir = resolve(root, "skills");
const sourcePath = resolve(skillsDir, "references/data-policy.md");
const check = process.argv.includes("--check");

if (!existsSync(sourcePath)) throw new Error("skills/references/data-policy.md is missing");
const source = readFileSync(sourcePath, "utf8");

const skills = readdirSync(skillsDir)
  .filter((name) => name !== "references")
  .filter((name) => statSync(join(skillsDir, name)).isDirectory())
  .filter((name) => existsSync(join(skillsDir, name, "SKILL.md")))
  .sort();

if (skills.length === 0) throw new Error("no skills found under skills/");

let drift = false;
for (const name of skills) {
  const dest = resolve(skillsDir, name, "references/data-policy.md");
  if (check) {
    if (!existsSync(dest) || readFileSync(dest, "utf8") !== source) {
      console.error(`${name}: references/data-policy.md is missing or out of sync`);
      drift = true;
    }
  } else {
    mkdirSync(dirname(dest), { recursive: true });
    writeFileSync(dest, source);
    console.log(`synced ${name}/references/data-policy.md`);
  }
}

if (drift) {
  console.error("Run npm run sync:skill-references and commit the generated copies.");
  process.exit(1);
}
if (check) console.log(`skill reference copies OK: ${skills.length} skills`);
