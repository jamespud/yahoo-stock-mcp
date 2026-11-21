from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
EVIDENCE = ROOT / "docs/evidence/alpha-diagnostics-v1-3.json"
CHECKSUM = ROOT / "docs/evidence/alpha-diagnostics-v1-3.sha256"
SCRIPT = ROOT / "scripts/quant-eval/run-alpha-diagnostics-acceptance.mjs"
PYTHON = os.environ.get("QUANT_PYTHON", sys.executable)
SOURCE_PROTOCOL_HASH = "sha256:eb5dfc8902172c11fd5f9ff17e76a134fc264e9faa2edef84da56231d846a45d"


def _replace(value, path: tuple[object, ...], replacement):
    current = value
    for key in path[:-1]:
        current = current[key]
    current[path[-1]] = replacement
    return value


def _run_check(*args: str) -> subprocess.CompletedProcess[str]:
    return _run_script("--check-evidence", *args)


def _run_script(*args: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT / "quant/src"),
        "QUANT_PYTHON": str(PYTHON),
    }
    return subprocess.run(
        ["node", str(SCRIPT), *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_protocol_embeds_the_frozen_50_symbol_identity_without_external_paths():
    from stock_quant.alpha.models.diagnostic_protocol import (
        PROTOCOL_HASH,
        SOURCE_SYMBOLS,
        frozen_protocol,
    )

    protocol = frozen_protocol()
    source = protocol["source_identity"]

    assert PROTOCOL_HASH.startswith("sha256:")
    assert PROTOCOL_HASH != SOURCE_PROTOCOL_HASH
    assert source["protocol_hash"] == SOURCE_PROTOCOL_HASH
    assert source["symbol_count"] == 50
    assert source["symbols"] == list(SOURCE_SYMBOLS)
    assert len(SOURCE_SYMBOLS) == len(set(SOURCE_SYMBOLS)) == 50
    assert SOURCE_SYMBOLS[0] == "AAPL"
    assert SOURCE_SYMBOLS[-1] == "ORLY"
    assert "legacy" in source["universe_version_note"].lower()
    assert protocol["selection"]["validation_subperiods"] == [
        {
            "start": "2008-01-14T00:00:00Z",
            "end": "2010-01-01T00:00:00Z",
            "min_rank_ic": 0.0,
        },
        {
            "start": "2010-01-01T00:00:00Z",
            "end": "2012-01-01T00:00:00Z",
            "min_rank_ic": 0.0,
        },
    ]
    assert "/home/spud" not in json.dumps(protocol, sort_keys=True)


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("source_identity", "symbols", -1), "ZZZZ"),
        (("windows", "validation", "end"), "2012-01-02T00:00:00Z"),
        (("signals", 0, "score_multiplier"), -1),
        (("candidates", 0, "configs", 0, "num_leaves"), 8),
        (("calendar", "policy_version"), "mapped-stock-calendar-v2"),
        (("selection", "metric_versions", "daily_rank_ic"), "daily-rank-ic-v2"),
        (("selection", "validation_subperiods", 0, "start"), "2008-01-15T00:00:00Z"),
        (("selection", "validation_subperiods", 0, "end"), "2010-01-02T00:00:00Z"),
        (("selection", "validation_subperiods", 0, "min_rank_ic"), 0.01),
        (("selection", "validation_subperiods", 1, "start"), "2010-01-02T00:00:00Z"),
        (("selection", "validation_subperiods", 1, "end"), "2011-12-31T00:00:00Z"),
        (("selection", "validation_subperiods", 1, "min_rank_ic"), 0.01),
        (("bootstrap", "draws"), 1999),
        (("budgets", "training", "max_trainings"), 11),
    ],
)
def test_protocol_hash_changes_for_every_frozen_input(path, replacement):
    from stock_quant.alpha.models.diagnostic_protocol import frozen_protocol, protocol_hash

    baseline = frozen_protocol()
    changed = _replace(copy.deepcopy(baseline), path, replacement)

    assert protocol_hash(baseline) == protocol_hash(copy.deepcopy(baseline))
    assert protocol_hash(changed) != protocol_hash(baseline)


