"""Build the acceptance artifact set from the completed 99-symbol run."""
from __future__ import annotations

import hashlib, json, subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPORTS = ROOT / "reports/alpha-reliability"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def main() -> None:
    summary = json.loads((REPORTS / "acceptance-summary.json").read_text())
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    dataset_hash = summary["dataset_hash"]
    protocol_hash = summary["protocol_hash"]
    common = {
        "commit_sha": commit,
        "research_protocol_hash": protocol_hash,
        "dataset_hashes": [dataset_hash],
        "factor_spec_hashes": summary["factor_spec_hashes"],
        "tool_catalog_hash": sha256_file(ROOT / "package.json"),
        "status": summary["status"],
        "job_ids": summary.get("job_ids", []),
        "execution_attempts": [{item["job_id"]: item["attempts"]} for item in summary.get("job_ids", [])],
        "research_parameters": summary.get("research_parameters", {}),
    }
    (REPORTS / "factor-regression.json").write_text(json.dumps({
        **common,
        "feature_set_ids": summary["feature_set_ids"],
        "verification_commands": ["ALPHA_RESEARCH_REGISTRY_ROOT=/home/spud/service/yahoo-stock-mcp/quant-runtime/registry.alpha npm run test:quant-alpha-research"],
    }, indent=2) + "\n")
    (REPORTS / "validation-only-results.json").write_text(json.dumps({
        **common,
        "models": summary["diagnostics"],
    }, indent=2) + "\n")
    (REPORTS / "model-selection-ledger.json").write_text(json.dumps({
        **common,
        "candidate_ledger_hash": summary["diagnostics"],
        "selected_model_id": summary["selected_model_id"],
    }, indent=2) + "\n")
    freeze_id = summary["freeze_id"]
    freeze_path = REPORTS / "freezes" / (freeze_id.removeprefix("sha256:") + ".json")
    (REPORTS / "freeze-manifest.json").write_text(json.dumps({
        **common,
        "freeze_id": freeze_id,
        "freeze_record": json.loads(freeze_path.read_text()),
    }, indent=2) + "\n")
    (REPORTS / "oos-evaluation.json").write_text(json.dumps({
        **common,
        "freeze_id": freeze_id,
        "oos_summary": summary["oos_summary"],
    }, indent=2) + "\n")
    dataset_manifest = json.loads((Path('/home/spud/service/yahoo-stock-mcp/quant-runtime/registry.alpha') / 'datasets' / dataset_hash.removeprefix('sha256:') / 'manifest.json').read_text())
    (REPORTS / "quality-diagnostics.json").write_text(json.dumps({
        **common,
        "dataset_id": dataset_hash,
        "row_count": dataset_manifest["data_quality"]["row_count"],
        "observed_symbols": dataset_manifest["data_quality"]["observed_symbols"],
        "calendar_version": dataset_manifest["calendar_version"],
        "price_basis": dataset_manifest["data_quality"]["price_basis"],
        "missing_ohlcv_rows": dataset_manifest["data_quality"]["missing_ohlcv_rows"],
    }, indent=2) + "\n")


if __name__ == "__main__":
    main()
