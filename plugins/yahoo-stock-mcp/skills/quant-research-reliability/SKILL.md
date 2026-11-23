---
name: quant-research-reliability
description: Use when auditing quantitative research for leakage, overfitting, benchmark validity, cost sensitivity, data quality, accounting consistency, or reproducibility before trusting a backtest or parameter search.
---

# Quant research reliability

Produce an evidence-based audit verdict: supported, conditional or unsupported, with each conclusion
linked to returned data and a specific limitation. A profitable backtest alone is not a reliability verdict.
Read [data policy](references/data-policy.md) and [quant research policy](references/quant-research-policy.md)
first. All references required to install this skill independently live inside this directory.

## MCP tools used

- `quant_get_capabilities` - discover deployed limits, execution models and result sections
- `quant_validate_strategy` - inspect types and explicit time alignment in the supplied DSL
- `quant_register_strategy` - register an authorized immutable comparison definition
- `quant_compile_strategy` - bind the exact parameters and symbols for a controlled replay
- `quant_get_data_quality` - audit legacy snapshot PIT availability/gaps, or Alpha dataset session coverage, mappings and volume missingness; `section="research_readiness"` returns the evidence-aware research-quality report
- `quant_get_alpha_model_diagnostics` - verify frozen feature importance and development-window stability evidence
- `quant_run_backtest` - submit an authorized cost, benchmark or reproducibility comparison
- `quant_get_job` - verify terminal status and durable diagnostics
- `quant_cancel_job` - cancel authorized work or enforce agreed budgets
- `quant_get_result` - retrieve trial records, ledgers, artifacts and reproducibility
- `quant_freeze_alpha_selection` / `quant_evaluate_alpha_oos` - audit Validation-selected freezes and independent OOS evidence

## Workflow

1. Inspect the actual tools/list, then call `quant_get_capabilities` if available. Quant is opt-in;
   without its tools, report the audit evidence unavailable and route setup to `stock-data-setup`.
   Capabilities do not prove provider coverage or successful Python isolation. No host-code fallback.
2. Establish the research claim and collect strategy_id, plan_id, data_snapshot_id, job_id, universe
   provenance, all search attempts, windows, cost assumptions and code/environment identities.
   Read [reliability checklist](references/reliability-checklist.md). Missing IDs or unreported trials
   are unresolved evidence, not permission to assume a clean experiment.
3. Inspect `quant_get_job` and only read completed result sections as successful research. Fetch
   reproducibility, artifacts, diagnostics, analytics, attribution, fills, final_account, cash_ledger,
   corporate_actions and relevant trials/walk_forward/parameter_sensitivity. Page complete ledgers with
   returned cursors. Check quality at the actual as_of clocks. For Alpha data, treat symbol presence as
   different from session coverage and report missing, extra and duplicate sessions and volume NULL/zero
   counts. Request `section="research_readiness"` for the explicit dimension statuses and findings.
   Report failed candidates and excluded symbols.
4. Audit data availability versus decisions/fills, warmup, explicit alignment, revisions, raw versus
   adjusted prices, corporate actions, historical universe and delisted-asset coverage. A local
   modeled delay cannot establish historical vendor publication times. Preserve these limits.
5. Audit train/validation/test separation, repeated holdout reuse, grid size, selection objective,
   fold stability and cost sensitivity. A runtime enforcing disjoint windows cannot prove the analyst
   did not repeatedly retune on test. Use model diagnostics to distinguish one-model importance from
   actual multi-window stability. Distinguish an independent test from an in-sample winner.
6. Apply [benchmark policy](references/benchmark-policy.md). Require aligned periods, data, currency,
   funding and costs for buy-and-hold/cash/strategy comparisons. If comparison runs are authorized,
   validate/register/compile the explicit baseline, then quant_run_backtest with the existing immutable
   snapshot and agreed config. Do not invent a benchmark tool or fabricate missing returns.
7. Check cash/holdings/equity and attribution.accounting_residual. Reconcile Alpha OOS expected
   opportunities = valid matches + exclusions, and expected IC dates = valid + invalid dates without
   double counting. Stock economic results require scoped price attestation plus complete corporate
   action coverage and daily cash/equity reconciliation. Compare returned metric definitions and null
   conditions. When replay is authorized, freeze every binding and compare IDs, ledgers and costs,
   not just a headline return. Use the local [execution checklist](references/execution-checklist.md)
   and [result guide](references/result-guide.md) for mechanics.
8. Fill the [audit report template](references/report-template.md), marking every criterion pass,
   using NOT_TESTED, SKIPPED, BLOCKED, FAILED or PASSED with evidence and material impact.
   PASSED requires an executed check. SKIPPED means deliberately omitted; BLOCKED means a required
   dependency or permission is missing; NOT_TESTED means no execution evidence exists. State the verdict
   and what evidence bounds it. Never claim independent validation unless an independent Oracle or
   the corresponding independent test was actually executed and its result is available.
   Keep recommended follow-up experiments within the user's scope and budget.

## Routing and common mistakes

Use `quant-strategy-research` to author a hypothesis, DSL, screen or optimization experiment; use
`stock-data-setup` for unavailable tools/data and `technical-analysis` for descriptive indicators.
The [local examples](references/strategy-examples.json) support standalone DSL validation.
Do not certify unseen trial history, treat missing metrics as zero, assume zero trading costs,
compare mismatched benchmarks, or describe a simulation as a broker order or investment advice.
