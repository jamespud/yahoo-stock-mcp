import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { ResourceTemplate } from "@modelcontextprotocol/sdk/server/mcp.js";
import { localSnapshot, type ExplicitCalendar } from "./local-data.js";
import { isValidIsoDate } from "../validation.js";
import { checkedQuantResponse, quantFrameBytes, quantRequest } from "./bridge.js";
import { registerAlphaTools } from "./alpha-tools.js";

export function registerQuantTools(server: McpServer): void {
  async function invoke(request: Record<string, unknown>, requestId?: string | number) {
    try {
      const result = await quantRequest(requestId === undefined ? request : {...request,
        mcp_overhead_bytes: quantFrameBytes({content: [{type: "text", text: ""}]}, requestId)});
      const response = { content: [{ type: "text" as const, text: JSON.stringify(result) }] };
      return requestId === undefined ? response : checkedQuantResponse(response, requestId);
    } catch (error) {
      return { isError: true, content: [{ type: "text" as const, text: error instanceof Error ? error.message : String(error) }] };
    }
  }
  const strategy = z.record(z.unknown()).describe("Strict DSL definition or Python artifact (mode=python); obtain schemas and isolation status via quant_get_capabilities");
  const executionConfig = z.object({signal_backend: z.enum(["pit", "vector"]).optional().describe("PIT supports explicit multi-timeframe decision grids; vector uses NumPy for one monotone symbol/timeframe with the shared event broker"), initial_cash: z.number().positive().optional(), quantity: z.number().positive().optional(), commission_bps: z.number().min(0).max(10000).optional(), slippage_bps: z.number().min(0).lt(10000).optional(), fill_policy: z.literal("next_bar_open").optional(), currency: z.string().regex(/^[A-Z]{3}$/).optional(), max_positions: z.number().int().min(1).max(500).optional(), max_position_weight: z.number().positive().max(1).optional(), max_gross_exposure: z.number().positive().max(1).optional(), max_order_notional: z.number().positive().optional(), volume_participation: z.number().positive().max(1).optional()}).strict();
  const parameterValues = z.record(z.number()).optional();
  server.tool("quant_get_capabilities", "Discover deployed quant and Alpha capabilities, dependency availability, stable errors and provider data coverage. Does not imply unavailable provider coverage.", {}, async () => invoke({ method: "capabilities" }));
  server.tool("quant_validate_strategy", "Validate DSL types/time semantics or Python artifact syntax without executing source. Python lifecycle is checked only in an isolated backtest.", { strategy, parameters: parameterValues, symbol: z.string().min(1).max(64).optional() }, async (args) => invoke({ method: "validate", ...args }));
  function registryPath() {
    const path = process.env.QUANT_REGISTRY_PATH;
    if (!path) throw new Error("QUANT_REGISTRY_PATH must name a writable SQLite file");
    return path;
  }
  server.tool("quant_register_strategy", "Register an immutable validated DSL or syntax-checked Python artifact in the quant registry.", { strategy }, async ({ strategy: definition }) => {
    try { return await invoke({ method: "register", strategy: definition, registry_path: registryPath() }); }
    catch (error) { return { isError: true, content: [{ type: "text" as const, text: String(error) }] }; }
  });
  server.tool("quant_compile_strategy", "Compile immutable DSL with parameter bindings, or pin a Python lifecycle plan to the operator-approved image and isolation protocol. Python artifacts do not accept DSL parameters.", { strategy_id: z.string().min(1).max(128), parameters: parameterValues, symbol: z.string().min(1).max(64).optional() }, async (args) => stored({method: "compile", ...args}));

  async function stored(request: Record<string, unknown>, requestId?: string | number) {
    try { return await invoke({ ...request, registry_path: registryPath() }, requestId); }
    catch (error) { return { isError: true, content: [{ type: "text" as const, text: String(error) }] }; }
  }
  const jobId = z.string().min(1).max(128);
  registerAlphaTools(server, { invoke, stored, registryPath });

  server.tool("quant_import_snapshot", "Import immutable explicit market observations with timezone-aware start_time/end_time/available_at. Does not infer provider timestamps or fill missing data.", {
    bars: z.array(z.record(z.unknown())).min(1).max(10000), calendar_version: z.string().min(1).max(128), actions: z.array(z.record(z.unknown())).max(5000).optional(), calendars: z.array(z.record(z.unknown())).max(50).optional().describe("Explicit exchange schedules; obtain calendar_schema via capabilities. Sessions and timezone participate in snapshot identity."),
  }, async (args) => stored({ method: "import_snapshot", ...args }));
  const quantTimeframe = z.enum(["1m", "5m", "15m", "1h", "1d", "1w"]);
  server.tool("quant_get_calendar", "Generate a versioned exchange schedule from the installed calendar library, including holidays, early closes, DST and trading breaks. Returns actual intervals and provenance to save in a snapshot; does not claim historical announcement times or ad hoc future closures.", {
    calendar_name: z.string().min(1).max(64), exchange: z.string().min(1).max(64),
    start_date: z.string().refine(isValidIsoDate), end_date: z.string().refine(isValidIsoDate),
  }, async (args) => invoke({method: "calendar", ...args}));
  const datasetId = z.string().regex(/^sha256:[0-9a-f]{64}$/);
  const datasetDetailSection = z.enum(["session_coverage_by_symbol", "exchange_mappings", "partition_manifest", "research_readiness"]);
  server.tool("quant_get_data_quality", "Inspect a legacy snapshot or an immutable Alpha dataset. Use overview for summary and integrity validation; use a detail section and next_cursor to audit all rows, or research_readiness for the evidence-aware report. Omit section for the legacy inline response when small.", {
    data_snapshot_id: jobId.optional(), dataset_id: datasetId.optional(),
    symbol: z.string().min(1).max(64).optional(), timeframe: quantTimeframe.optional(), as_of: z.string().datetime({offset: true}).optional(),
    section: z.union([z.literal("overview"), datasetDetailSection]).optional(),
    limit: z.number().int().min(1).max(1000).optional(), cursor: z.string().min(1).max(2048).nullish(),
    max_staleness_seconds: z.number().int().nonnegative().optional().describe("Optional wall-clock tolerance, including overnight/weekend elapsed time"),
  }, async (args, extra) => {
    if (args.dataset_id && (args.data_snapshot_id || args.timeframe || args.as_of || args.max_staleness_seconds !== undefined)) {
      return {isError: true, content: [{type: "text" as const, text: "dataset_id cannot be combined with snapshot quality fields"}]};
    }
    if (args.dataset_id) return stored({method: "alpha_data_quality", ...args}, extra.requestId);
    if (args.section || args.limit !== undefined || args.cursor != null) {
      return {isError: true, content: [{type: "text" as const, text: "Dataset audit fields require dataset_id"}]};
    }
    if (!args.data_snapshot_id || !args.symbol || !args.timeframe || !args.as_of) {
      return {isError: true, content: [{type: "text" as const, text: "data_snapshot_id, symbol, timeframe and as_of are required for snapshot quality"}]};
    }
    return stored({method: "data_quality", ...args});
  });
  server.tool("quant_resample_snapshot", "Create an immutable snapshot retaining source bars plus explicit larger-timeframe raw OHLCV aggregates. Requires a versioned exchange schedule and every exact constituent; incomplete/internal-split buckets are omitted with diagnostics. Does not replace existing target data.", {
    data_snapshot_id: jobId, symbol: z.string().min(1).max(64), source_timeframe: quantTimeframe, target_timeframe: quantTimeframe,
    as_of: z.string().datetime({offset: true}).optional(),
  }, async (args) => stored({method: "resample", ...args}));
  server.tool("quant_run_backtest", "Submit a durable long-only backtest against immutable plan/snapshot. A Python portfolio uses one artifact and isolation policy across symbol bindings and one isolated lifecycle actor; DSL uses bound per-symbol plans.", {
    plan_id: z.string().min(1).max(128).optional(), portfolio: z.record(z.string().min(1).max(128)).optional().describe("Map execution symbol to immutable plan_id; use this OR plan_id/symbol/timeframe"), data_snapshot_id: z.string().min(1).max(128),
    symbol: z.string().min(1).max(64).optional(), timeframe: z.enum(["1m", "5m", "15m", "1h", "1d", "1w"]).optional(),
    config: executionConfig.optional(),
    start: z.string().datetime({ offset: true }).optional(), end: z.string().datetime({ offset: true }).optional(), random_seed: z.number().int().optional().describe("Python seeds standard-library random before module/class loading and exposes ctx.random_seed for custom generators. DSL execution is deterministic."), idempotency_key: z.string().min(1).max(128).optional(),
  }, async (backtest) => stored({method: "run_backtest", backtest}));
  server.tool("quant_get_job", "Read durable job status and progress; restart consumers for queued/expired work with lease fencing.", {job_id: jobId}, async (args) => stored({method: "get_job", ...args}));
  server.tool("quant_cancel_job", "Cancel queued/running work. Terminal jobs remain terminal; cancelled workers cannot publish a result.", {job_id: jobId}, async (args) => stored({method: "cancel_job", ...args}));
  server.tool("quant_get_result", "Read a completed result section. List sections use bounded pagination with cursors tied to the job, section and result hash.", {
    job_id: jobId, section: z.enum(["artifacts", "analytics", "attribution", "daily_returns", "summary", "orders", "fills", "equity_curve", "reproducibility", "final_account", "diagnostics", "cash_ledger", "corporate_actions", "exposures", "risk_events", "trials", "walk_forward", "parameter_sensitivity", "screen", "dataset", "data_quality", "factors", "factor_diagnostics", "option_analytics", "training_metrics", "baseline_comparison", "model_registry", "predictions", "rank_ic", "portfolio_metrics", "target_weights", "trades"]).default("summary"), limit: z.number().int().min(1).max(1000).default(100), cursor: z.string().max(2048).optional(),
  }, async (args, extra) => stored({method: "get_result", ...args}, extra.requestId));

  server.tool("quant_snapshot_from_local", "Snapshot local Yahoo vendor OHLC using a versioned calendar or session schedule. Price basis defaults to unverified and is refused by the broker unless the caller explicitly attests raw historical prices. Availability is modeled as bar end plus declared delay; historical publication/revision timestamps are not known. Missing timeframes fail.", {
    symbol: z.string().min(1).max(64), timeframe: z.enum(["1m", "5m", "15m", "1h", "1d"]),
    calendar_version: z.string().min(1).max(128).optional(), calendar: z.record(z.unknown()).optional().describe("Schedule from quant_get_calendar; use this OR sessions. Calendar exchange must match the instrument."), publication_delay_seconds: z.number().int().min(0).max(86400),
    price_basis: z.enum(["vendor_ohlc_unverified", "raw_attested"]).default("vendor_ohlc_unverified").describe("raw_attested asserts verified executable historical prices, not merely the absence of a separate adj_close field. Supply applicable action terms explicitly."),
    actions: z.array(z.record(z.unknown())).max(5000).optional(),
    sessions: z.array(z.object({date: z.string().refine(isValidIsoDate), open: z.string().datetime({offset: true}), close: z.string().datetime({offset: true})}).strict()).min(1).max(5000).optional(),
  }, async (args) => {
    try {
      const calendar = args.calendar ? await quantRequest({method: "validate_calendar", calendar: args.calendar}) as ExplicitCalendar : undefined;
      return await stored({method: "import_snapshot", ...await localSnapshot({...args, calendar})});
    }
    catch (error) { return {isError: true, content: [{type: "text" as const, text: String(error)}]}; }
  });
  server.resource("quant-result", new ResourceTemplate("quant://results/{job_id}/{section}", {list: undefined}), {mimeType: "application/json", description: "Completed result section, first 100 rows with pagination cursor for lists."}, async (uri, variables, extra) => {
    const mcp_overhead_bytes = quantFrameBytes({contents: [{uri: uri.href, mimeType: "application/json", text: ""}]}, extra.requestId);
    const result = await quantRequest({method: "get_result", registry_path: registryPath(), job_id: variables.job_id, section: variables.section, limit: 100, mcp_overhead_bytes});
    return checkedQuantResponse({contents: [{uri: uri.href, mimeType: "application/json", text: JSON.stringify(result)}]}, extra.requestId);
  });
  server.resource("quant-dataset-audit", new ResourceTemplate("quant://datasets/{dataset_id}/{section}", {list: undefined}), {
    mimeType: "application/json", description: "First unfiltered page of an immutable dataset detail section. Use quant_get_data_quality for subsequent pages or symbol filters, or request research_readiness for the evidence-aware report.",
  }, async (uri, variables, extra) => {
    const mcp_overhead_bytes = quantFrameBytes({contents: [{uri: uri.href, mimeType: "application/json", text: ""}]}, extra.requestId);
    const result = await quantRequest({method: "alpha_data_quality", registry_path: registryPath(),
      dataset_id: datasetId.parse(variables.dataset_id), section: datasetDetailSection.parse(variables.section), mcp_overhead_bytes});
    return checkedQuantResponse({contents: [{uri: uri.href, mimeType: "application/json", text: JSON.stringify(result)}]}, extra.requestId);
  });

  server.tool("quant_evaluate_signal", "Evaluate a registered DSL as of a timezone-aware instant. Optional symbol binds $symbol and the alignment decision grid. Reports source and target-grid warmup separately.", {
    strategy_id: z.string().min(1).max(128), data_snapshot_id: z.string().min(1).max(128), as_of: z.string().datetime({offset: true}), symbol: z.string().min(1).max(64).optional(), parameters: parameterValues,
  }, async (args) => stored({method: "evaluate_signal", ...args}));

  const instant = z.string().datetime({offset: true});
  const window = z.object({train_start: instant, train_end: instant, validation_start: instant, validation_end: instant, test_start: instant, test_end: instant}).strict();
  server.tool("quant_optimize", "Submit bounded grid search/walk-forward. Train chooses finalists, validation selects one, then only the frozen winner reads test performance. Every interval, candidate, cost scenario and budget is recorded.", {
    strategy_id: z.string().min(1).max(128), data_snapshot_id: z.string().min(1).max(128), symbol: z.string().min(1).max(64),
    search_space: z.record(z.array(z.number()).min(1).max(100)), windows: z.array(window).min(1).max(10),
    objective: z.enum(["total_return", "max_drawdown"]).optional(), train_top_k: z.number().int().min(1).max(10).optional(), max_trials: z.number().int().min(1).max(100).optional(), max_evaluations: z.number().int().min(1).max(1000).optional(),
    cost_multipliers: z.array(z.number().nonnegative()).min(1).max(5).optional(), config: executionConfig.optional(), random_seed: z.number().int().optional(), idempotency_key: z.string().min(1).max(128).optional(),
  }, async (research) => stored({method: "optimize", research}));
  server.tool("quant_run_screen", "Submit PIT screening over a caller-supplied as-of universe using a $symbol strategy template. Returns per-symbol errors and cross-sectional rank/percentile/zscore; does not supply historical index constituents.", {
    strategy_id: z.string().min(1).max(128), data_snapshot_id: z.string().min(1).max(128), symbols: z.array(z.string().min(1).max(64)).min(1).max(500), as_of: instant,
    parameters: parameterValues, score_signal: z.string().min(1).max(64).optional(), random_seed: z.number().int().optional(), idempotency_key: z.string().min(1).max(128).optional(),
  }, async (research) => stored({method: "screen", research}));

}
