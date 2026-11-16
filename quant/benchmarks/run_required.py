"""Execute every category in manifest_v1.json; return JUnit-derived JSON metrics."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from time import perf_counter
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]


def run():
    manifest = json.loads(Path(__file__).with_name("manifest_v1.json").read_text())
    results = []
    with tempfile.TemporaryDirectory(prefix="quant-required-") as tmp:
        for case in manifest["benchmarks"]:
            xml = Path(tmp) / (case["category"] + ".xml")
            start = perf_counter()
            completed = subprocess.run([sys.executable, "-m", "pytest", *case["pytest_node_ids"], "-q", f"--junitxml={xml}"], cwd=ROOT, capture_output=True, text=True)
            elapsed = perf_counter() - start
            suites = ET.parse(xml).getroot().findall(".//testsuite") if xml.exists() else []
            failed = sum(int(s.get("failures", "0")) + int(s.get("errors", "0")) for s in suites)
            skipped = sum(int(s.get("skipped", "0")) for s in suites)
            count = sum(int(s.get("tests", "0")) for s in suites)
            row = {"id": case["id"], "category": case["category"], "status": "PASS" if completed.returncode == 0 and count and not failed and not skipped else "FAIL",
                   "test_count": count, "failed": failed, "skipped": skipped, "runtime_seconds": elapsed,
                   "snapshot_hash": case["snapshot_hash"], "plan_hashes": case["plan_hashes"], "random_seed": case["random_seed"]}
            if row["status"] == "FAIL":
                row["output"] = (completed.stdout + completed.stderr)[-12000:]
            results.append(row)
    return {"schema_version": "phase2-required-benchmark-metrics-v1", "status": "PASS" if all(r["status"] == "PASS" for r in results) else "FAIL", "benchmarks": results}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run()
    encoded = json.dumps(result, indent=2, allow_nan=False) + "\n"
    if args.output: args.output.write_text(encoded)
    else: print(encoded, end="")
    raise SystemExit(0 if result["status"] == "PASS" else 1)
