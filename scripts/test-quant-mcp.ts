/** Actual stdio MCP -> trusted Python runtime -> durable registry round trip. */
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import { openDatabase } from "../src/storage/database.js";
import { applySqliteMigrations } from "../src/storage/migrations.js";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

const root = resolve(import.meta.dirname, "..");
const tmp = mkdtempSync(resolve(tmpdir(), "quant-mcp-"));
const seed = openDatabase(resolve(tmp, "data.db"));
applySqliteMigrations(seed);
seed.db.exec(`INSERT INTO instruments(symbol,exchange,currency) VALUES ('AAPL','XNAS','USD');
INSERT INTO daily_bars(instrument_id,trade_date,open,high,low,close,volume,source) VALUES
(1,'2026-01-02','10','12','9','11',1000,'yahoo'),
(1,'2026-01-05','11','13','10','12',1000,'yahoo');
INSERT INTO intraday_bars(instrument_id,ts,bar_interval,open,high,low,close,volume,source) VALUES
(1,'2026-01-02 14:30:00','15m','10','12','9','11',1000,'yahoo'),
(1,'2026-01-02 14:45:00','15m','99','100','98','99',1000,'investing'),
(1,'2026-01-02 14:30:00','60m','10','12','9','11',1000,'yahoo');`);
seed.close();
const env = Object.fromEntries(Object.entries(process.env).filter((pair): pair is [string, string] => pair[1] !== undefined));
const transportOptions = {command: process.execPath, args: [resolve(root, "dist/cli.js"), "server"], env: { ...env, QUANT_ENABLED: "1", QUANT_PYTHON: process.env.QUANT_PYTHON || resolve(root, "quant/.venv/bin/python"), QUANT_REGISTRY_PATH: resolve(tmp, "quant.db"), YAHOO_STOCK_MCP_SQLITE_PATH: resolve(tmp, "data.db") }, stderr: "pipe" as const};
let transport = new StdioClientTransport(transportOptions);
let client = new Client({name: "quant-acceptance", version: "1.0"});
const deadline = setTimeout(() => { void transport.close(); }, 60_000);
try {
  await client.connect(transport);
  const tools = await client.listTools();
  for (const name of ["quant_get_capabilities", "quant_validate_strategy", "quant_register_strategy", "quant_compile_strategy", "quant_get_calendar", "quant_get_data_quality", "quant_resample_snapshot", "get_bars"]) assert(tools.tools.some(t => t.name === name));
  async function call(name: string, args: Record<string, unknown> = {}) {
    const result = await client.callTool({name, arguments: args});
    assert(!result.isError, JSON.stringify(result));
    const content = result.content as {type: string; text: string}[];
    return JSON.parse(content[0].text);
  }
  const capabilities = await call("quant_get_capabilities");
  assert.equal(capabilities.python_sandbox, Boolean(process.env.QUANT_SANDBOX_IMAGE));
  assert.equal(capabilities.mcp_execution_enabled, true);
  const strategy = {schema_version: "1.0", name: "hold", decision_timeframe: "1d", entry: {kind: "const", value: true}, exit: {kind: "const", value: false}};
  const invalid = await call("quant_validate_strategy", {strategy: {...strategy, entry: {kind: "const", value: 1}}});
  assert.equal(invalid.valid, false);
  assert.equal(invalid.errors[0].code, "TYPE_MISMATCH");
  const valid = await call("quant_validate_strategy", {strategy});
  assert.equal(valid.valid, true);
  const registered = await call("quant_register_strategy", {strategy});
  const plan = await call("quant_compile_strategy", {strategy_id: registered.strategy_id});
  assert.equal(plan.plan_id, valid.plan_hash);
  assert.equal((await call("quant_compile_strategy", {strategy_id: registered.strategy_id})).plan_id, plan.plan_id);
  const bars = Array.from({length: 4}, (_, i) => {
    const day = `2026-01-0${i + 2}`;
    return {symbol: "AAPL", timeframe: "1d", exchange: "XNAS", start_time: `${day}T14:30:00Z`, end_time: `${day}T21:00:00Z`, available_at: `${day}T21:00:00Z`, open: 10 + i, high: 12 + i, low: 9 + i, close: 11 + i, volume: 1000, source: "fixture", adjustment: "raw", dataset_version: "1"};
  });
  const snapshot = await call("quant_import_snapshot", {bars, calendar_version: "synthetic-v1"});
  const evaluated = await call("quant_evaluate_signal", {strategy_id: registered.strategy_id, data_snapshot_id: snapshot.data_snapshot_id, as_of: "2026-01-02T22:00:00Z"});
  assert.deepEqual(evaluated.signals, {entry: true, exit: false});
  const local = await call("quant_snapshot_from_local", {symbol: "AAPL", timeframe: "1d", calendar_version: "fixture-v1", publication_delay_seconds: 60, sessions: ["2026-01-02", "2026-01-05"].map(date => ({date, open: `${date}T14:30:00Z`, close: `${date}T21:00:00Z`}))});
  assert.equal(local.bar_count, 2);
  for (const timeframe of ["15m", "1h"]) {
    const copied = await call("quant_snapshot_from_local", {symbol: "AAPL", timeframe, calendar_version: "fixture-v1", publication_delay_seconds: 60, sessions: [{date: "2026-01-02", open: "2026-01-02T09:30:00-05:00", close: "2026-01-02T16:00:00-05:00"}]});
    assert.equal(copied.bar_count, 1);
  }
  const calendar = await call("quant_get_calendar", {calendar_name: "XNYS", exchange: "XNAS", start_date: "2026-01-02", end_date: "2026-01-05"});
  assert.equal(calendar.provider, "exchange_calendars");
  assert.equal(calendar.sessions.length, 2);
  const calendarLocal = await call("quant_snapshot_from_local", {symbol: "AAPL", timeframe: "1d", calendar, publication_delay_seconds: 60});
  assert.equal(calendarLocal.calendar_ids.length, 1);
  const localQualityArgs = {symbol: "AAPL", timeframe: "1d", as_of: "2026-01-05T21:01:00Z"};
  const unverifiedQuality = await call("quant_get_data_quality", {...localQualityArgs, data_snapshot_id: calendarLocal.data_snapshot_id});
  assert.deepEqual(unverifiedQuality.adjustment_policies, ["unknown"]);
  const unverifiedJob = await client.callTool({name: "quant_run_backtest", arguments: {plan_id: plan.plan_id, data_snapshot_id: calendarLocal.data_snapshot_id, symbol: "AAPL", timeframe: "1d"}});
  assert.equal(unverifiedJob.isError, true);
  assert.match(JSON.stringify(unverifiedJob.content), /requires raw/);
  const attestedLocal = await call("quant_snapshot_from_local", {symbol: "AAPL", timeframe: "1d", calendar, publication_delay_seconds: 60, price_basis: "raw_attested",
    actions: [{id: "local-dividend", kind: "cash_dividend", symbol: "AAPL", amount: 0.1, currency: "USD", available_at: "2026-01-02T14:30:00Z", effective_at: "2026-01-05T14:30:00Z", pay_at: "2026-01-05T21:00:00Z"}]});
  assert.notEqual(attestedLocal.data_snapshot_id, calendarLocal.data_snapshot_id);
  assert.equal(attestedLocal.action_count, 1);
  assert.deepEqual((await call("quant_get_data_quality", {...localQualityArgs, data_snapshot_id: attestedLocal.data_snapshot_id})).adjustment_policies, ["raw"]);
  const attestedJob = await call("quant_run_backtest", {plan_id: plan.plan_id, data_snapshot_id: attestedLocal.data_snapshot_id, symbol: "AAPL", timeframe: "1d"});
  const qualityBars = Array.from({length: 8}, (_, i) => {
    const start = Date.parse("2026-01-02T14:30:00Z") + i * 900000;
    return {symbol: "AAPL", timeframe: "15m", exchange: "XNAS", start_time: new Date(start).toISOString(), end_time: new Date(start + 900000).toISOString(),
      available_at: new Date(start + 900000 + (i === 3 ? 600000 : 0)).toISOString(), open: 10+i, high: 12+i, low: 9+i, close: 11+i, volume: 100,
      source: "quality-fixture", adjustment: "raw", dataset_version: "v1"};
  });
  const qualitySnapshot = await call("quant_import_snapshot", {bars: qualityBars, calendar_version: calendar.version, calendars: [calendar]});
  const quality = await call("quant_get_data_quality", {data_snapshot_id: qualitySnapshot.data_snapshot_id, symbol: "AAPL", timeframe: "15m", as_of: qualityBars[3].end_time});
  assert.equal(quality.observed_count, 3);
  assert.equal(quality.unavailable_completed_count, 1);
  const unfinished = await call("quant_resample_snapshot", {data_snapshot_id: qualitySnapshot.data_snapshot_id, symbol: "AAPL", source_timeframe: "15m", target_timeframe: "1h", as_of: qualityBars[3].end_time});
  assert.equal(unfinished.valid, false);
  assert.equal(unfinished.data_snapshot_id, null);
  const resampled = await call("quant_resample_snapshot", {data_snapshot_id: qualitySnapshot.data_snapshot_id, symbol: "AAPL", source_timeframe: "15m", target_timeframe: "1h"});
  assert.equal(resampled.diagnostics.emitted_count, 2);
  assert.equal(resampled.lineage.parent_snapshot_id, qualitySnapshot.data_snapshot_id);
  const resampledQuality = await call("quant_get_data_quality", {data_snapshot_id: resampled.data_snapshot_id, symbol: "AAPL", timeframe: "1h", as_of: qualityBars[7].available_at});
  assert.equal(resampledQuality.observed_count, 2);
  const noIntraday = await client.callTool({name: "quant_snapshot_from_local", arguments: {symbol: "AAPL", timeframe: "1m", calendar_version: "fixture-v1", publication_delay_seconds: 0, sessions: [{date: "2026-01-02", open: "2026-01-02T14:30:00Z", close: "2026-01-02T21:00:00Z"}]}});
  assert.equal(noIntraday.isError, true);
  const job = await call("quant_run_backtest", {plan_id: plan.plan_id, data_snapshot_id: snapshot.data_snapshot_id, symbol: "AAPL", timeframe: "1d", config: {quantity: 2, initial_cash: 1000}});
  assert.equal(job.status, "queued");
  await client.close();
  transport = new StdioClientTransport(transportOptions);
  client = new Client({name: "quant-reconnected", version: "1.0"});
  await client.connect(transport);
  assert.equal((await call("quant_compile_strategy", {strategy_id: registered.strategy_id})).plan_id, plan.plan_id);
  let status = await call("quant_get_job", {job_id: job.job_id});
  for (let i = 0; i < 20 && ["queued", "running"].includes(status.status); i++) {
    await new Promise(resolve => setTimeout(resolve, 50));
    status = await call("quant_get_job", {job_id: job.job_id});
  }
  assert.equal(status.status, "completed", JSON.stringify(status));
  let attestedStatus = await call("quant_get_job", {job_id: attestedJob.job_id});
  for (let i = 0; i < 20 && ["queued", "running"].includes(attestedStatus.status); i++) {
    await new Promise(resolve => setTimeout(resolve, 50));
    attestedStatus = await call("quant_get_job", {job_id: attestedJob.job_id});
  }
  assert.equal(attestedStatus.status, "completed", JSON.stringify(attestedStatus));
  assert.equal((await call("quant_get_result", {job_id: attestedJob.job_id, section: "fills"})).rows.length, 1);
  assert.equal((await call("quant_get_result", {job_id: job.job_id, section: "fills"})).rows.length, 1);
  const analytics = await call("quant_get_result", {job_id: job.job_id, section: "analytics"});
  assert.equal(analytics.version, "1.0");
  assert(analytics.elapsed_seconds > 0);
  const attribution = await call("quant_get_result", {job_id: job.job_id, section: "attribution"});
  assert(Math.abs(attribution.accounting_residual) < 1e-9);
  assert((await call("quant_get_result", {job_id: job.job_id, section: "daily_returns"})).total_count > 0);
  const artifacts = await call("quant_get_result", {job_id: job.job_id, section: "artifacts"});
  const fillsArtifact = artifacts.rows.find((row: {section: string}) => row.section === "fills");
  assert.equal(fillsArtifact.format, "parquet-json-v1");
  assert.equal(fillsArtifact.row_count, 1);
  assert.match(fillsArtifact.content_hash, /^sha256:[0-9a-f]{64}$/);
  const page = await call("quant_get_result", {job_id: job.job_id, section: "equity_curve", limit: 2});
  assert.equal(page.rows.length, 2);
  assert(page.next_cursor);
  assert.equal((await call("quant_get_result", {job_id: job.job_id, section: "equity_curve", limit: 2, cursor: page.next_cursor})).rows.length, 2);
  const resource = await client.readResource({uri: `quant://results/${job.job_id}/summary`});
  assert.equal(JSON.parse((resource.contents[0] as {text: string}).text).fill_count, 1);
  assert.equal((await call("quant_cancel_job", {job_id: job.job_id})).status, "completed");
  const portfolioBars = [...bars, ...bars.map(b => ({...b, symbol: "MSFT"}))];
  const action = {kind: "cash_dividend", id: "div-fixture", symbol: "AAPL", amount: 1, currency: "USD", available_at: bars[0].start_time, effective_at: bars[2].start_time, pay_at: bars[3].end_time};
  const portfolioSnapshot = await call("quant_import_snapshot", {bars: portfolioBars, calendar_version: "synthetic-v1", actions: [action]});
  assert.equal(portfolioSnapshot.action_count, 1);
  const portfolioJob = await call("quant_run_backtest", {portfolio: {AAPL: plan.plan_id, MSFT: plan.plan_id}, data_snapshot_id: portfolioSnapshot.data_snapshot_id, config: {quantity: 2, initial_cash: 1000, currency: "USD", max_positions: 2}});
  let portfolioStatus = await call("quant_get_job", {job_id: portfolioJob.job_id});
  for (let i = 0; i < 20 && ["queued", "running"].includes(portfolioStatus.status); i++) {
    await new Promise(resolve => setTimeout(resolve, 50));
    portfolioStatus = await call("quant_get_job", {job_id: portfolioJob.job_id});
  }
  assert.equal(portfolioStatus.status, "completed", JSON.stringify(portfolioStatus));
  const finalAccount = await call("quant_get_result", {job_id: portfolioJob.job_id, section: "final_account"});
  assert.equal(finalAccount.positions.AAPL.quantity, 2);
  assert.equal(finalAccount.positions.MSFT.quantity, 2);
  assert.equal(finalAccount.equity, 1014);
  const cashLedger = await call("quant_get_result", {job_id: portfolioJob.job_id, section: "cash_ledger"});
  assert.equal(cashLedger.rows.filter((r: {kind: string}) => r.kind === "dividend_payment").length, 1);
  const template = {schema_version: "1.0", name: "research-template", decision_timeframe: "1d",
    parameters: {threshold: {value_type: "number", minimum: 0, maximum: 100, default: 0}, period: {value_type: "integer", minimum: 1, maximum: 5, default: 1}},
    signals: {score: {kind: "ref", symbol: "$symbol", field: "close", timeframe: "1d"}},
    entry: {kind: "binary", op: "gt", left: {kind: "call", operator: "sma", args: [{kind: "ref", symbol: "$symbol", field: "close", timeframe: "1d"}], params: {period: {kind: "param", name: "period"}}}, right: {kind: "param", name: "threshold"}}, exit: {kind: "const", value: false}};
  const researchStrategy = await call("quant_register_strategy", {strategy: template});
  const bound = await call("quant_compile_strategy", {strategy_id: researchStrategy.strategy_id, parameters: {period: 2}, symbol: "AAPL"});
  assert.equal(bound.symbol_binding, "AAPL");
  assert.equal(bound.requirements[0].warmup_bars, 2);
  const researchBars = ["AAPL", "MSFT"].flatMap(symbol => Array.from({length: 12}, (_, i) => {
    const date = `2026-01-${String(i + 2).padStart(2, "0")}`;
    const price = 10 + i + (symbol === "MSFT" ? 20 : 0);
    return {...bars[0], symbol, start_time: `${date}T14:30:00Z`, end_time: `${date}T21:00:00Z`, available_at: `${date}T21:00:00Z`, open: price, high: price, low: price, close: price};
  }));
  const researchSnapshot = await call("quant_import_snapshot", {bars: researchBars, calendar_version: "research-fixture"});
  async function completed(jobId: string) {
    for (let i = 0; i < 30; i++) {
      const status = await call("quant_get_job", {job_id: jobId});
      if (status.status === "completed") return;
      assert(["queued", "running"].includes(status.status), JSON.stringify(status));
      await new Promise(resolve => setTimeout(resolve, 50));
    }
    assert.fail("research job did not complete within poll budget");
  }
  const optimized = await call("quant_optimize", {strategy_id: researchStrategy.strategy_id, data_snapshot_id: researchSnapshot.data_snapshot_id, symbol: "AAPL", search_space: {threshold: [0, 100]}, train_top_k: 2, windows: [{train_start: "2026-01-02T00:00:00Z", train_end: "2026-01-05T00:00:00Z", validation_start: "2026-01-05T00:00:00Z", validation_end: "2026-01-08T00:00:00Z", test_start: "2026-01-08T00:00:00Z", test_end: "2026-01-11T00:00:00Z"}]});
  await completed(optimized.job_id);
  const selectedFold = (await call("quant_get_result", {job_id: optimized.job_id, section: "walk_forward"})).rows[0];
  assert.equal(selectedFold.selected_parameters.threshold, 0);
  const replay = await call("quant_run_backtest", {plan_id: selectedFold.selected_plan_id, symbol: "AAPL", timeframe: "1d", data_snapshot_id: researchSnapshot.data_snapshot_id, start: selectedFold.boundaries.test_start, end: selectedFold.boundaries.test_end});
  await completed(replay.job_id);
  assert.equal((await call("quant_get_result", {job_id: replay.job_id, section: "summary"})).total_return, selectedFold.test.total_return);
  const trials = await call("quant_get_result", {job_id: optimized.job_id, section: "trials", limit: 1});
  assert.equal(trials.rows.length, 1);
  assert(trials.next_cursor);
  const screened = await call("quant_run_screen", {strategy_id: researchStrategy.strategy_id, data_snapshot_id: researchSnapshot.data_snapshot_id, symbols: ["AAPL", "MSFT"], as_of: "2026-01-12T22:00:00Z", score_signal: "score"});
  await completed(screened.job_id);
  const crossSection = await call("quant_get_result", {job_id: screened.job_id, section: "screen"});
  assert.equal(crossSection.rows[0].symbol, "MSFT");
  assert.equal(crossSection.rows[0].rank, 1);
  assert.equal(capabilities.signal_backends.vector.implementation, "numpy");
  const vectorJob = await call("quant_run_backtest", {plan_id: bound.plan_id, symbol: "AAPL", timeframe: "1d", data_snapshot_id: researchSnapshot.data_snapshot_id, config: {signal_backend: "vector"}});
  const pitJob = await call("quant_run_backtest", {plan_id: bound.plan_id, symbol: "AAPL", timeframe: "1d", data_snapshot_id: researchSnapshot.data_snapshot_id});
  await completed(vectorJob.job_id);
  await completed(pitJob.job_id);
  for (const section of ["summary", "orders", "fills", "final_account"]) {
    const vectorResult = await call("quant_get_result", {job_id: vectorJob.job_id, section});
    const pitResult = await call("quant_get_result", {job_id: pitJob.job_id, section});
    assert.deepEqual(vectorResult.rows ?? vectorResult, pitResult.rows ?? pitResult);
  }
  assert.equal((await call("quant_get_result", {job_id: vectorJob.job_id, section: "reproducibility"})).signal_backend, "vector");
  const gridStrategy = {schema_version: "1.0", name: "grid-stdio", decision_timeframe: "15m", signals: {
    score: {kind: "call", operator: "sma", args: [{kind: "align", timeframe: "15m", policy: "last_completed", value: {kind: "ref", symbol: "AAPL", field: "close", timeframe: "1d"}}], params: {period: 2}}},
    entry: {kind: "binary", op: "gt", left: {kind: "signal", name: "score"}, right: {kind: "const", value: 10}}, exit: {kind: "const", value: false}};
  const gridRegistered = await call("quant_register_strategy", {strategy: gridStrategy});
  const gridPlan = await call("quant_compile_strategy", {strategy_id: gridRegistered.strategy_id});
  assert.deepEqual(gridPlan.grid_requirements, [{timeframe: "15m", warmup_bars: 2}]);
  const gridBars = Array.from({length: 5}, (_, i) => ({...bars[1], timeframe: "15m", start_time: `2026-01-03T${i < 2 ? "14" : "15"}:${String((30 + 15 * i) % 60).padStart(2, "0")}:00Z`, end_time: `2026-01-03T${i < 1 ? "14" : "15"}:${String((45 + 15 * i) % 60).padStart(2, "0")}:00Z`, available_at: `2026-01-03T${i < 1 ? "14" : "15"}:${String((45 + 15 * i) % 60).padStart(2, "0")}:00Z`}));
  const gridSnapshot = await call("quant_import_snapshot", {bars: [...bars, ...gridBars], calendar_version: "grid-stdio"});
  const earlyGrid = await call("quant_evaluate_signal", {strategy_id: gridRegistered.strategy_id, data_snapshot_id: gridSnapshot.data_snapshot_id, as_of: gridBars[0].available_at, symbol: "AAPL"});
  assert.equal(earlyGrid.errors[0].code, "INSUFFICIENT_HISTORY");
  assert.equal(earlyGrid.grid_dependencies[0].available_bars, 1);
  const gridJob = await call("quant_run_backtest", {plan_id: gridPlan.plan_id, symbol: "AAPL", timeframe: "15m", data_snapshot_id: gridSnapshot.data_snapshot_id});
  await completed(gridJob.job_id);
  assert.equal((await call("quant_get_result", {job_id: gridJob.job_id, section: "fills"})).rows[0].time, "2026-01-03T15:15:00+00:00");
  if (capabilities.python_sandbox) {
    const pythonStrategy = {mode: "python", name: "python-stdio-hold", decision_timeframe: "1d", data_timeframes: ["1d"], source: `class Strategy:
    def initialize(self, ctx):
        assert ctx.random_seed == 42
        self.count = 0
    def on_bar(self, ctx, bar):
        history = ctx.history(bar.symbol, "1d", 256)
        assert history[-1].available_at == bar.available_at
        if ctx.position(bar.symbol) == 0:
            return [{"symbol": bar.symbol, "side": "buy", "quantity": 2}]
        return []
    def on_fill(self, ctx, fill):
        self.count += 1
`};
    assert.equal((await call("quant_validate_strategy", {strategy: pythonStrategy})).valid, true);
    const pythonRegistered = await call("quant_register_strategy", {strategy: pythonStrategy});
    const pythonPlan = await call("quant_compile_strategy", {strategy_id: pythonRegistered.strategy_id});
    assert.equal(pythonPlan.mode, "python");
    const pythonJob = await call("quant_run_backtest", {portfolio: {AAPL: pythonPlan.plan_id, MSFT: pythonPlan.plan_id}, data_snapshot_id: portfolioSnapshot.data_snapshot_id, config: {quantity: 2, initial_cash: 1000}, random_seed: 42});
    await completed(pythonJob.job_id);
    const pythonAccount = await call("quant_get_result", {job_id: pythonJob.job_id, section: "final_account"});
    assert.equal(pythonAccount.equity, 1014);
    const pythonRepro = await call("quant_get_result", {job_id: pythonJob.job_id, section: "reproducibility"});
    assert.equal(pythonRepro.strategy_mode, "python");
    assert.equal(pythonRepro.sandbox_image, process.env.QUANT_SANDBOX_IMAGE);
    assert.equal(pythonRepro.random_seed, 42);
    assert.match(pythonRepro.randomness_policy, /stdlib_random_seed_v1/);
    console.log("Quant isolated Python stdio acceptance passed: syntax-only registration, pinned image/runtime plan, shared-cash lifecycle portfolio, PIT history, dividend accounting and reproducibility");
  }
  const missing = await client.callTool({name: "quant_compile_strategy", arguments: {strategy_id: "missing"}});
  assert.equal(missing.isError, true);
  console.log("Quant stdio acceptance passed: discovery, validation, registration, durable compilation, snapshot import, asynchronous backtest across MCP restart, local daily/hourly/15m adapter, unverified price rejection and raw attestation, generated versioned exchange calendar, PIT coverage diagnostics, complete-constituent resampling/lineage, resource reads, shared-cash portfolio/dividend ledger, parameter binding, optimization/walk-forward, cross-sectional screening, PIT/vector shared-broker parity, rolling decision-grid alignment/warmup, result pagination, terminal cancellation, error propagation");
} finally {
  clearTimeout(deadline);
  await client.close();
  rmSync(tmp, {recursive: true, force: true});
}
