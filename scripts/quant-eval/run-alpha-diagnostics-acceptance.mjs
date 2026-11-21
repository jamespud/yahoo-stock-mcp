// Deterministic synthetic acceptance for the frozen Alpha v1.3 diagnostics protocol.
import assert from "node:assert/strict";
import {execFileSync} from "node:child_process";
import {createHash} from "node:crypto";
import {existsSync, mkdirSync, readFileSync, writeFileSync} from "node:fs";
import {dirname, resolve} from "node:path";
import {fileURLToPath} from "node:url";

const root = fileURLToPath(new URL("../../", import.meta.url));
const python = process.env.QUANT_PYTHON || resolve(root, "quant/.venv/bin/python");
const defaultEvidencePath = resolve(root, "docs/evidence/alpha-diagnostics-v1-3.json");

function parseArgs(argv) {
  const result = {checkEvidence: false, writeEvidence: false};
  for (let index = 0; index < argv.length; index++) {
    const arg = argv[index];
    if (arg === "--check-evidence") result.checkEvidence = true;
    else if (arg === "--write-evidence") result.writeEvidence = true;
    else if (arg === "--replace-evidence") result.replaceEvidence = true;
    else if (arg === "--synthetic-report") result.syntheticReport = resolve(argv[++index]);
    else if (arg === "--command-results") result.commandResults = resolve(argv[++index]);
    else if (arg === "--evidence-path") result.evidencePath = resolve(argv[++index]);
    else throw new Error(`Unknown argument: ${arg}`);
  }
  if (!result.syntheticReport && !result.writeEvidence) result.checkEvidence = true;
  assert(!(result.checkEvidence && result.writeEvidence), "Use either --check-evidence or --write-evidence");
  return result;
}

function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

function runPython(script, args = []) {
  return execFileSync(python, ["-c", script, ...args], {
    cwd: root,
    env: {...process.env, PYTHONPATH: resolve(root, "quant/src")},
    encoding: "utf8",
    maxBuffer: 16 * 1024 * 1024,
  }).trim();
}

function gitOutput(...args) {
  return execFileSync("git", args, {cwd: root, encoding: "utf8"}).trim();
}

const protocol = JSON.parse(runPython(String.raw`
import json
from stock_quant.alpha.models.diagnostic_protocol import (
    EXPECTED_NPM_TEST_FAILURE_LABELS,
    NPM_TEST_BASELINE_COMMIT,
    NPM_TEST_FAILED_SUITE,
    PROTOCOL_HASH,
    frozen_protocol,
    protocol_canonical_json,
)
print(json.dumps({
    "hash": PROTOCOL_HASH,
    "canonical_json": protocol_canonical_json(),
    "document": frozen_protocol(),
    "npm_test_baseline": {
        "baseline_commit": NPM_TEST_BASELINE_COMMIT,
        "failed_suite": NPM_TEST_FAILED_SUITE,
        "failure_labels": list(EXPECTED_NPM_TEST_FAILURE_LABELS),
    },
}, sort_keys=True, separators=(",", ":"), allow_nan=False))
`));

