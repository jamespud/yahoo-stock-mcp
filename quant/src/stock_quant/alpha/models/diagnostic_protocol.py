"""Frozen Alpha v1.3 diagnostics protocol and claim-boundary validation."""
from __future__ import annotations

import hashlib
import json
import subprocess
from copy import deepcopy
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Callable, Literal

import numpy as np

from ..errors import AlphaError, AlphaErrorCode
from .evaluation import metrics

SOURCE_PROTOCOL_HASH = "sha256:eb5dfc8902172c11fd5f9ff17e76a134fc264e9faa2edef84da56231d846a45d"
NPM_TEST_BASELINE_COMMIT = "5b4ece411348620e9b73ce4e9508b59ff5f42770"
NPM_TEST_FAILED_SUITE = "test:sqlite-sync-e2e"
EXPECTED_NPM_TEST_FAILURE_LABELS = (
    "FAIL  1. syncOne --full writes the fixture data into the target tables",
    "FAIL  2. repeated full sync is idempotent (row counts stable, no duplicates)",
    "FAIL  3. incremental sync updates the bar set and sync_state without duplicating",
    "FAIL  4. options snapshot replacement is atomic: a bad row rolls the whole snapshot back",
    "FAIL  5. a failing provider endpoint yields partial results without losing good data",
    "FAIL  6. syncAll walks a non-empty instrument list and syncs each symbol",
    "FAIL  7. syncSectors writes the ETF and sector rows for a seeded sector",
    "FAIL  8. a NEW process reopens the database and continues incrementally",
    "FAIL  10. provider priority: the primary wins, the fallback only fills NULL, and repeats are idempotent",
    "FAIL  11. syncSectors({members:true}) writes real sector_members rows with DECIMAL weights",
    "FAIL  13. a failing Investing response yields partial without losing the Yahoo data",
)
PROTOCOL_DERIVED_FIELDS = frozenset({"protocol_hash", "source_protocol_hash", "symbol_count", "symbols"})
SOURCE_CONTENT_HASH_ALGORITHM = "tracked-source-files-v1"
SOURCE_CONTENT_EXCLUDED_PREFIXES = (
    "docs/evidence/",
    "docs/superpowers/",
    ".superpowers/",
    "reports/",
)
ProtocolMode = Literal["synthetic_contract", "frozen_v1_3"]
RESERVED_TEST_START = datetime(2012, 1, 1, tzinfo=timezone.utc)
RESERVED_TEST_END = datetime(2015, 1, 1, tzinfo=timezone.utc)
SYNTHETIC_START = datetime(2009, 1, 5, tzinfo=timezone.utc)
SYNTHETIC_END = datetime(2009, 12, 31, tzinfo=timezone.utc)

SOURCE_SYMBOLS = (
    "AAPL", "NVDA", "MSFT", "AMZN", "AMD", "MU", "WMT", "COST", "CSCO", "INTC",
    "AMAT", "LLY", "LRCX", "TXN", "BRK-B", "AMGN", "LIN", "JPM", "GILD", "KLAC",
    "PEP", "XOM", "ADI", "QCOM", "JNJ", "VRTX", "SBUX", "ADP", "BKNG", "CVX",
    "WDC", "MAR", "ASML", "CSX", "REGN", "ADBE", "PG", "KO", "MRK", "UNH",
    "CMCSA", "INTU", "CTAS", "CDNS", "SNPS", "MNST", "BAC", "HD", "GE", "ORLY",
)

_CONFIGS = [
    {
        "id": "C1",
        "num_leaves": 7,
        "max_depth": 3,
        "learning_rate": 0.03,
        "n_estimators": 150,
        "min_child_samples": 100,
        "random_seed": 20261120,
    },
    {
        "id": "C2",
        "num_leaves": 15,
        "max_depth": 4,
        "learning_rate": 0.03,
        "n_estimators": 150,
        "min_child_samples": 100,
        "random_seed": 20261120,
    },
]

_HYPOTHESES = [
    ("H01", ["mom63", "mom126", "skip5mom"], 127),
    ("H02", ["ret1", "ret5", "rsi5"], 7),
    ("H03", ["vol20", "vol60"], 62),
    ("H04", ["ret1", "volume_ratio", "price_volume"], 21),
    ("H05", ["mom63", "vol20", "trend_vol", "interaction"], 64),
    ("H06", ["ret1", "ret5", "mom63", "mom126", "vol20", "volume_ratio", "trend_vol"], 127),
]

