import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

const root = resolve(import.meta.dirname, "../..");
const archive = resolve(process.env.ALPHA_VALIDITY_ARCHIVE || "/home/spud/proj/sg/research/alpha-restart-2026-11-19");
const outputDir = resolve(root, "reports/alpha-data-validity");
const output = resolve(outputDir, "acceptance.json");
mkdirSync(outputDir, { recursive: true });

if (!existsSync(resolve(archive, "registry.db")) || !existsSync(resolve(archive, "registry.alpha"))) {
  writeFileSync(output, JSON.stringify({
    schema_version: "alpha-data-validity-acceptance-v1",
    generated_at: new Date().toISOString(),
    archive,
    DATA_QUALITY_ENGINEERING: "BLOCKED",
    RESEARCH_DIAGNOSTICS_READINESS: "BLOCKED",
    EXECUTION_ENGINEERING: "FIXTURE_TESTED",
    REAL_MARKET_TRADING_VALIDITY: "BLOCKED",
    blocker: "ARCHIVE_NOT_FOUND",
  }, null, 2));
  console.log(`Alpha data validity archive unavailable; wrote ${output}`);
  process.exit(0);
}

const python = process.env.QUANT_PYTHON || resolve(root, "quant/.venv/bin/python");
const sourceDb = process.env.ALPHA_SOURCE_DB || "/home/spud/proj/sg/research/alpha-2026-11-19/sync-long-2026-11-19/stocks-final-long-history.db";
const script = String.raw`
import json, os, sqlite3, hashlib
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from stock_quant.alpha.data.catalog import DatasetStore
from stock_quant.alpha.data.exchange_mapping import build_exchange_mapping
from stock_quant.alpha.data.session_coverage import audit_session_coverage
from stock_quant.calendar import generate_long_calendar
from stock_quant.alpha.models.labels import stock_forward_labels
from stock_quant.alpha.models.evaluation_coverage import attribute_evaluation_coverage
from stock_quant.alpha.models.feature_diagnostics import diagnose_alpha_model
from stock_quant.alpha.models.registry import AlphaModelRegistry

archive = Path(os.environ["ALPHA_VALIDITY_ARCHIVE"])
source_db = Path(os.environ["ALPHA_SOURCE_DB"])
protected_names = [
    "frozen-protocol.json", "selection-frozen.json", "final-oos-result.json",
    "frozen-predictions-result.json", "development-dataset.json", "test-data-result.json",
]
def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
before_hashes = {name: file_hash(archive / name) for name in protected_names}
store = DatasetStore(archive / "registry.alpha")
dev_id = "sha256:17aac6be963370a02532c17f301b8197023359fee137fe2816375bab972bb002"
test_id = "sha256:cf415f99f740da8a11fc115be827add16f25fd7597f6022294557fa22b0dc8e8"
prediction_id = "sha256:1fcffec19257c1f8170825d55ebf117dcf523ef054457da12b136e9bb996a96d"
model_id = "sha256:1f3dba655042d89d7c7aa5c7695ef7ce8b5097d446eb65da12091eb3f5a38d2e"

dev = store.load_manifest(dev_id)
test = store.load_manifest(test_id)
dev_rows = store.read_rows(dev_id)
test_rows = store.read_rows(test_id)
prediction_rows = store.read_rows(prediction_id)
calendar = generate_long_calendar(calendar_name="XNYS", exchange="XNYS", start_date=dev.start_at.date(), end_date=dev.end_at.date())
test_calendar = generate_long_calendar(calendar_name="XNYS", exchange="XNYS", start_date=test.start_at.date(), end_date=test.end_at.date())
calendar_dates = [session.date for session in calendar.sessions]
session_dates = {}
for row in dev_rows:
    session_dates.setdefault(row["instrument_id"], []).append(row["start_at"].date())
reports = audit_session_coverage(
    dev_rows,
    requested_symbols=dev.instrument_ids,
    calendar_session_dates=calendar_dates,
    calendar_id=calendar.id,
    session_dates_by_symbol=session_dates,
)
coverage_values = [row["session_coverage"] for row in reports if row["session_coverage"] is not None]
session_summary = {
    "instrument_count": len(reports),
    "complete_count": sum(row["session_status"] == "COMPLETE" for row in reports),
    "missing_instrument_count": sum(bool(row["missing_sessions"]) for row in reports),
    "missing_session_total": sum(row["missing_session_count"] for row in reports),
    "extra_session_total": sum(row["extra_session_count"] for row in reports),
    "duplicate_session_total": sum(row["duplicate_session_count"] for row in reports),
    "minimum_session_coverage": min(coverage_values) if coverage_values else None,
    "mean_session_coverage": sum(coverage_values) / len(coverage_values) if coverage_values else None,
    "reports": reports,
}

exchange_map = {}
if source_db.is_file():
    connection = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    placeholders = ",".join("?" for _ in dev.instrument_ids)
    for row in connection.execute(f"SELECT symbol, exchange FROM instruments WHERE symbol IN ({placeholders})", dev.instrument_ids):
        exchange_map[row["symbol"].upper()] = build_exchange_mapping(
            row["symbol"], row["exchange"], start_date=dev.start_at.date(), end_date=dev.end_at.date()
        )
    volume = {"volume_null_count": 0, "volume_zero_count": 0, "volume_positive_count": 0, "volume_invalid_count": 0}
    query = f"""SELECT b.volume FROM daily_bars b JOIN instruments i ON i.id=b.instrument_id
                WHERE i.symbol IN ({placeholders}) AND b.trade_date>=? AND b.trade_date<?"""
    for row in connection.execute(query, (*dev.instrument_ids, dev.start_at.date().isoformat(), dev.end_at.date().isoformat())):
        value = row["volume"]
        if value is None:
            volume["volume_null_count"] += 1
        elif float(value) < 0:
            volume["volume_invalid_count"] += 1
        elif float(value) == 0:
            volume["volume_zero_count"] += 1
        else:
            volume["volume_positive_count"] += 1
    connection.close()
    volume_total = sum(volume.values())
    volume["volume_valid_coverage"] = (volume["volume_zero_count"] + volume["volume_positive_count"]) / volume_total if volume_total else 0.0
else:
    volume = {"status": "SOURCE_DB_UNAVAILABLE"}

calendar_summary = Counter(item["calendar_mapping_status"] for item in exchange_map.values())
labels = stock_forward_labels(dev_rows, horizon=5, calendar=calendar)
label_reasons = Counter(reason for row in labels for reason in row.label_quality)
label_summary = {
    "row_count": len(labels),
    "valid_count": sum(row.label_value is not None for row in labels),
    "valid_coverage": sum(row.label_value is not None for row in labels) / len(labels) if labels else 0.0,
    "reason_counts": dict(sorted(label_reasons.items())),
    "label_version": labels[0].label_version if labels else None,
}

test_labels = stock_forward_labels(test_rows, horizon=5, calendar=test_calendar)
start = datetime.fromisoformat("2015-01-15T00:00:00+00:00")
end = datetime.fromisoformat("2017-01-01T00:00:00+00:00")
expected_decisions = sorted({row.feature_decision_at for row in test_labels if start <= row.feature_decision_at < end} | {row["decision_at"] for row in prediction_rows if start <= row["decision_at"] < end})
expected_instruments = sorted(set(test.instrument_ids) | {row["instrument_id"] for row in prediction_rows if start <= row["decision_at"] < end})
oos = attribute_evaluation_coverage(prediction_rows, test_labels, expected_instruments=expected_instruments, expected_decisions=expected_decisions).summary
diagnostics = diagnose_alpha_model(model_id, registry=AlphaModelRegistry(archive / "registry.models"))
after_hashes = {name: file_hash(archive / name) for name in protected_names}
history_unchanged = before_hashes == after_hashes

manifest_hashes = {}
for name, manifest in (("development", dev), ("test", test)):
    manifest_hashes[name] = {"dataset_id": manifest.dataset_id, "content_hash": manifest.content_hash}
readiness = (
    len(reports) == 87
    and len(exchange_map) == 87
    and diagnostics.get("model_id") == model_id
    and oos.get("conservation_holds") is True
    and history_unchanged
)
result = {
    "schema_version": "alpha-data-validity-acceptance-v1",
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "archive": str(archive),
    "session_coverage_summary": session_summary,
    "exchange_mapping_summary": dict(sorted(calendar_summary.items())),
    "volume_summary": volume,
    "session_label_summary": label_summary,
    "ic_and_oos_summary": oos,
    "model_diagnostics": diagnostics,
    "dataset_identity": manifest_hashes,
    "history_immutability": {"unchanged": history_unchanged, "before": before_hashes, "after": after_hashes},
    "DATA_QUALITY_ENGINEERING": "PASS" if len(reports) == 87 and volume.get("volume_null_count") is not None else "FAIL",
    "RESEARCH_DIAGNOSTICS_READINESS": "PASS" if readiness else "FAIL",
    "EXECUTION_ENGINEERING": "FIXTURE_TESTED",
    "REAL_MARKET_TRADING_VALIDITY": "BLOCKED",
    "real_market_blockers": [
        "Independent raw OHLC attestation for the archived 87-symbol history is not available",
        "Historical corporate-action coverage for the archived symbols is not available",
        "Current-survivor universe bias and trade-time realizability remain unverified",
    ],
}
print(json.dumps(result, allow_nan=False))
`;

const result = JSON.parse(execFileSync(python, ["-"], {
  cwd: root,
  env: {
    ...process.env,
    ALPHA_VALIDITY_ARCHIVE: archive,
    ALPHA_SOURCE_DB: sourceDb,
    PYTHONPATH: resolve(root, "quant/src"),
  },
  input: script,
  maxBuffer: 16 * 1024 * 1024,
}).toString());
writeFileSync(output, JSON.stringify(result, null, 2));
console.log(`Alpha data validity: ${result.DATA_QUALITY_ENGINEERING} / ${result.RESEARCH_DIAGNOSTICS_READINESS}; report ${output}`);
