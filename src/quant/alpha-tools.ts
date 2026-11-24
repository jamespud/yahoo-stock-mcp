import { createHash } from "node:crypto";
import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import type { z } from "zod";
import {
  alphaResearchLedgerInput,
  alphaResearchReportInput,
  registerAlphaResearchProtocolFields,
  registerAlphaResearchProtocolInput,
  registerAlphaTrialInput,
  sealAlphaCandidateLedgerInput,
  freezeAlphaSelectionInput,
  evaluateAlphaOOSInput,
  alphaModelDiagnosticsInput,
  backtestAlphaPortfolioInput,
  backtestOptionsInput,
  captureOptionsSnapshotInput,
  computeFactorsInput,
  computeOptionFactorsInput,
  predictAlphaInput,
  prepareAlphaDatasetInput,
  prepareOptionsDatasetInput,
  trainAlphaModelInput,
  trainAlphaModelToolFields,
  walkForwardAlphaInput,
  walkForwardAlphaToolFields,
} from "./alpha-contracts.js";
import { developmentWindows } from "./alpha-contracts.js";
import { canInlineAlphaRows, stageAlphaRows } from "./alpha-artifacts.js";
import { captureYahooOptionRows } from "./option-capture.js";

interface AlphaToolsRuntime {
  // The MCP SDK expects CallToolResult; the shared runtime closures already
  // return that exact serializable shape and reject bridge failures.
  invoke(request: Record<string, unknown>): Promise<any>;
  stored(request: Record<string, unknown>): Promise<any>;
  registryPath(): string;
}

function withoutPeriod<T extends { period: { start: string; end: string } }>(value: T) {
  const { period, ...rest } = value;
  return { ...rest, start: period.start, end: period.end };
}