def test_reserved_test_admission_raises_before_any_synthetic_object_is_created():
    from stock_quant.alpha.models.diagnostic_protocol import assert_development_window

    created: list[str] = []

    with pytest.raises(ValueError, match="reserved Test"):
        assert_development_window(
            datetime(2012, 1, 1, tzinfo=timezone.utc),
            datetime(2012, 2, 1, tzinfo=timezone.utc),
            on_admit=lambda: created.append("feature_label_prediction_metric"),
        )
    with pytest.raises(ValueError, match="reserved Test"):
        assert_development_window(
            datetime(2011, 12, 1, tzinfo=timezone.utc),
            datetime(2012, 1, 2, tzinfo=timezone.utc),
            on_admit=lambda: created.append("feature_label_prediction_metric"),
        )

    assert created == []
    assert_development_window(
        datetime(2009, 1, 2, tzinfo=timezone.utc),
        datetime(2009, 4, 1, tzinfo=timezone.utc),
        on_admit=lambda: created.append("synthetic_fixture"),
    )
    assert created == ["synthetic_fixture"]


def test_committed_evidence_is_sanitized_and_checksum_verified():
    from stock_quant.alpha.models.diagnostic_protocol import validate_diagnostic_evidence

    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    validate_diagnostic_evidence(evidence)
    checksum_line = CHECKSUM.read_text(encoding="ascii").strip().split()

    assert checksum_line == [
        hashlib.sha256(EVIDENCE.read_bytes()).hexdigest(),
        EVIDENCE.name,
    ]
    serialized = json.dumps(evidence, sort_keys=True, allow_nan=False)
    assert "/home/spud" not in serialized
    assert "price_history" not in serialized
    assert "raw_bars" not in serialized
    assert evidence["statuses"]["REAL_MARKET_TRADING_VALIDITY"] == "BLOCKED"
    assert evidence["statuses"]["RESEARCH_DIAGNOSTIC_READINESS"] == "NOT_TESTED"
    assert evidence["statuses"]["SYNTHETIC_CONTRACT_VALIDATION"] == "PASS"
    assert evidence["real_study"]["test_oos_executions"] == 0
    assert evidence["real_study"]["candidate_promotions"] == 0
    assert evidence["full_npm_test"]["status"] == "FAILED"
    assert evidence["full_npm_test"]["failure_count"] == 11
    assert evidence["full_npm_test"]["failure_labels_match_saved_baseline"] is True
    assert evidence["full_npm_test"]["byte_identical_logs"] is False
    assert "not green" in evidence["full_npm_test"]["comparison_scope"].lower()


def test_evidence_validator_rejects_a_real_market_or_promotion_claim():
    from stock_quant.alpha.models.diagnostic_protocol import validate_diagnostic_evidence

    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    inflated = copy.deepcopy(evidence)
    inflated["statuses"]["REAL_MARKET_TRADING_VALIDITY"] = "PASS"
    promoted = copy.deepcopy(evidence)
    promoted["real_study"]["candidate_promotions"] = 1

    with pytest.raises(ValueError, match="REAL_MARKET_TRADING_VALIDITY"):
        validate_diagnostic_evidence(inflated)
    with pytest.raises(ValueError, match="candidate promotion"):
        validate_diagnostic_evidence(promoted)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value["protocol"]["signals"][0].update({"score_multiplier": -1}),
        lambda value: value["protocol"]["source_identity"]["symbols"].__setitem__(-1, "ZZZZ"),
        lambda value: value["protocol"]["bootstrap"].update({"draws": 1999}),
    ],
)
def test_evidence_validator_independently_rehashes_and_deep_compares_protocol_body(mutation):
    from stock_quant.alpha.models.diagnostic_protocol import validate_diagnostic_evidence

    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    retained_hash = evidence["protocol"]["protocol_hash"]
    mutation(evidence)
    assert evidence["protocol"]["protocol_hash"] == retained_hash

    with pytest.raises(ValueError, match="(?i)protocol body|protocol hash"):
        validate_diagnostic_evidence(evidence)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value["full_npm_test"].update({"status": "NOT_TESTED"}),
        lambda value: value["full_npm_test"].update({"failure_count": 10}),
        lambda value: value["full_npm_test"]["failure_labels"].pop(),
        lambda value: value["full_npm_test"].update({"failure_labels_match_saved_baseline": False}),
        lambda value: value["full_npm_test"].update({"byte_identical_logs": True}),
    ],
)
def test_evidence_validator_requires_truthful_non_green_npm_test_disclosure(mutation):
    from stock_quant.alpha.models.diagnostic_protocol import validate_diagnostic_evidence

    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    mutation(evidence)

    with pytest.raises(ValueError, match="npm test"):
        validate_diagnostic_evidence(evidence)