const synthetic = JSON.parse(runPython(String.raw`
import json
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import numpy as np
from scipy.stats import spearmanr

from stock_quant.alpha.contracts import StockDatasetRequest
from stock_quant.alpha.data.catalog import DatasetStore
from stock_quant.alpha.models.diagnostic_protocol import (
    PROTOCOL_HASH,
    SYNTHETIC_END,
    SYNTHETIC_START,
    assert_development_window,
    frozen_protocol,
)
from stock_quant.alpha.models.labels import LabelRow
from stock_quant.alpha.models.lightgbm_trainer import (
    AlphaModelRegistry,
    ModelResourceBudget,
    TrainAlphaRequest,
    train_alpha,
)
from stock_quant.alpha.models.signal_diagnostics import (
    DirectSignalSpec,
    evaluate_direct_signal,
    evaluate_weekly_signal,
    moving_block_bootstrap,
    regime_breakdown,
    stability_by_year,
    top20_forward_diagnostics,
    weekly_last_session_dates,
)
from stock_quant.alpha.models.temporal_split import ModelDevelopmentSplit

UTC = timezone.utc
admitted = False
def admit():
    global admitted
    admitted = True

assert_development_window(SYNTHETIC_START, SYNTHETIC_END, on_admit=admit)
assert admitted
protocol = frozen_protocol()

symbols = [f"S{index:02d}" for index in range(24)]
sessions = []
cursor = date(2009, 1, 5)
while len(sessions) < 60:
    if cursor.weekday() < 5 and cursor != date(2009, 2, 6):
        sessions.append(cursor)
    cursor += timedelta(days=1)

features = []
labels = []
for day_index, session in enumerate(sessions):
    decision_at = datetime.combine(session, time(21, 5), tzinfo=UTC)
    for index, symbol in enumerate(symbols):
        mom63 = float((index - day_index * 2) % 24 - 12)
        ret5 = float((day_index * 3 - index) % 17 - 8)
        label_value = 0.002 * mom63 - 0.0015 * ret5 + 0.00001 * index
        for factor_name, factor_value in (("mom63", mom63), ("ret5", ret5)):
            features.append({
                "decision_at": decision_at,
                "instrument_id": symbol,
                "factor_name": factor_name,
                "factor_value": factor_value,
                "feature_available_at": decision_at,
                "eligible": True,
                "quality_flags": [],
                "factor_version": "synthetic-diagnostic-v1",
            })
        labels.append(LabelRow(
            feature_decision_at=decision_at,
            entry_at=decision_at + timedelta(hours=1),
            exit_at=decision_at + timedelta(days=7),
            label_available_at=decision_at + timedelta(days=7),
            label_value=label_value,
            label_quality=[],
            instrument_id=symbol,
            label_name="stock_forward_return",
            label_version="session-aligned-v2",
        ))

label_by_key = {(row.instrument_id, row.feature_decision_at.date()): row.label_value for row in labels}
specs = {
    record["id"]: DirectSignalSpec(
        factor_name=record["factor_name"],
        score_multiplier=record["score_multiplier"],
        version=record["version"],
    )
    for record in protocol["signals"]
}

direct_results = {}
oracle_delta = 0.0
for signal_id, spec in specs.items():
    result = evaluate_direct_signal(
        features,
        labels,
        expected_instruments=symbols,
        expected_sessions=sessions,
        spec=spec,
    )
    assert result["summary"]["valid_ic_decision_count"] == len(sessions)
    for record in result["rank_ic_by_decision"]:
        session = date.fromisoformat(record["decision_at"][:10])
        values = [
            (row["factor_value"] * spec.score_multiplier, label_by_key[(row["instrument_id"], session)])
            for row in features
            if row["factor_name"] == spec.factor_name and row["decision_at"].date() == session
        ]
        oracle = float(spearmanr(
            [value[1] for value in values],
            [value[0] for value in values],
        ).statistic)
        oracle_delta = max(oracle_delta, abs(oracle - record["rank_ic"]))
    direct_results[signal_id] = result

b1 = {
    "status": "PASS" if oracle_delta < 1e-12 else "FAIL",
    "signals": list(specs),
    "decision_count": len(sessions),
    "scipy_oracle_max_abs_delta": oracle_delta,
    "rank_ic_mean": {
        signal_id: result["summary"]["rank_ic_mean"]
        for signal_id, result in direct_results.items()
    },
    "icir": {
        signal_id: result["summary"]["icir"]
        for signal_id, result in direct_results.items()
    },
}

daily = direct_results["D01"]
weekly = evaluate_weekly_signal(
    features,
    labels,
    expected_instruments=symbols,
    expected_sessions=sessions,
    spec=specs["D01"],
)
actual_weekly = weekly_last_session_dates(sessions)
reported_weekly = [date.fromisoformat(value) for value in weekly["summary"]["weekly_decision_dates"]]
missing_friday = date(2009, 2, 6)
b2_pass = (
    reported_weekly == actual_weekly
    and daily["summary"]["decision_count"] == len(sessions)
    and weekly["summary"]["weekly_decision_count"] == len(actual_weekly)
    and missing_friday not in actual_weekly
    and date(2009, 2, 5) in actual_weekly
)
b2 = {
    "status": "PASS" if b2_pass else "FAIL",
    "daily_decision_count": daily["summary"]["decision_count"],
    "weekly_decision_count": weekly["summary"]["weekly_decision_count"],
    "weekly_actual_session_rule": b2_pass,
    "missing_friday": missing_friday.isoformat(),
    "actual_last_session_that_week": date(2009, 2, 5).isoformat(),
    "daily_frequency": "daily_observed_sessions",
    "weekly_frequency": "weekly_last_trading_session",
}

top20 = top20_forward_diagnostics(
    features,
    labels,
    expected_instruments=symbols,
    expected_sessions=sessions,
    spec=specs["D01"],
)
turnovers = [
    record["turnover_proxy"]
    for record in top20["top20_forward_labels"]
    if record["turnover_proxy"] is not None
]
b3_pass = (
    top20["summary"]["analysis_label"] == "predictive_label_analysis_not_executable_backtest"
    and top20["summary"]["complete_selection_count"] == top20["summary"]["decision_count"]
    and bool(turnovers)
)
b3 = {
    "status": "PASS" if b3_pass else "FAIL",
    "analysis_label": top20["summary"]["analysis_label"],
    "decision_count": top20["summary"]["decision_count"],
    "complete_selection_count": top20["summary"]["complete_selection_count"],
    "top20_size": top20["summary"]["top20_size"],
    "turnover_proxy_count": len(turnovers),
    "turnover_proxy_first": turnovers[0] if turnovers else None,
}

with tempfile.TemporaryDirectory(prefix="alpha-diagnostic-b4-") as temporary:
    temp_root = Path(temporary)
    store = DatasetStore(temp_root / "registry.alpha")
    rng = np.random.default_rng(20261121)
    training_start = datetime(2009, 1, 5, tzinfo=UTC)
    model_symbols = [f"M{index:02d}" for index in range(20)]
    model_features = []
    model_labels = []
    for day_index in range(50):
        decision_at = training_start + timedelta(days=day_index, hours=21)
        for instrument_index, symbol in enumerate(model_symbols):
            x1, x2 = rng.normal(size=2)
            target = 0.8 * x1 - 0.4 * x2 + 0.03 * rng.normal()
            for factor_name, factor_value in (("f1", x1), ("f2", x2)):
                model_features.append({
                    "decision_at": decision_at,
                    "instrument_id": symbol,
                    "factor_name": factor_name,
                    "factor_value": float(factor_value),
                    "feature_available_at": decision_at,
                    "eligible": True,
                    "quality_flags": [],
                    "factor_version": "synthetic-b4-v1",
                })
            model_labels.append(LabelRow(
                feature_decision_at=decision_at,
                entry_at=decision_at + timedelta(hours=1),
                exit_at=decision_at + timedelta(days=5),
                label_available_at=decision_at + timedelta(days=5),
                label_value=float(target),
                label_quality=[],
                instrument_id=symbol,
                label_name="stock_forward_return",
                label_version="session-aligned-v2",
            ))
    parent = store.publish_stock(
        StockDatasetRequest(
            source="fixture",
            symbols=[model_symbols[0]],
            start=training_start,
            end=training_start + timedelta(days=1),
        ),
        [{
            "instrument_id": model_symbols[0],
            "start_at": training_start,
            "end_at": training_start + timedelta(hours=6),
            "available_at": training_start + timedelta(hours=6),
            "open": 100,
            "high": 101,
            "low": 99,
            "close": 100,
            "volume": 1,
            "adjustment": "raw",
            "source": "fixture",
            "source_version": "synthetic-b4-v1",
            "currency": "USD",
        }],
        calendar_version="synthetic-b4-v1",
        source_version="synthetic-b4-v1",
        data_quality={},
    )
    feature_set = store.publish_feature_panel(
        model_features,
        parent_dataset_id=parent.dataset_id,
        factor_spec_hash="sha256:" + "b" * 64,
        instrument_ids=model_symbols,
        start_at=training_start,
        end_at=training_start + timedelta(days=51),
        calendar_version="synthetic-b4-v1",
        price_basis="price_basis_unverified",
    )
    split = ModelDevelopmentSplit(
        train_start=training_start,
        train_end=training_start + timedelta(days=35),
        validation_start=training_start + timedelta(days=35),
        validation_end=training_start + timedelta(days=45),
        embargo_days=0,
    )
    base_request = TrainAlphaRequest(
        feature_set_id=feature_set.dataset_id,
        label_source_dataset_id=parent.dataset_id,
        label_name="stock_forward_return",
        target_type="stock_return",
        evaluation_mode="validation_only",
        research_protocol_hash=PROTOCOL_HASH,
        split=split,
        hyperparameters={
            "learning_rate": 0.08,
            "num_leaves": 7,
            "min_child_samples": 5,
            "n_estimators": 4,
        },
        random_seed=20261121,
        resource_budget=ModelResourceBudget(max_seconds=60, max_rows=10000),
        min_rank_ic=-1,
    )
    l2_model = train_alpha(
        base_request.model_copy(update={"selection_metric": "l2"}),
        store=store,
        registry=AlphaModelRegistry(temp_root / "models-l2"),
        label_rows=model_labels,
    )
    rank_model = train_alpha(
        base_request.model_copy(update={"selection_metric": "daily_rank_ic"}),
        store=store,
        registry=AlphaModelRegistry(temp_root / "models-rank-ic"),
        label_rows=model_labels,
    )
    l2_diagnostics = l2_model.selection_diagnostics
    rank_diagnostics = rank_model.selection_diagnostics
b4_pass = (
    l2_diagnostics["selection_metric"] == "l2"
    and rank_diagnostics["selection_metric"] == "daily_rank_ic"
    and l2_diagnostics["candidate_iteration_count"] == rank_diagnostics["candidate_iteration_count"] == 4
    and l2_diagnostics["candidate_iterations_contiguous"] is True
    and rank_diagnostics["candidate_iterations_contiguous"] is True
    and l2_diagnostics["tree_path"] == rank_diagnostics["tree_path"] == "single_booster_all_candidate_iterations"
)
b4 = {
    "status": "PASS" if b4_pass else "FAIL",
    "candidate_iteration_count": l2_diagnostics["candidate_iteration_count"],
    "candidate_iteration_start": 1,
    "candidate_iteration_end": 4,
    "equal_candidate_budget": l2_diagnostics["candidate_iteration_count"] == rank_diagnostics["candidate_iteration_count"],
    "same_tree_path": l2_diagnostics["tree_path"] == rank_diagnostics["tree_path"],
    "tie_rule": "first_iteration_on_exact_tie",
    "selected_iteration": {
        "l2": l2_model.selected_iteration,
        "daily_rank_ic": rank_model.selected_iteration,
    },
    "validation_only": l2_model.evaluation_mode == rank_model.evaluation_mode == "validation_only",
    "model_hash_equal": l2_model.model_hash == rank_model.model_hash,
}

weekly_ic = [
    {
        "decision_at": (datetime(2009, 1, 2, 21, 5, tzinfo=UTC) + timedelta(weeks=index)).isoformat(),
        "rank_ic": 0.01 * ((index % 5) + 1),
        "paired_rank_ic": -0.005 * ((index % 4) + 1),
    }
    for index in range(12)
]
bootstrap_first = moving_block_bootstrap(weekly_ic)
bootstrap_second = moving_block_bootstrap(weekly_ic)
stability_records = [
    {
        "decision_at": (datetime(2009, 2, 2, 21, 5, tzinfo=UTC) + timedelta(days=index * 90)).isoformat(),
        "rank_ic": 0.02 if index % 2 == 0 else 0.01,
        "expected_count": 24,
        "valid_count": 24,
        "excluded_count": 0,
        "exclusion_reasons": [],
    }
    for index in range(6)
]
annual = stability_by_year(stability_records)
regime_sessions = []
cursor = date(2009, 1, 5)
while len(regime_sessions) < 64:
    if cursor.weekday() < 5:
        regime_sessions.append(cursor)
    cursor += timedelta(days=1)
benchmark_rows = []
for index, session in enumerate(regime_sessions):
    available_at = datetime.combine(session, time(21, 5), tzinfo=UTC)
    benchmark_rows.append({
        "instrument_id": "SPY",
        "start_at": datetime.combine(session, time(14, 30), tzinfo=UTC),
        "end_at": datetime.combine(session, time(21, 0), tzinfo=UTC),
        "available_at": available_at,
        "close": 100.0 + index,
    })
regime_decision = datetime.combine(regime_sessions[-1], time(21, 5), tzinfo=UTC)
regimes = regime_breakdown(
    [{
        "decision_at": regime_decision.isoformat(),
        "rank_ic": 0.1,
        "expected_count": 24,
        "valid_count": 24,
        "excluded_count": 0,
        "exclusion_reasons": [],
    }],
    benchmark_rows,
    benchmark_symbol="SPY",
    expected_sessions=regime_sessions,
)
up = next(record for record in regimes if record["regime"] == "UP")
bootstrap_config = bootstrap_first["config"]
configured_bootstrap = protocol["bootstrap"]
b5_pass = (
    bootstrap_first == bootstrap_second
    and bootstrap_first["status"] == "TESTED"
    and bootstrap_config["draws"] == configured_bootstrap["draws"]
    and bootstrap_config["block_weeks"] == configured_bootstrap["block_weeks"]
    and bootstrap_config["seed"] == configured_bootstrap["seed"]
    and len(annual) >= 2
    and up["valid_ic_decision_count"] == 1
)
b5 = {
    "status": "PASS" if b5_pass else "FAIL",
    "annual_period_count": len(annual),
    "regime_names": [record["regime"] for record in regimes],
    "up_regime_decision_count": up["valid_ic_decision_count"],
    "bootstrap": bootstrap_first,
    "deterministic_equal": bootstrap_first == bootstrap_second,
}

sections = {"B1": b1, "B2": b2, "B3": b3, "B4": b4, "B5": b5}
report = {
    "schema_version": "alpha-diagnostics-synthetic-acceptance-v1",
    "scope": "RESEARCH_DIAGNOSTIC_READINESS_SYNTHETIC_CONTRACT_VALIDATION",
    "protocol_hash": PROTOCOL_HASH,
    "protocol_version": protocol["protocol_version"],
    "fixture": {
        "kind": "deterministic_synthetic_development_fixture",
        "symbol_count": len(symbols),
        "session_count": len(sessions),
        "start": sessions[0].isoformat(),
        "end": sessions[-1].isoformat(),
        "label_version": "session-aligned-v2",
        "price_basis": "synthetic_only",
    },
    "sections": sections,
    "reserved_test_reads": 0,
    "test_oos_executions": 0,
    "candidate_promotions": 0,
    "real_market_trading_validity": "BLOCKED",
    "claim": "Synthetic contract validation only; no real-study readiness or tradability claim.",
}
assert all(section["status"] == "PASS" for section in sections.values())
print(json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False))
`));

