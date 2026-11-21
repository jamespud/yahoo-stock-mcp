"""Frozen Alpha v1.3 diagnostics protocol and claim-boundary validation."""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, time, timezone
from typing import Callable

from ...compiler import digest


SOURCE_PROTOCOL_HASH = "sha256:eb5dfc8902172c11fd5f9ff17e76a134fc264e9faa2edef84da56231d846a45d"
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


def protocol_hash(protocol: dict) -> str:
    return digest(protocol)


PROTOCOL_HASH = protocol_hash(_FROZEN_PROTOCOL)


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
    if protocol.get("protocol_hash") != PROTOCOL_HASH:
        raise ValueError("Protocol hash does not match the frozen diagnostics protocol")
    if protocol.get("source_protocol_hash") != SOURCE_PROTOCOL_HASH:
        raise ValueError("Source protocol identity mismatch")
    if protocol.get("symbol_count") != 50 or protocol.get("symbols") != list(SOURCE_SYMBOLS):
        raise ValueError("Frozen 50-symbol identity mismatch")
    sections = evidence.get("synthetic_acceptance", {}).get("sections", {})
    if set(sections) != {"B1", "B2", "B3", "B4", "B5"}:
        raise ValueError("Synthetic acceptance must contain exactly B1 through B5")
    if any(section.get("status") != "PASS" for section in sections.values()):
        raise ValueError("Every synthetic acceptance section must pass")