def _write_tampered_evidence(tmp_path: Path, mutation) -> Path:
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    mutation(evidence)
    evidence_path = tmp_path / "alpha-diagnostics-v1-3.json"
    bytes_value = (json.dumps(evidence, indent=2, sort_keys=True) + "\n").encode()
    evidence_path.write_bytes(bytes_value)
    checksum = tmp_path / "alpha-diagnostics-v1-3.sha256"
    checksum.write_text(
        f"{hashlib.sha256(bytes_value).hexdigest()}  {evidence_path.name}\n",
        encoding="ascii",
    )
    return evidence_path


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value["protocol"]["signals"][0].update({"score_multiplier": -1}),
        lambda value: value["full_npm_test"].update({"status": "NOT_TESTED"}),
    ],
)
def test_node_checker_rejects_tampered_protocol_or_npm_disclosure(tmp_path, mutation):
    evidence_path = _write_tampered_evidence(tmp_path, mutation)
    completed = _run_check("--evidence-path", str(evidence_path))

    assert completed.returncode != 0
    assert "protocol body" in completed.stderr.lower() or "npm test" in completed.stderr.lower()


@pytest.mark.parametrize("with_command_results", [False, True])
def test_evidence_regeneration_rejects_missing_or_not_tested_npm_results(tmp_path, with_command_results):
    target = tmp_path / "target-evidence.json"
    target.write_text("sentinel\n", encoding="ascii")
    checksum = tmp_path / "target-evidence.sha256"
    checksum.write_text("sentinel checksum\n", encoding="ascii")
    args = [
        "--write-evidence",
        "--replace-evidence",
        "--evidence-path",
        str(target),
    ]
    if with_command_results:
        command_results = tmp_path / "command-results.json"
        command_results.write_text(
            json.dumps({
                "commands": [],
                "full_npm_test": {"status": "NOT_TESTED"},
                "baseline_comparison": None,
            }),
            encoding="utf-8",
        )
        args.extend(["--command-results", str(command_results)])

    completed = _run_script(*args)

    assert completed.returncode != 0
    assert target.read_text(encoding="ascii") == "sentinel\n"
    assert checksum.read_text(encoding="ascii") == "sentinel checksum\n"
    assert "npm test" in completed.stderr.lower()


def test_quant_workflow_paths_include_evidence_changes():
    workflow = (ROOT / ".github/workflows/quant-regression.yml").read_text(encoding="utf-8")
    push, pull_request = workflow.split("  pull_request:\n", 1)

    assert "      - 'docs/evidence/**'\n" in push
    assert "      - 'docs/evidence/**'\n" in pull_request


def test_acceptance_runner_emits_b1_to_b5_and_validates_the_attachment(tmp_path):
    report_path = tmp_path / "synthetic-report.json"
    completed = _run_check("--synthetic-report", str(report_path))

    assert completed.returncode == 0, completed.stderr + completed.stdout
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["scope"] == "RESEARCH_DIAGNOSTIC_READINESS_SYNTHETIC_CONTRACT_VALIDATION"
    assert set(report["sections"]) == {"B1", "B2", "B3", "B4", "B5"}
    assert all(section["status"] == "PASS" for section in report["sections"].values())
    assert report["sections"]["B1"]["signals"] == ["D01", "D02"]
    assert report["sections"]["B2"]["weekly_actual_session_rule"] is True
    assert report["sections"]["B3"]["analysis_label"] == (
        "predictive_label_analysis_not_executable_backtest"
    )
    assert report["sections"]["B4"]["equal_candidate_budget"] is True
    assert report["sections"]["B4"]["candidate_iteration_count"] == 4
    assert report["sections"]["B4"]["same_tree_path"] is True
    assert report["sections"]["B4"]["validation_only"] is True
    assert report["sections"]["B5"]["bootstrap"]["config"]["draws"] == 2000
    assert report["real_market_trading_validity"] == "BLOCKED"
    assert report["candidate_promotions"] == 0
    assert report["test_oos_executions"] == 0
    assert report["reserved_test_reads"] == 0