const reportHash = sha256(canonicalJson(synthetic));
assert.equal(synthetic.protocol_hash, protocol.hash);
assert.equal(synthetic.scope, "RESEARCH_DIAGNOSTIC_READINESS_SYNTHETIC_CONTRACT_VALIDATION");
assert.deepEqual(Object.keys(synthetic.sections).sort(), ["B1", "B2", "B3", "B4", "B5"]);
assert(synthetic.sections.B1.signals.includes("D01") && synthetic.sections.B1.signals.includes("D02"));
assert.equal(synthetic.sections.B1.scipy_oracle_max_abs_delta < 1e-12, true);
assert.equal(synthetic.sections.B2.weekly_actual_session_rule, true);
assert.equal(synthetic.sections.B3.analysis_label, "predictive_label_analysis_not_executable_backtest");
assert.equal(synthetic.sections.B4.equal_candidate_budget, true);
assert.equal(synthetic.sections.B5.bootstrap.config.draws, 2000);
assert.equal(synthetic.real_market_trading_validity, "BLOCKED");
assert.equal(synthetic.candidate_promotions, 0);
assert.equal(synthetic.test_oos_executions, 0);
assert.equal(synthetic.reserved_test_reads, 0);

const options = parseArgs(process.argv.slice(2));
const activeEvidencePath = options.evidencePath || defaultEvidencePath;
const activeChecksumPath = activeEvidencePath.endsWith(".json")
  ? `${activeEvidencePath.slice(0, -5)}.sha256`
  : `${activeEvidencePath}.sha256`;
