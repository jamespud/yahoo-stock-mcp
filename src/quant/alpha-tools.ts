import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import type { z } from "zod";
import {
  freezeAlphaSelectionInput,
  evaluateAlphaOOSInput,
  backtestAlphaPortfolioInput,
  backtestOptionsInput,
  captureOptionsSnapshotInput,
  computeFactorsInput,
  computeOptionFactorsInput,
  predictAlphaInput,
  prepareAlphaDatasetInput,
  prepareOptionsDatasetInput,
  trainAlphaModelInput,
} from "./alpha-contracts.js";
import { developmentWindows, temporalWindows } from "./alpha-contracts.js";
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

  server.tool(
    "quant_train_alpha_model",
    "Train a deterministic CPU LightGBM regressor with chronological train/validation/test windows, label purging and frozen OOS evaluation.",
    trainAlphaModelInput.shape,
    async (args) => {
      const { label_spec, windows, ...rest } = args;
      const split = developmentWindows.safeParse(windows).success ? windows : temporalWindows.parse(windows);
      return submit({
        kind: "train_alpha_model", ...rest, ...label_spec, split,
      });
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
    async (args) => submit({ kind: "freeze_alpha_selection", ...args }),
  );

  server.tool(
    "quant_evaluate_alpha_oos",
    "Submit an independent OOS evaluation that reads only the frozen model and Test inputs.",
    evaluateAlphaOOSInput.shape,
    async (args) => submit({ kind: "evaluate_alpha_oos", ...args }),
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