_FROZEN_PROTOCOL = {
    "schema_version": "alpha-diagnostic-protocol-v1",
    "protocol_version": "alpha-diagnostics-v1.3",
    "frozen_at": "2026-11-20T00:00:00Z",
    "source_identity": {
        "kind": "alpha-v1.2-frozen-protocol",
        "protocol_hash": SOURCE_PROTOCOL_HASH,
        "protocol_name": "us-alpha-v1.2-walkforward-2002-2015-v4",
        "universe_version_label": "voo-qqq-top80-pre2000-current-weights-v1",
        "universe_version_note": (
            "The v1.2 universe_version label is legacy; the authoritative frozen membership is 50 symbols."
        ),
        "symbol_count": 50,
        "symbols": list(SOURCE_SYMBOLS),
    },
    "windows": {
        "dataset": {"start": "2002-01-01T00:00:00Z", "end": "2015-01-01T00:00:00Z"},
        "train": {"start": "2003-01-01T00:00:00Z", "end": "2008-01-01T00:00:00Z"},
        "validation": {"start": "2008-01-14T00:00:00Z", "end": "2012-01-01T00:00:00Z"},
        "source_test": {"start": "2012-01-15T00:00:00Z", "end": "2015-01-01T00:00:00Z"},
        "reserved_test": {"start": "2012-01-01T00:00:00Z", "end": "2015-01-01T00:00:00Z"},
        "development_end": "2012-01-01T00:00:00Z",
        "embargo_days": 14,
        "interval_semantics": "left-closed-right-open",
        "timezone": "UTC",
    },
    "signals": [
        {"id": "D01", "factor_name": "mom63", "score_multiplier": 1, "version": "direct-factor-v1"},
        {"id": "D02", "factor_name": "ret5", "score_multiplier": -1, "version": "direct-factor-v1"},
    ],
    "candidates": [
        {
            "id": hypothesis_id,
            "model_version": "validation-only-lightgbm-v1",
            "factor_outputs": outputs,
            "warmup_observations": warmup,
            "configs": deepcopy(_CONFIGS),
            "promotion_allowed": False,
        }
        for hypothesis_id, outputs, warmup in _HYPOTHESES
    ],
    "selection": {
        "version": "frozen-validation-selection-v1.2",
        "metric_versions": {
            "l2": "validation-l2-rmse-v1",
            "daily_rank_ic": "validation-daily-rank-ic-v1",
            "weekly_rank_ic": "validation-weekly-rank-ic-v1",
        },
        "min_validation_rank_ic": 0.03,
        "min_prediction_coverage": 0.8,
        "min_validation_icir": 0.0,
        "validation_subperiods": [
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
        ],
        "tie_rule": "first_iteration_on_exact_tie",
        "max_finalists": 1,
        "selection_scope": "validation_only",
        "candidate_promotion": "FORBIDDEN_IN_DIAGNOSTICS",
    },
    "calendar": {
        "policy_version": "mapped-stock-calendar-v1",
        "label_version": "session-aligned-v2",
        "decision_frequency": "1d",
        "weekly_decision_rule": "last_actual_scheduled_session_per_iso_week",
        "entry_rule": "next_eligible_session_open",
        "exit_rule": "five_eligible_sessions_after_entry",
        "boundary_spillover": "FORBIDDEN",
        "historical_bounds_policy": "UNRESOLVED_PR_A_A3",
        "historical_calendar_gate": "BLOCKED",
    },
    "bootstrap": {
        "version": "weekly-moving-block-bootstrap-v1",
        "block_weeks": 4,
        "draws": 2000,
        "seed": 20261120,
        "rng": "PCG64",
        "interval": 0.95,
        "percentile_method": "linear",
        "minimum_weeks": 8,
        "minimum_valid_ics": 8,
        "minimum_finite_draws": 1900,
    },
    "environment": {
        "quant_package": "yahoo-stock-quant==0.1.0",
        "python": {"requires": ">=3.12", "ci": "3.12", "scheduled_compatibility": "3.14"},
        "node": "22",
        "dependencies": {
            "lightgbm": ">=4.5,<5",
            "numpy": ">=2.3,<3",
            "pandas": ">=2.2,<4",
            "pydantic": ">=2.12,<3",
            "scipy": ">=1.13,<2",
        },
        "deterministic_execution": {
            "lightgbm_num_threads": 1,
            "lightgbm_deterministic": True,
            "lightgbm_force_col_wise": True,
            "bootstrap_rng": "PCG64",
        },
    },
    "budgets": {
        "training": {
            "max_hypotheses": 6,
            "max_configs_each": 2,
            "max_trainings": 12,
            "planned_trainings": 12,
            "count_failed_executions": True,
        },
        "diagnostics": {
            "direct_signal_count": 2,
            "top20_group_size": 20,
            "max_result_page_rows": 1000,
            "serialized_page_rows_bytes": 2500000,
            "mcp_frame_bytes": 4194304,
        },
        "execution": {
            "training": {"max_seconds": 600, "max_rows": 2_000_000, "max_iterations": 150},
            "folds": {"max_seconds": 600, "max_rows": 1_000_000, "max_iterations": 150},
        },
    },
    "data_gates": {
        "min_observed_symbols": 50,
        "max_missing_symbols": 0,
        "require_session_coverage_report": True,
        "max_unexpected_session_anomalies": 0,
        "max_nonfinite_features": 0,
        "max_unexpected_factor_missing": 0,
        "volume_missingness_must_be_reported": True,
    },
    "claim_boundaries": [
        "Yahoo price basis remains unverified",
        "corporate actions are incomplete",
        "historical revisions and PIT membership unavailable",
        "predictive label groups are not executable backtests",
        "no candidate promotion from diagnostic output",
        "REAL_MARKET_TRADING_VALIDITY remains BLOCKED",
    ],
}