if (options.syntheticReport) {
  mkdirSync(dirname(options.syntheticReport), {recursive: true});
  writeFileSync(options.syntheticReport, JSON.stringify(synthetic, null, 2) + "\n");
}

function protocolBody(protocolRecord) {
  const derived = new Set(["protocol_hash", "source_protocol_hash", "symbol_count", "symbols"]);
  return Object.fromEntries(Object.entries(protocolRecord).filter(([key]) => !derived.has(key)));
}

function assertProtocolIntegrity(evidence) {
  const body = protocolBody(evidence.protocol);
  assert.deepEqual(body, protocol.document, "Protocol body differs from the canonical frozen diagnostics protocol");
  assert.equal(
    `sha256:${sha256(canonicalJson(body))}`,
    evidence.protocol.protocol_hash,
    "Protocol hash does not independently rehash the embedded protocol body",
  );
  assert.equal(evidence.protocol.protocol_hash, protocol.hash);
  assert.deepEqual(evidence.protocol.symbols, protocol.document.source_identity.symbols);
}

function assertNpmTestDisclosure(npmTest) {
  assert(npmTest && typeof npmTest === "object", "npm test disclosure is required");
  const baseline = protocol.npm_test_baseline;
  assert.equal(npmTest.status, "FAILED", "npm test must be disclosed as FAILED");
  assert.equal(npmTest.exit_code, 1, "npm test must retain exit code 1");
  assert.equal(npmTest.failed_suite, baseline.failed_suite, "npm test failed suite mismatch");
  assert.equal(npmTest.failure_count, baseline.failure_labels.length, "npm test failure count mismatch");
  assert.deepEqual(npmTest.failure_labels, baseline.failure_labels, "npm test failure labels do not match baseline");
  assert.equal(npmTest.failure_labels_match_saved_baseline, true, "npm test baseline comparison must be true");
  assert.equal(npmTest.baseline_commit, baseline.baseline_commit, "npm test baseline commit mismatch");
  assert.equal(npmTest.byte_identical_logs, false, "npm test logs must not claim byte identity");
  const scope = String(npmTest.comparison_scope || "").toLowerCase();
  assert(scope.includes("not byte-identical") && scope.includes("not green"), "npm test must remain explicitly non-green");
}

