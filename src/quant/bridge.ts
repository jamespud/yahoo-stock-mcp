import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { delimiter } from "node:path";

let activeRequests = 0;
const runtimePath = fileURLToPath(new URL("../../quant/src", import.meta.url));

/** Trusted control RPC; Python artifacts remain data until an isolated worker loads them. */
export async function quantRequest(request: Record<string, unknown>): Promise<unknown> {
  const input = JSON.stringify(request);
  if (Buffer.byteLength(input) > 1_048_576) throw new Error("RESOURCE_LIMIT_EXCEEDED: request exceeds 1 MiB");
  if (activeRequests >= 4) throw new Error("RESOURCE_LIMIT_EXCEEDED: at most four quant requests may run concurrently");
  activeRequests++;
  return new Promise<unknown>((resolve, reject) => {
    const child = spawn(process.env.QUANT_PYTHON || "python3", ["-m", "stock_quant"], {
      env: { ...process.env, PYTHONPATH: [runtimePath, process.env.PYTHONPATH].filter(Boolean).join(delimiter) },
      stdio: ["pipe", "pipe", "pipe"],
    });
    const chunks: Buffer[] = [];
    let size = 0;
    let stderr = "";
    let failed = false;
    const timer = setTimeout(() => fail(new Error("RESOURCE_LIMIT_EXCEEDED: quant request timed out")), 60_000);
    function fail(error: Error) {
      if (failed) return;
      failed = true;
      clearTimeout(timer);
      child.kill("SIGKILL");
      reject(error);
    }
    child.on("error", fail);
    child.stdin.on("error", fail);
    child.stdout.on("data", (chunk: Buffer) => {
      size += chunk.length;
      if (size > 4_194_304) return fail(new Error("RESOURCE_LIMIT_EXCEEDED: quant response exceeds 4 MiB"));
      chunks.push(chunk);
    });
    child.stderr.on("data", (chunk: Buffer) => { if (stderr.length < 4096) stderr += chunk.toString().slice(0, 4096 - stderr.length); });
    child.on("close", (code) => {
      clearTimeout(timer);
      if (failed) return;
      try {
        const response = JSON.parse(Buffer.concat(chunks).toString());
        if (response.error) throw new Error(`${response.error.code}: ${response.error.message}`);
        if (code !== 0) throw new Error(`Quant runtime exited ${code}`);
        resolve(response.result);
      } catch (error) {
        reject(error instanceof SyntaxError ? new Error(`Quant runtime unavailable: ${stderr || "invalid response"}`) : error);
      }
    });
    child.stdin.end(input);
  }).finally(() => { activeRequests--; });
}
