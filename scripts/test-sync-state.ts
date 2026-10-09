/**
 * C5b-1b: `sync_state` transitions and the three sync entry points on SQLite.
 *
 * MySQL is actively blocked, not merely unused: `YAHOO_STOCK_MCP_DATABASE_URL` points at a dead
 * endpoint for the whole run, so any stray MySQL I/O fails immediately instead of passing quietly.
 */
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { openDatabase } from "../src/storage/database.js";
import { applySqliteMigrations } from "../src/storage/migrations.js";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const tmp = mkdtempSync(resolve(tmpdir(), "yahoo-stock-mcp-syncstate-"));
const dbPath = resolve(tmp, "state.db");
const setup = openDatabase(dbPath);
applySqliteMigrations(setup);
setup.close();

// ---- the active MySQL block: any MySQL I/O in this run cannot succeed ----
process.env.YAHOO_STOCK_MCP_STORAGE_BACKEND = "sqlite";
process.env.YAHOO_STOCK_MCP_SQLITE_PATH = dbPath;
process.env.YAHOO_STOCK_MCP_DATABASE_URL = "mysql://nobody:nobody@127.0.0.1:1/nope";

// Imported after the env is set so src/config.ts captures the dead MySQL endpoint.
const { persistSyncState, syncAll, syncOne, syncSectors } = await import("../src/services/sync.service.js");
const { closeStorageBackend, sqliteDatabase } = await import("../src/storage/backend.js");
const db = await sqliteDatabase();

let failures = 0;
async function check(name: string, fn: () => Promise<void> | void): Promise<void> {
  try {
    await fn();
    console.log(`  ok  ${name}`);
  } catch (err: any) {
    failures++;
    console.error(`FAIL  ${name}\n      ${err?.message ?? String(err)}`);
  }
}
const readState = (id: number): any =>
  db.prepare("SELECT * FROM sync_state WHERE instrument_id = ?").get(id);

await check("full + success records the full sync, clears the incremental time, sets quote time", async () => {
  await persistSyncState(1, true, "success", "2026-09-11", [], true);
  const row = readState(1);
  assert.equal(row.full_synced, 1);
  assert.ok(row.last_full_sync_at, "last_full_sync_at must be set");
  assert.equal(row.last_incremental_at, null, "a full sync must not set the incremental time");
  assert.equal(row.last_bar_date, "2026-09-11");
  assert.ok(row.last_quote_at, "quoteSucceeded=true must set last_quote_at");
  assert.equal(row.error_count, 0);
  assert.equal(row.last_error, null);
});

await check("full + failure keeps full_synced false and leaves the quote time unset", async () => {
  await persistSyncState(2, true, "failed", null, ["boom", "last warning"], false);
  const row = readState(2);
  assert.equal(row.full_synced, 0);
  assert.equal(row.last_quote_at, null, "quoteSucceeded=false must not set last_quote_at");
  assert.equal(row.error_count, 2, "error_count follows the warning count");
  assert.equal(row.last_error, "last warning", "last_error is the final warning");
});

await check("full + partial records the bar date but stays not-fully-synced", async () => {
  await persistSyncState(3, true, "partial", "2026-09-10", ["p"], true);
  const row = readState(3);
  assert.equal(row.full_synced, 0);
  assert.equal(row.last_bar_date, "2026-09-10");
  assert.ok(row.last_quote_at);
  assert.equal(row.error_count, 1);
});

await check("incremental sets the incremental time and preserves the full-sync record", async () => {
  await persistSyncState(4, true, "success", "2026-09-11", [], true);
  const before = readState(4);
  await persistSyncState(4, false, "success", "2026-09-12", [], true);
  const after = readState(4);
  assert.equal(after.full_synced, 1, "an incremental sync must not clear full_synced");
  assert.equal(after.last_full_sync_at, before.last_full_sync_at, "the full-sync time is preserved");
  assert.ok(after.last_incremental_at, "last_incremental_at must be set");
  assert.equal(after.last_bar_date, "2026-09-12");
});

await check("a failing incremental keeps the previous quote time", async () => {
  await persistSyncState(5, true, "success", "2026-09-11", [], true);
  const first = readState(5);
  await persistSyncState(5, false, "partial", "2026-09-11", ["q failed"], false);
  const second = readState(5);
  assert.equal(second.last_quote_at, first.last_quote_at, "quoteSucceeded=false must keep the old quote time");
  assert.ok(second.last_incremental_at);
  assert.equal(second.error_count, 1);
});

await check("repeated identical writes are idempotent (no duplicate sync_state rows)", async () => {
  await persistSyncState(6, false, "success", "2026-09-12", [], true);
  await persistSyncState(6, false, "success", "2026-09-12", [], true);
  const rows: any[] = db.prepare("SELECT instrument_id FROM sync_state WHERE instrument_id = 6").all();
  assert.equal(rows.length, 1);
});

await check("syncAll on an empty SQLite database completes without MySQL or network", async () => {
  const result = await syncAll({ full: false });
  assert.equal(result.results.length, 0, "an empty database has nothing to sync");
});

await check("syncSectors on an empty SQLite database completes without MySQL or network", async () => {
  const result = await syncSectors();
  assert.equal(result.sectors.length, 0);
});

await check("syncOne is no longer blocked by the migration guard", async () => {
  // It proceeds to the provider layer now. Any failure must come from the provider/network, never
  // from the removed guard, and never from MySQL.
  try {
    await syncOne("NOSUCHSYMBOL");
  } catch (err: any) {
    const message = String(err?.message ?? err);
    assert.doesNotMatch(message, /not available on the SQLite backend yet/);
    assert.doesNotMatch(message, /ECONNREFUSED 127\.0\.0\.1:1/, "no MySQL I/O may occur on the SQLite path");
  }
});

closeStorageBackend();
rmSync(tmp, { recursive: true, force: true });

if (failures > 0) {
  console.error(`\nsync state tests: ${failures} failure(s)`);
  process.exit(1);
}
console.log("\nsync state tests: all checks passed");