function assertEvidenceSemantics(evidence) {
  assertProtocolIntegrity(evidence);
  assertNpmTestDisclosure(evidence.full_npm_test);
  assert.equal(evidence.statuses.SYNTHETIC_CONTRACT_VALIDATION, "PASS");
  assert.equal(evidence.statuses.RESEARCH_DIAGNOSTIC_READINESS, "NOT_TESTED");
  assert.equal(evidence.statuses.REAL_MARKET_TRADING_VALIDITY, "BLOCKED");
  assert.equal(evidence.real_study.status, "NOT_EXECUTED");
  assert.equal(evidence.real_study.test_oos_executions, 0);
  assert.equal(evidence.real_study.candidate_promotions, 0);
  const source = evidence.source;
  assert(source && typeof source === "object", "Evidence source provenance is required");
  assert.match(source.commit, /^[0-9a-f]{40}$/, "Evidence source commit must be a full Git SHA");
  assert.equal(evidence.commands_source_commit, source.commit, "Evidence command source commit mismatch");
  assert.equal(source.acceptance_ran_at_commit, true, "Acceptance must run at the recorded source commit");
  assert.equal(source.worktree_clean_before_generation, true, "Evidence generation requires a clean worktree");
  gitOutput("merge-base", "--is-ancestor", source.commit, "HEAD");
  assert.equal(evidence.synthetic_acceptance.report_hash, `sha256:${reportHash}`);
  assert.deepEqual(evidence.synthetic_acceptance.sections, synthetic.sections);
}

