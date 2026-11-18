import { createHash, randomUUID } from "node:crypto";
import { dirname, basename, resolve } from "node:path";
import { mkdir, open, rename, rm } from "node:fs/promises";

const MAX_INLINE_ALPHA_BYTES = 768 * 1024;

export function alphaInboxRoot(registryPath: string): string {
  const dir = dirname(registryPath);
  const file = basename(registryPath);
  const stem = file.includes(".") ? file.slice(0, file.lastIndexOf(".")) : file;
  return resolve(dir, `${stem}.alpha-inbox`);
}

export function inlineAlphaBytes(value: unknown): number {
  return Buffer.byteLength(JSON.stringify(value));
}

export function canInlineAlphaRows(rows: unknown[]): boolean {
  return inlineAlphaBytes(rows) <= MAX_INLINE_ALPHA_BYTES;
}

export async function stageAlphaRows(registryPath: string, rows: unknown[]): Promise<string> {
  const root = alphaInboxRoot(registryPath);
  await mkdir(root, { recursive: true, mode: 0o700 });
  const body = Buffer.from(JSON.stringify({ rows }), "utf8");
  const hash = "sha256:" + createHash("sha256").update(body).digest("hex");
  const destination = resolve(root, hash.slice(7) + ".json");
  const temp = resolve(root, `.${randomUUID()}.tmp`);
  const handle = await open(temp, "wx", 0o600);
  try {
    await handle.writeFile(body);
    await handle.sync();
  } finally {
    await handle.close();
  }
  try {
    await rename(temp, destination);
  } catch (error) {
    await rm(temp, { force: true });
    const existing = await import("node:fs/promises").then(fs => fs.readFile(destination));
    if ("sha256:" + createHash("sha256").update(existing).digest("hex") !== hash) throw error;
  }
  return hash;
}