def frozen_protocol() -> dict:
    """Return a detached copy of the canonical configurable protocol content."""
    return deepcopy(_FROZEN_PROTOCOL)


def _canonical_protocol_value(value):
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {key: _canonical_protocol_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_canonical_protocol_value(item) for item in value]
    return value


def protocol_canonical_json(protocol: dict | None = None) -> str:
    value = _FROZEN_PROTOCOL if protocol is None else protocol
    return json.dumps(
        _canonical_protocol_value(value),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def protocol_hash(protocol: dict) -> str:
    return "sha256:" + hashlib.sha256(protocol_canonical_json(protocol).encode()).hexdigest()


PROTOCOL_HASH = protocol_hash(_FROZEN_PROTOCOL)


def protocol_body_from_evidence(protocol: dict) -> dict:
    """Strip evidence-only derived fields without trusting their stored values."""
    if not isinstance(protocol, dict):
        raise ValueError("Protocol evidence must be an object")
    return {key: deepcopy(value) for key, value in protocol.items() if key not in PROTOCOL_DERIVED_FIELDS}


def protocol_claim(mode: str) -> dict:
    if mode not in {"synthetic_contract", "frozen_v1_3"}:
        raise ValueError("Unknown diagnostic protocol mode")
    return {
        "protocol_mode": mode,
        "v1_3_frozen": mode == "frozen_v1_3",
        "claim_status": "FROZEN_V1_3" if mode == "frozen_v1_3" else "NON_V1_3_SYNTHETIC_CONTRACT",
    }


def _mapping(value, *, name: str) -> dict:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return value
    raise ValueError(f"{name} must be an object")


def _signal_matches(signal: dict, expected: dict) -> bool:
    return all(signal.get(key) == expected[key] for key in ("factor_name", "score_multiplier", "version"))


def candidate_definition(candidate_id: str) -> dict:
    candidate = next((item for item in _FROZEN_PROTOCOL["candidates"] if item["id"] == candidate_id), None)
    if candidate is None:
        raise ValueError("candidate is not in the frozen protocol")
    return candidate


def _frozen_training_config(candidate_id: str, hyperparameters: dict, random_seed: int) -> dict:
    candidate = candidate_definition(candidate_id)
    normalized = dict(hyperparameters)
    for config in candidate["configs"]:
        expected = {key: value for key, value in config.items() if key not in {"id", "random_seed"}}
        if normalized == expected and random_seed == config["random_seed"]:
            return config
    raise ValueError("candidate configuration does not match an approved frozen configuration")


def validate_frozen_training_binding(
    *,
    protocol_hash_value: str,
    split,
    candidate_id: str | None,
    hyperparameters: dict,
    random_seed: int,
    resource_budget,
    baseline_signal,
    min_rank_ic: float,
) -> dict:
    from .signal_diagnostics import DirectSignalSpec

    if protocol_hash_value != PROTOCOL_HASH:
        raise ValueError("frozen_v1_3 requires the exact protocol hash")
    split_payload = _mapping(split, name="split")
    expected_windows = _FROZEN_PROTOCOL["windows"]
    expected_split = {
        "train_start": expected_windows["train"]["start"],
        "train_end": expected_windows["train"]["end"],
        "validation_start": expected_windows["validation"]["start"],
        "validation_end": expected_windows["validation"]["end"],
        "embargo_days": expected_windows["embargo_days"],
    }
    if split_payload != expected_split:
        raise ValueError("frozen_v1_3 window or embargo does not match the protocol")
    if not candidate_id:
        raise ValueError("frozen_v1_3 requires a candidate id")
    config = _frozen_training_config(candidate_id, hyperparameters, random_seed)
    budget_payload = _mapping(resource_budget, name="resource budget")
    expected_budget = _FROZEN_PROTOCOL["budgets"]["execution"]["training"]
    if budget_payload != {
        "max_seconds": expected_budget["max_seconds"],
        "max_rows": expected_budget["max_rows"],
    }:
        raise ValueError("frozen_v1_3 budget does not match the protocol")
    if min_rank_ic != _FROZEN_PROTOCOL["selection"]["min_validation_rank_ic"]:
        raise ValueError("frozen_v1_3 gate thresholds cannot be overridden")
    if baseline_signal is None:
        raise ValueError("frozen_v1_3 requires an approved signal identity")
    signal = DirectSignalSpec.model_validate(baseline_signal) if isinstance(baseline_signal, dict) else baseline_signal
    if not any(_signal_matches(signal.model_dump(mode="json"), expected) for expected in _FROZEN_PROTOCOL["signals"]):
        raise ValueError("frozen_v1_3 signal identity or direction is not approved")
    return {"candidate": candidate_definition(candidate_id), "config": config, "signal": signal.model_dump(mode="json")}


def validate_frozen_fold_binding(
    *,
    protocol_hash_value: str,
    split,
    candidate_id: str | None,
    hyperparameters: dict,
    random_seed: int,
    resource_budget,
) -> dict:
    if protocol_hash_value != PROTOCOL_HASH:
        raise ValueError("frozen_v1_3 requires the exact protocol hash")
    split_payload = _mapping(split, name="split")
    expected_windows = _FROZEN_PROTOCOL["windows"]
    expected_split = {
        "train_start": expected_windows["train"]["start"],
        "train_end": expected_windows["train"]["end"],
        "validation_start": expected_windows["validation"]["start"],
        "validation_end": expected_windows["validation"]["end"],
        "embargo_days": expected_windows["embargo_days"],
    }
    if split_payload != expected_split:
        raise ValueError("frozen_v1_3 window or embargo does not match the protocol")
    if not candidate_id:
        raise ValueError("frozen_v1_3 requires a candidate id")
    config = _frozen_training_config(candidate_id, hyperparameters, random_seed)
    budget_payload = _mapping(resource_budget, name="resource budget")
    expected_budget = _FROZEN_PROTOCOL["budgets"]["execution"]["folds"]
    if budget_payload != {
        "max_seconds": expected_budget["max_seconds"],
        "max_rows": expected_budget["max_rows"],
    }:
        raise ValueError("frozen_v1_3 fold budget does not match the protocol")
    return {"candidate": candidate_definition(candidate_id), "config": config}


def validate_frozen_signal_binding(*, protocol_hash_value: str, split, evaluation_window, signal_spec) -> dict:
    from .signal_diagnostics import DirectSignalSpec

    if protocol_hash_value != PROTOCOL_HASH:
        raise ValueError("frozen_v1_3 requires the exact protocol hash")
    expected_windows = _FROZEN_PROTOCOL["windows"]
    expected_split = {
        "train_start": expected_windows["train"]["start"],
        "train_end": expected_windows["train"]["end"],
        "validation_start": expected_windows["validation"]["start"],
        "validation_end": expected_windows["validation"]["end"],
        "embargo_days": expected_windows["embargo_days"],
    }
    if _mapping(split, name="split") != expected_split:
        raise ValueError("frozen_v1_3 window or embargo does not match the protocol")
    expected_window = {
        "start": expected_windows["validation"]["start"],
        "end": expected_windows["validation"]["end"],
    }
    if _mapping(evaluation_window, name="evaluation window") != expected_window:
        raise ValueError("frozen_v1_3 evaluation window does not match the protocol")
    spec = DirectSignalSpec.model_validate(signal_spec) if isinstance(signal_spec, dict) else signal_spec
    if not any(_signal_matches(spec.model_dump(mode="json"), expected) for expected in _FROZEN_PROTOCOL["signals"]):
        raise ValueError("frozen_v1_3 signal identity or direction is not approved")
    return spec.model_dump(mode="json")


def validate_frozen_membership(instrument_ids: list[str]) -> None:
    normalized = tuple(str(value).upper() for value in instrument_ids)
    if len(normalized) != len(SOURCE_SYMBOLS) or set(normalized) != set(SOURCE_SYMBOLS):
        raise ValueError("frozen_v1_3 membership does not match the exact 50-symbol universe")


def validate_frozen_label_contract(label_rows: list) -> None:
    if not label_rows:
        raise AlphaError(AlphaErrorCode.DATA_UNAVAILABLE, "frozen_v1_3 requires session-aligned-v2 labels")
    versions = {getattr(row, "label_version", None) for row in label_rows}
    if versions != {"session-aligned-v2"}:
        raise AlphaError(
            AlphaErrorCode.DATA_UNAVAILABLE,
            "frozen_v1_3 requires session-aligned-v2 labels; observed-record-v1 fallback is forbidden",
        )


def evaluate_frozen_validation_gate(
    records: list[dict],
    *,
    expected_instruments: list[str],
    expected_sessions: list[date | datetime],
) -> dict:
    instruments = sorted({str(value).upper() for value in expected_instruments})
    sessions = sorted({_utc(value).date() for value in expected_sessions})
    if not instruments or not sessions:
        raise ValueError("Frozen Validation gate requires expected instruments and sessions")
    expected_opportunities = len(instruments) * len(sessions)

    def summarize(selected: list[dict], decisions: list[date]) -> dict:
        targets = np.asarray([float(row["label_value"]) for row in selected], dtype=float)
        predictions = np.asarray([float(row["prediction"]) for row in selected], dtype=float)
        observed_sessions = [_utc(row["decision_at"]).date() for row in selected]
        result = metrics(targets, predictions, observed_sessions, expected_decisions=decisions)
        sample_count = int(result["sample_count"])
        return {
            **result,
            "sample_count": sample_count,
            "expected_opportunities": len(instruments) * len(decisions),
            "coverage": sample_count / (len(instruments) * len(decisions)) if decisions else 0.0,
        }

    overall = summarize(records, sessions)
    overall["expected_opportunities"] = expected_opportunities
    thresholds = _FROZEN_PROTOCOL["selection"]
    failures: list[str] = []
    if overall["rank_ic_mean"] is None or overall["rank_ic_mean"] < thresholds["min_validation_rank_ic"]:
        failures.append("RANK_IC_BELOW_0.03")
    if overall["coverage"] < thresholds["min_prediction_coverage"]:
        failures.append("COVERAGE_BELOW_0.8")
    if overall["icir"] is None or overall["icir"] < thresholds["min_validation_icir"]:
        failures.append("ICIR_BELOW_0.0")

    subperiods = []
    for index, period in enumerate(thresholds["validation_subperiods"]):
        start = datetime.fromisoformat(period["start"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(period["end"].replace("Z", "+00:00"))
        period_sessions = [session for session in sessions if start.date() <= session < end.date()]
        period_records = [
            row for row in records
            if start.date() <= _utc(row["decision_at"]).date() < end.date()
        ]
        summary = summarize(period_records, period_sessions)
        passed = summary["rank_ic_mean"] is not None and summary["rank_ic_mean"] >= period["min_rank_ic"]
        if not passed:
            failures.append(f"SUBPERIOD_{index + 1}_RANK_IC_BELOW_{period['min_rank_ic']}")
        subperiods.append({
            "start": period["start"],
            "end": period["end"],
            "min_rank_ic": period["min_rank_ic"],
            "passed": passed,
            "rank_ic_mean": summary["rank_ic_mean"],
            "sample_count": summary["sample_count"],
            "expected_opportunities": summary["expected_opportunities"],
        })
    return {
        "status": "PASS" if not failures else "FAIL",
        "passed": not failures,
        "failure_reasons": failures,
        "thresholds": {
            "min_validation_rank_ic": thresholds["min_validation_rank_ic"],
            "min_prediction_coverage": thresholds["min_prediction_coverage"],
            "min_validation_icir": thresholds["min_validation_icir"],
            "validation_subperiods": deepcopy(thresholds["validation_subperiods"]),
        },
        "overall": overall,
        "subperiods": subperiods,
        "coverage_definition": "finite matched predictions / (expected instruments x expected mapped sessions)",
    }


def _utc(value: date | datetime) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return datetime.combine(value, time.min, tzinfo=timezone.utc)


def assert_development_window(
    start: date | datetime,
    end: date | datetime,
    *,
    on_admit: Callable[[], None] | None = None,
) -> None:
    """Fail closed before synthetic or real objects are created in reserved Test."""
    start_at, end_at = _utc(start), _utc(end)
    if start_at >= end_at:
        raise ValueError("Expected a non-empty development window")
    if start_at >= RESERVED_TEST_START or end_at > RESERVED_TEST_START:
        raise ValueError("Diagnostics must not enter the reserved Test interval at or after 2012-01-01T00:00:00Z")
    if on_admit is not None:
        on_admit()


def validate_diagnostic_evidence(evidence: dict) -> None:
    """Reject unsupported readiness, promotion, Test/OOS, or identity claims."""
    statuses = evidence.get("statuses", {})
    if statuses.get("REAL_MARKET_TRADING_VALIDITY") != "BLOCKED":
        raise ValueError("REAL_MARKET_TRADING_VALIDITY must remain BLOCKED")
    if statuses.get("RESEARCH_DIAGNOSTIC_READINESS") != "NOT_TESTED":
        raise ValueError("Real-study RESEARCH_DIAGNOSTIC_READINESS must remain NOT_TESTED")
    if statuses.get("SYNTHETIC_CONTRACT_VALIDATION") != "PASS":
        raise ValueError("Synthetic contract validation must be PASS")
    real_study = evidence.get("real_study", {})
    if real_study.get("status") != "NOT_EXECUTED":
        raise ValueError("Real diagnostics were not authorized and must be NOT_EXECUTED")
    if real_study.get("test_oos_executions") != 0:
        raise ValueError("Test/OOS execution is forbidden")
    if real_study.get("candidate_promotions") != 0:
        raise ValueError("candidate promotion is forbidden")
    protocol = evidence.get("protocol", {})
    body = protocol_body_from_evidence(protocol)
    if body != _FROZEN_PROTOCOL or protocol_canonical_json(body) != protocol_canonical_json(_FROZEN_PROTOCOL):
        raise ValueError("Protocol body does not match the canonical frozen diagnostics protocol")
    if protocol_hash(body) != protocol.get("protocol_hash"):
        raise ValueError("Protocol hash does not independently rehash the embedded protocol body")
    if protocol.get("protocol_hash") != PROTOCOL_HASH:
        raise ValueError("Protocol hash does not match the canonical frozen diagnostics protocol")
    if protocol.get("source_protocol_hash") != SOURCE_PROTOCOL_HASH:
        raise ValueError("Source protocol identity mismatch")
    if protocol.get("symbol_count") != 50 or protocol.get("symbols") != list(SOURCE_SYMBOLS):
        raise ValueError("Frozen 50-symbol identity mismatch")
    sections = evidence.get("synthetic_acceptance", {}).get("sections", {})
    if set(sections) != {"B1", "B2", "B3", "B4", "B5"}:
        raise ValueError("Synthetic acceptance must contain exactly B1 through B5")
    if any(section.get("status") != "PASS" for section in sections.values()):
        raise ValueError("Every synthetic acceptance section must pass")
    npm_test = evidence.get("full_npm_test")
    if not isinstance(npm_test, dict):
        raise ValueError("npm test disclosure is required")
    if npm_test.get("status") != "FAILED" or npm_test.get("exit_code") != 1:
        raise ValueError("npm test disclosure must record a FAILED exit 1")
    if npm_test.get("failed_suite") != NPM_TEST_FAILED_SUITE:
        raise ValueError("npm test disclosure has the wrong failed suite")
    if npm_test.get("failure_count") != len(EXPECTED_NPM_TEST_FAILURE_LABELS):
        raise ValueError("npm test disclosure has the wrong failure count")
    if npm_test.get("failure_labels") != list(EXPECTED_NPM_TEST_FAILURE_LABELS):
        raise ValueError("npm test failure labels do not match the saved baseline")
    if npm_test.get("failure_labels_match_saved_baseline") is not True:
        raise ValueError("npm test baseline comparison must be explicitly true")
    if npm_test.get("baseline_commit") != NPM_TEST_BASELINE_COMMIT:
        raise ValueError("npm test disclosure has the wrong baseline commit")
    if npm_test.get("byte_identical_logs") is not False:
        raise ValueError("npm test disclosure must state that logs are not byte-identical")
    comparison_scope = str(npm_test.get("comparison_scope", "")).lower()
    if "not byte-identical" not in comparison_scope or "not green" not in comparison_scope:
        raise ValueError("npm test disclosure must explicitly remain not green")
    validate_evidence_source_provenance(evidence)


def _git_output(repo_root: Path, *args: str) -> str:
    try:
        return subprocess.check_output(
            ["git", *args],
            cwd=repo_root,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise ValueError("Evidence source commit cannot be verified against Git history") from error


def _source_path_included(path: str) -> bool:
    return not any(path.startswith(prefix) for prefix in SOURCE_CONTENT_EXCLUDED_PREFIXES)


def source_content_hash(repo_root: Path | None = None, *, tracked_paths: list[str] | None = None) -> str:
    root = Path(repo_root) if repo_root is not None else Path(__file__).resolve().parents[5]
    if tracked_paths is None:
        raw = subprocess.check_output(["git", "ls-files", "-z"], cwd=root)
        tracked_paths = [value.decode("utf-8") for value in raw.split(b"\0") if value]
    paths = sorted(
        (path for path in tracked_paths if _source_path_included(path)),
        key=lambda path: path.encode("utf-8"),
    )
    digest_value = hashlib.sha256()
    for path in paths:
        digest_value.update(path.encode("utf-8"))
        digest_value.update(b"\0")
        digest_value.update((root / path).read_bytes())
        digest_value.update(b"\0")
    return "sha256:" + digest_value.hexdigest()


def _commit_is_available(repo_root: Path, commit: str) -> bool:
    try:
        subprocess.check_output(
            ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
            cwd=repo_root,
            stderr=subprocess.DEVNULL,
        )
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def _commit_is_ancestor(repo_root: Path, commit: str, head: str) -> bool:
    try:
        subprocess.check_output(
            ["git", "merge-base", "--is-ancestor", commit, head],
            cwd=repo_root,
            stderr=subprocess.DEVNULL,
        )
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def validate_evidence_source_provenance(evidence: dict, *, repo_root: Path | None = None) -> None:
    source = evidence.get("source")
    if not isinstance(source, dict):
        raise ValueError("Evidence source provenance is required")
    commit = source.get("commit")
    if not isinstance(commit, str) or len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
        raise ValueError("Evidence source commit must be a full lowercase Git SHA")
    if evidence.get("commands_source_commit") != commit:
        raise ValueError("Evidence command source commit does not match the recorded source commit")
    if source.get("acceptance_ran_at_commit") is not True:
        raise ValueError("Evidence must state that acceptance ran at the recorded source commit")
    if source.get("worktree_clean_before_generation") is not True:
        raise ValueError("Evidence generation requires a clean source worktree")
    root = Path(repo_root) if repo_root is not None else Path(__file__).resolve().parents[5]
    if source.get("content_hash_algorithm") != SOURCE_CONTENT_HASH_ALGORITHM:
        raise ValueError("Evidence source content hash algorithm is not supported")
    current_content_hash = source_content_hash(root)
    if source.get("content_hash") != current_content_hash:
        raise ValueError("Evidence source content hash does not match the current checkout")
    head = _git_output(root, "rev-parse", "HEAD")
    if _commit_is_available(root, commit) and _commit_is_ancestor(root, commit, head):
        return
    # Squash/rebase and shallow checkouts can erase the tested commit. The
    # content hash above remains the authoritative fallback in those cases.