function syntheticAttachment() {
  return {
    status: "PASS",
    scope: synthetic.scope,
    report_hash: `sha256:${reportHash}`,
    fixture: synthetic.fixture,
    section_hashes: Object.fromEntries(
      Object.entries(synthetic.sections).map(([name, section]) => [
        name, `sha256:${sha256(canonicalJson(section))}`,
      ]),
    ),
    sections: synthetic.sections,
  };
}

function evidenceDocument(commandResults) {
  const document = protocol.document;
  return {
    schema_version: "alpha-diagnostics-evidence-v1",
    generated_at: new Date().toISOString(),
    source: {
      commit: commandResults.source_commit,
      acceptance_ran_at_commit: true,
      worktree_clean_before_generation: true,
    },
    commands_source_commit: commandResults.source_commit,
    protocol: {
      ...document,
      protocol_hash: protocol.hash,
      source_protocol_hash: document.source_identity.protocol_hash,
      symbol_count: document.source_identity.symbol_count,
      symbols: document.source_identity.symbols,
    },
    statuses: {
      SYNTHETIC_CONTRACT_VALIDATION: "PASS",
      RESEARCH_DIAGNOSTIC_READINESS: "NOT_TESTED",
      REAL_MARKET_TRADING_VALIDITY: "BLOCKED",
    },
    real_study: {
      status: "NOT_EXECUTED",
      reason: "PR A historical calendar gate remains BLOCKED; real diagnostics are not authorized",
      real_diagnostic_jobs: 0,
      features_labels_predictions_created_from_real_data: 0,
      test_oos_executions: 0,
      candidate_promotions: 0,
    },
    exclusions: {
      reserved_test_start: document.windows.reserved_test.start,
      reserved_test_end: document.windows.reserved_test.end,
      reserved_test_rows_read: 0,
      oos_artifacts_read: 0,
      candidate_ledger_mutated: false,
    },
    synthetic_acceptance: syntheticAttachment(),
    commands: commandResults?.commands || [],
    full_npm_test: commandResults.full_npm_test,
    baseline_comparison: commandResults.baseline_comparison,
    evidence_scope: "SYNTHETIC_CONTRACT_VALIDATION_ONLY",
    claim_boundaries: document.claim_boundaries,
  };
}