export function registerAlphaTools(server: McpServer, runtime: AlphaToolsRuntime): void {
  const submit = (job: Record<string, unknown>) => runtime.stored({ method: "submit_alpha", job });

  server.tool(
    "quant_prepare_alpha_dataset",
    "Submit immutable stock dataset preparation. The dataset records explicit price basis, PIT availability, quality and content-addressed partitions.",
    prepareAlphaDatasetInput.shape,
    async (args) => submit({ kind: "prepare_dataset", ...withoutPeriod(args) }),
  );

  server.tool(
    "quant_capture_options_snapshot",
    "Capture an append-only Yahoo current option-chain snapshot. Current observations never satisfy historical option quote admission.",
    captureOptionsSnapshotInput.shape,
    async (args) => {
      const capture = await captureYahooOptionRows(args.underlying, args.expirations);
      const base = { kind: "capture_option_chain", source: args.source, underlying: args.underlying, expirations: args.expirations, observed_at: capture.observedAt };
      if (canInlineAlphaRows(capture.rows)) return submit({ ...base, rows: capture.rows });
      return submit({ ...base, rows_artifact: await stageAlphaRows(runtime.registryPath(), capture.rows) });
    },
  );

  server.tool(
    "quant_prepare_options_dataset",
    "Prepare a licensed historical option dataset with explicit bid/ask quote policy. ThetaData access is reported as unavailable when not configured.",
    prepareOptionsDatasetInput.shape,
    async (args) => submit({ kind: "prepare_options_dataset", ...withoutPeriod(args) }),
  );

  server.tool(
    "quant_compute_factors",
    "Submit a validated FactorSpec DAG over an immutable stock dataset and publish a feature panel.",
    computeFactorsInput.shape,
    async (args) => submit({ kind: "compute_factors", ...args }),
  );

  server.tool(
    "quant_compute_option_factors",
    "Submit option volatility factor computation with explicit pricing configuration and PIT quote semantics.",
    computeOptionFactorsInput.shape,
    async (args) => submit({ kind: "compute_option_factors", ...args }),
  );

  server.registerTool(
    "quant_train_alpha_model",
    {
      description: "Train a deterministic CPU LightGBM regressor on explicit train/validation windows. Optional baseline_comparison adds fixed short-reversal, RSI, Ridge and LightGBM development metrics on one common cohort. Formal research models exclude Test data and require a completed freeze for OOS evaluation.",
      inputSchema: trainAlphaModelToolFields,
    },
    async (args) => {
      const parsed = trainAlphaModelInput.safeParse(args);
      if (!parsed.success) {
        const fields = [...new Set(parsed.error.issues.flatMap((issue) =>
          issue.code === "unrecognized_keys"
            ? issue.keys.map((key) => [...issue.path, key].join("."))
            : [issue.path.join(".") || "request"],
        ).map((field) => field.slice(0, 128)))].sort().slice(0, 20);
        const audit = await runtime.stored({method: "audit_training_rejection", fields});
        const suffix = audit.isError ? "; rejection could not be recorded" : "";
        return {isError: true, content: [{type: "text", text: `Invalid Alpha training fields: ${fields.join(", ")}${suffix}`}]};
      }
      const { label_spec, windows, ...rest } = parsed.data;
      const split = developmentWindows.parse(windows);
      return submit({
        kind: "train_alpha_model", ...rest, ...label_spec, split,
      });
    },
  );

  server.registerTool(
    "quant_run_alpha_walk_forward",
    {
      description: "Run leakage-safe development walk-forward validation for the fixed short-reversal, RSI, Ridge and nested-tuned LightGBM candidates. Outer validation is never used for fitting or early stopping, and fold models are never registered or frozen.",
      inputSchema: walkForwardAlphaToolFields,
    },
    async (args) => {
      const parsed = walkForwardAlphaInput.safeParse(args);
      if (!parsed.success) {
        const fields = [...new Set(parsed.error.issues.flatMap((issue) =>
          issue.code === "unrecognized_keys"
            ? issue.keys.map((key) => [...issue.path, key].join("."))
            : [issue.path.join(".") || "request"],
        ).map((field) => field.slice(0, 128)))].sort().slice(0, 20);
        const audit = await runtime.stored({method: "audit_walk_forward_rejection", fields});
        const suffix = audit.isError ? "; rejection could not be recorded" : "";
        return {isError: true, content: [{type: "text", text: `Invalid Alpha walk-forward fields: ${fields.join(", ")}${suffix}`}]};
      }
      return submit({kind: "walk_forward_alpha", ...parsed.data});
    },
  );

  server.tool(
    "quant_predict_alpha",
    "Predict with a frozen model set over a non-overlapping feature period and publish an immutable prediction panel.",
    predictAlphaInput.shape,
    async (args) => submit({ kind: "predict_alpha", ...withoutPeriod(args) }),
  );

  server.tool(
    "quant_freeze_alpha_selection",
    "Freeze a Validation-selected model and Test identities into a content-addressed immutable record. This is the only gate that permits an OOS evaluation.",
    freezeAlphaSelectionInput.shape,
    async (args) => submit({
      kind: "freeze_alpha_selection", ...args,
      idempotency_key: `alpha_freeze:${createHash("sha256").update(JSON.stringify(args)).digest("hex")}`,
    }),
  );

  server.tool(
    "quant_register_alpha_research_protocol",
    "Create or reference a Research Campaign and register an immutable server-hashed Protocol. The first Protocol is preregistered; later revisions must name their parent and preserve Campaign budgets.",
    registerAlphaResearchProtocolFields.shape,
    async (args) => {
      const parsed = registerAlphaResearchProtocolInput.safeParse(args);
      if (!parsed.success) {
        return {isError: true, content: [{type: "text" as const, text: parsed.error.issues.map((issue) => issue.message).join("; ")}]};
      }
      return runtime.stored({ method: "register_alpha_research_protocol", ...parsed.data });
    },
  );

  server.tool(
    "quant_register_alpha_trial",
    "Register a canonical research request without executing it. A managed Protocol requires this Trial before training or walk-forward submission.",
    registerAlphaTrialInput.shape,
    async (args) => runtime.stored({ method: "register_alpha_trial", ...args }),
  );

  server.tool(
    "quant_get_alpha_research_ledger",
    "Read the paginated Campaign ledger, lifecycle events, integrity status, budget use and evidence references.",
    alphaResearchLedgerInput.shape,
    async (args) => runtime.stored({ method: "get_alpha_research_ledger", ...args }),
  );

  server.tool(
    "quant_seal_alpha_candidate_ledger",
    "Reconcile a Campaign, seal an immutable Candidate Ledger Snapshot only when all trials are terminal, and apply the predeclared candidate selection rule. Never starts Freeze or OOS.",
    sealAlphaCandidateLedgerInput.shape,
    async (args) => runtime.stored({ method: "seal_alpha_candidate_ledger", ...args }),
  );

  server.tool(
    "quant_get_alpha_research_report",
    "Build a deterministic evidence report bound to a sealed Snapshot or current watermark, with paginated hypotheses, reuse, exposure, PR3 and reconstructed PR4 evidence.",
    alphaResearchReportInput.shape,
    async (args) => runtime.stored({ method: "get_alpha_research_report", ...args }),
  );

  server.tool(
    "quant_get_alpha_model_diagnostics",
    "Read a verified frozen Alpha model and recompute feature gain/split importance. Stability remains NOT_TESTED unless independently trained development-window folds are present.",
    alphaModelDiagnosticsInput.shape,
    async (args) => runtime.stored({ method: "alpha_model_diagnostics", ...args }),
  );

  server.tool(
    "quant_evaluate_alpha_oos",
    "Submit an independent OOS evaluation that reads only the frozen model and Test inputs.",
    evaluateAlphaOOSInput.shape,
    async (args) => submit({ kind: "evaluate_alpha_oos", ...args, idempotency_key: `alpha_oos:${args.freeze_id}` }),
  );

  server.tool(
    "quant_backtest_alpha_portfolio",
    "Submit a Top-K stock portfolio backtest using only frozen predictions and next-open fills.",
    backtestAlphaPortfolioInput.shape,
    async (args) => submit({ kind: "backtest_alpha_portfolio", ...args }),
  );

  server.tool(
    "quant_backtest_options",
    "Submit a long-only historical option backtest. Buys require next valid ask and sells require next valid bid.",
    backtestOptionsInput.shape,
    async (args) => submit({ kind: "backtest_options", ...args }),
  );
}
