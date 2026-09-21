import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const pkg = JSON.parse(readFileSync(resolve(root, "package.json"), "utf8"));

export function expectedReleaseTag(version) {
  return `v${version}`;
}

export function verifyReleaseTag(tag, version) {
  const expected = expectedReleaseTag(version);
  if (tag !== expected) {
    throw new Error(`release tag ${JSON.stringify(tag)} does not match package version; expected ${JSON.stringify(expected)}`);
  }
  return expected;
}

const isDirectRun = process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isDirectRun) {
  const tag = process.argv[2];
  if (!tag) {
    console.error("usage: node scripts/verify-release-tag.mjs <tag>");
    process.exit(2);
  }

  try {
    const expected = verifyReleaseTag(tag, pkg.version);
    console.log(`release tag verified: ${expected}`);
  } catch (err) {
    console.error(err instanceof Error ? err.message : String(err));
    process.exit(1);
  }
}