function verifyCommittedEvidence() {
  assert(existsSync(activeEvidencePath), `Missing evidence attachment: ${activeEvidencePath}`);
  assert(existsSync(activeChecksumPath), `Missing evidence checksum: ${activeChecksumPath}`);
  const bytes = readFileSync(activeEvidencePath);
  const evidence = JSON.parse(bytes.toString("utf8"));
  const expectedLine = `${sha256(bytes)}  ${resolve(activeEvidencePath).split("/").at(-1)}`;
  assert.equal(readFileSync(activeChecksumPath, "utf8").trim(), expectedLine, "Evidence SHA-256 mismatch");
  runPython(String.raw`
import json, sys
from stock_quant.alpha.models.diagnostic_protocol import validate_diagnostic_evidence
with open(sys.argv[1], encoding="utf-8") as handle:
    validate_diagnostic_evidence(json.load(handle))
`, [activeEvidencePath]);
  assertEvidenceSemantics(evidence);
  assert.equal(evidence.protocol.symbol_count, 50);
  const serialized = JSON.stringify(evidence);
  assert(!serialized.includes("/home/spud"), "Evidence contains a private filesystem path");
  assert(!serialized.includes("raw_bars"), "Evidence contains raw bar data");
  assert(!serialized.includes("price_history"), "Evidence contains raw price history");
}

function writeCommittedEvidence() {
  if (existsSync(activeEvidencePath) && !options.replaceEvidence) {
    throw new Error("Evidence already exists; pass --replace-evidence to replace it");
  }
  if (!options.commandResults) {
    throw new Error("Evidence regeneration requires npm test command results");
  }
  const commandResults = JSON.parse(readFileSync(options.commandResults, "utf8"));
  const evidence = evidenceDocument(commandResults);
  assertEvidenceSemantics(evidence);
  const currentCommit = gitOutput("rev-parse", "HEAD");
  const dirty = gitOutput("status", "--porcelain", "--untracked-files=normal");
  const allowedEvidencePaths = new Set([
    "docs/evidence/alpha-diagnostics-v1-3.json",
    "docs/evidence/alpha-diagnostics-v1-3.sha256",
  ]);
  const unexpectedChanges = dirty
    ? dirty.split("\n").map(line => line.replace(/^[ MADRCU?!]{2}\s/, "").trim()).filter(path => !allowedEvidencePaths.has(path))
    : [];
  assert.equal(unexpectedChanges.length, 0, `Evidence regeneration requires a clean source worktree: ${unexpectedChanges.join(", ")}`);
  assert.equal(commandResults.source_commit, currentCommit, "Evidence command results source commit mismatch");
  const bytes = Buffer.from(JSON.stringify(evidence, null, 2) + "\n");
  mkdirSync(dirname(activeEvidencePath), {recursive: true});
  writeFileSync(activeEvidencePath, bytes);
  writeFileSync(activeChecksumPath, `${sha256(bytes)}  ${resolve(activeEvidencePath).split("/").at(-1)}\n`);
}

if (options.writeEvidence) writeCommittedEvidence();
if (options.checkEvidence) verifyCommittedEvidence();

console.log(JSON.stringify({
  status: "PASS",
  protocol_hash: protocol.hash,
  synthetic_report_hash: `sha256:${reportHash}`,
  sections: Object.fromEntries(Object.entries(synthetic.sections).map(([name, value]) => [name, value.status])),
  evidence_checked: options.checkEvidence,
}));
