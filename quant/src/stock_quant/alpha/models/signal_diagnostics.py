"""Deterministic direct-factor diagnostics over a scheduled decision grid."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Callable, Literal

import numpy as np
from pydantic import Field, field_validator

from ...compiler import digest
from ...contracts import StrictModel
from .evaluation_coverage import attribute_evaluation_coverage


ANALYSIS_LABEL = "predictive_label_analysis_not_executable_backtest"
TOP20_FORWARD_LABEL_GROUP_SIZE = 20
BOOTSTRAP_BLOCK_WEEKS = 4
BOOTSTRAP_DRAWS = 2_000
BOOTSTRAP_SEED = 20261120
BOOTSTRAP_INTERVAL = 0.95
BOOTSTRAP_MIN_WEEKS = 8
BOOTSTRAP_MIN_VALID_ICS = 8
BOOTSTRAP_MIN_FINITE_DRAWS = 1_900
REGIME_LOOKBACK_SESSIONS = 63
REGIME_NAMES = ("UP", "DOWN", "FLAT", "UNKNOWN")


def _utc(value: date | datetime | str) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=timezone.utc)
    raise TypeError(f"Unsupported decision value: {value!r}")


def _session_date(value: date | datetime | str) -> date:
    return _utc(value).date()


class DirectSignalSpec(StrictModel):
    factor_name: str = Field(min_length=1, max_length=128)
    score_multiplier: Literal[1, -1]
    version: Literal["direct-factor-v1"]

    @field_validator("score_multiplier", mode="before")
    @classmethod
    def strict_multiplier(cls, value):
        if type(value) is not int:
            raise ValueError("score_multiplier must be the integer 1 or -1")
        return value


def signal_spec_hash(spec: DirectSignalSpec) -> str:
    return digest(spec.model_dump(mode="json"))


def expected_session_dates(calendar, start: date, end: date) -> list[date]:
    """Return scheduled sessions in the half-open interval ``[start, end)``."""
    start_date, end_date = _session_date(start), _session_date(end)
    if start_date >= end_date:
        raise ValueError("Expected start < end for the session interval")
    sessions = {
        _session_date(session.date)
        for session in calendar.sessions
        if start_date <= _session_date(session.date) < end_date
    }
    return sorted(sessions)


def weekly_last_session_dates(sessions: list[date]) -> list[date]:
    """Return the last actual scheduled session in each ISO calendar week."""
    last_by_week: dict[tuple[int, int], date] = {}
    for value in sessions:
        session = _session_date(value)
        iso = session.isocalendar()
        week = (iso.year, iso.week)
        last_by_week[week] = max(session, last_by_week.get(week, session))
    return sorted(last_by_week.values())


def _optional_mean(values: list[float]) -> float | None:
    return float(np.mean(values)) if values else None


def _ic_summary(records: list[dict]) -> tuple[float | None, float | None, float | None]:
    correlations = [record["rank_ic"] for record in records if record["rank_ic"] is not None]
    mean = _optional_mean(correlations)
    std = float(np.std(correlations, ddof=1)) if len(correlations) >= 2 else None
    icir = mean / std if mean is not None and std not in (None, 0.0) else None
    return mean, std, icir


def _period_record(records: list[dict], *, start: str | None, end: str | None, **extra) -> dict:
    correlations = [
        float(record["rank_ic"])
        for record in records
        if record.get("rank_ic") is not None and np.isfinite(float(record["rank_ic"]))
    ]
    mean = float(np.mean(correlations)) if correlations else None
    std = float(np.std(correlations, ddof=1)) if len(correlations) >= 2 else None
    icir = mean / std if mean is not None and std not in (None, 0.0) else None
    return {
        **extra,
        "period_start": start,
        "period_end": end,
        "decision_count": len(records),
        "valid_ic_decision_count": len(correlations),
        "invalid_ic_decision_count": len(records) - len(correlations),
        "expected_count": sum(int(record.get("expected_count", 0)) for record in records),
        "valid_count": sum(int(record.get("valid_count", 0)) for record in records),
        "excluded_count": sum(int(record.get("excluded_count", 0)) for record in records),
        "rank_ic_mean": mean,
        "rank_ic_std": std,
        "icir": icir,
        "decisions": [
            {
                **record,
                "decision_at": _utc(record["decision_at"]).isoformat(),
            }
            for record in records
        ],
    }


def stability_by_year(records: list[dict]) -> list[dict]:
    """Aggregate decision-level IC evidence by UTC calendar year without dropping nulls."""
    ordered = sorted(records, key=lambda record: _utc(record["decision_at"]))
    grouped: dict[int, list[dict]] = {}
    for record in ordered:
        grouped.setdefault(_utc(record["decision_at"]).year, []).append(record)
    if not grouped:
        return []
    result = []
    for year in range(min(grouped), max(grouped) + 1):
        group = grouped.get(year, [])
        if group:
            record = _period_record(
                group,
                start=group[0]["decision_at"],
                end=group[-1]["decision_at"],
                year=year,
            )
            record.update({"status": "OBSERVED", "reason": None})
        else:
            start = datetime(year, 1, 1, tzinfo=timezone.utc).isoformat()
            end = datetime(year + 1, 1, 1, tzinfo=timezone.utc).isoformat()
            record = _period_record(
                [],
                start=start,
                end=end,
                year=year,
            )
            record.update({"status": "EMPTY", "reason": "NO_DECISIONS_IN_YEAR"})
        result.append(record)
    return result


def stability_by_subperiod(records: list[dict], *, period_count: int = 4) -> list[dict]:
    """Return contiguous chronological chunks, retaining empty and null-IC periods."""
    if type(period_count) is not int or period_count < 1:
        raise ValueError("period_count must be a positive integer")
    ordered = sorted(records, key=lambda record: _utc(record["decision_at"]))
    chunks = np.array_split(np.asarray(ordered, dtype=object), period_count) if ordered else [
        np.asarray([], dtype=object) for _ in range(period_count)
    ]
    result = []
    for index, chunk in enumerate(chunks):
        group = list(chunk)
        result.append(_period_record(
            group,
            start=group[0]["decision_at"] if group else None,
            end=group[-1]["decision_at"] if group else None,
            period_index=index,
        ))
    return result


def _benchmark_observations(benchmark_rows: list[dict], *, benchmark_symbol: str) -> list[dict]:
    symbol = benchmark_symbol.upper()
    by_session = {}
    for row in benchmark_rows:
        if str(row["instrument_id"]).upper() != symbol:
            continue
        session = _session_date(row["start_at"])
        if session in by_session:
            raise ValueError(f"Duplicate benchmark session: {session.isoformat()}")
        by_session[session] = row
    return [
        {
            **row,
            "session": session,
            "available_at": _utc(row["available_at"]),
            "close_value": float(row["close"]),
        }
        for session, row in sorted(by_session.items())
    ]


def _regime_for_decision(
    record: dict,
    observations: list[dict],
    *,
    benchmark_symbol: str | None,
    expected_sessions: list[date] | None,
    expected_indices: dict[date, int] | None,
) -> dict:
    decision_at = _utc(record["decision_at"])
    base = {
        **record,
        "decision_at": decision_at.isoformat(),
        "benchmark_symbol": benchmark_symbol.upper() if benchmark_symbol else None,
        "benchmark_trailing_return": None,
        "benchmark_window_start": None,
        "benchmark_window_end": None,
        "benchmark_observation_count": 0,
        "missing_benchmark_sessions": [],
        "unavailable_benchmark_sessions": [],
        "regime": "UNKNOWN",
        "regime_reason": None,
    }
    if benchmark_symbol is None:
        return {**base, "regime_reason": "BENCHMARK_NOT_CONFIGURED"}
    if not expected_sessions or expected_indices is None:
        return {**base, "regime_reason": "EXPECTED_SESSION_GRID_REQUIRED"}
    decision_session = decision_at.date()
    if decision_session not in expected_indices:
        return {**base, "regime_reason": "DECISION_NOT_IN_EXPECTED_SESSION_GRID"}
    decision_index = expected_indices[decision_session]
    required = REGIME_LOOKBACK_SESSIONS + 1
    if decision_index + 1 < required:
        return {
            **base,
            "benchmark_observation_count": decision_index + 1,
            "benchmark_window_start": expected_sessions[0].isoformat(),
            "benchmark_window_end": decision_session.isoformat(),
            "regime_reason": "INCOMPLETE_63_SESSION_WINDOW",
        }
    window_sessions = expected_sessions[decision_index - REGIME_LOOKBACK_SESSIONS:decision_index + 1]
    by_session = {row["session"]: row for row in observations}
    missing = [session for session in window_sessions if session not in by_session]
    unavailable = [
        session for session in window_sessions
        if session in by_session and by_session[session]["available_at"] > decision_at
    ]
    common = {
        **base,
        "benchmark_observation_count": len(window_sessions) - len(missing) - len(unavailable),
        "benchmark_window_start": window_sessions[0].isoformat(),
        "benchmark_window_end": window_sessions[-1].isoformat(),
        "missing_benchmark_sessions": [session.isoformat() for session in missing],
        "unavailable_benchmark_sessions": [session.isoformat() for session in unavailable],
    }
    if missing:
        return {**common, "regime_reason": "MISSING_BENCHMARK_SESSION"}
    if unavailable:
        return {**common, "regime_reason": "BENCHMARK_NOT_AVAILABLE_BY_DECISION"}
    start_value = by_session[window_sessions[0]]["close_value"]
    end_value = by_session[window_sessions[-1]]["close_value"]
    if not np.isfinite(start_value) or not np.isfinite(end_value) or start_value <= 0:
        return {**common, "regime_reason": "INVALID_BENCHMARK_PRICE"}
    trailing_return = float(end_value / start_value - 1.0)
    regime = "UP" if trailing_return > 0 else "DOWN" if trailing_return < 0 else "FLAT"
    return {
        **common,
        "benchmark_trailing_return": trailing_return,
        "regime": regime,
        "regime_reason": None,
    }


def regime_breakdown(
    records: list[dict],
    benchmark_rows: list[dict],
    *,
    benchmark_symbol: str | None = None,
    expected_sessions: list[date | datetime] | None = None,
) -> list[dict]:
    """Group IC decisions by an explicit trailing-63-session benchmark regime."""
    observations = (
        _benchmark_observations(benchmark_rows, benchmark_symbol=benchmark_symbol)
        if benchmark_symbol else []
    )
    expected = sorted({_session_date(value) for value in expected_sessions}) if expected_sessions else None
    expected_indices = {session: index for index, session in enumerate(expected)} if expected else None
    classified = [
        _regime_for_decision(
            record,
            observations,
            benchmark_symbol=benchmark_symbol,
            expected_sessions=expected,
            expected_indices=expected_indices,
        )
        for record in sorted(records, key=lambda item: _utc(item["decision_at"]))
    ]
    grouped = {regime: [] for regime in REGIME_NAMES}
    for record in classified:
        grouped[record["regime"]].append(record)
    return [
        {
            **_period_record(
                group,
                start=group[0]["decision_at"] if group else None,
                end=group[-1]["decision_at"] if group else None,
                regime=regime,
            ),
            "benchmark_symbol": benchmark_symbol.upper() if benchmark_symbol else None,
            "lookback_sessions": REGIME_LOOKBACK_SESSIONS,
            "definition": "UNKNOWN unless a complete 64-observation benchmark window is available by decision",
        }
        for regime, group in grouped.items()
    ]


def sign_consistency(records: list[dict]) -> dict:
    """Describe directional consistency without treating missing IC periods as zero."""
    values = [
        float(record["rank_ic"])
        for record in records
        if record.get("rank_ic") is not None and np.isfinite(float(record["rank_ic"]))
    ]
    positive = sum(value > 0 for value in values)
    negative = sum(value < 0 for value in values)
    zero = sum(value == 0 for value in values)
    null_count = len(records) - len(values)
    if len(values) < 2:
        status, reason = "NOT_TESTED", "FEWER_THAN_TWO_VALID_PERIODS"
    elif positive == len(values):
        status, reason = "CONSISTENT_POSITIVE", "ALL_VALID_PERIODS_POSITIVE"
    elif negative == len(values):
        status, reason = "CONSISTENT_NEGATIVE", "ALL_VALID_PERIODS_NEGATIVE"
    elif zero == len(values):
        status, reason = "ZERO", "ALL_VALID_PERIODS_ZERO"
    else:
        status, reason = "MIXED", "POSITIVE_AND_NEGATIVE_PERIODS"
    return {
        "status": status,
        "reason": reason,
        "period_count": len(records),
        "valid_period_count": len(values),
        "null_count": null_count,
        "positive_count": positive,
        "negative_count": negative,
        "zero_count": zero,
        "positive_fraction": positive / len(values) if values else None,
        "negative_fraction": negative / len(values) if values else None,
    }


def _bootstrap_draw_matrix(length: int) -> np.ndarray:
    blocks_per_draw = int(np.ceil(length / BOOTSTRAP_BLOCK_WEEKS))
    rng = np.random.Generator(np.random.PCG64(BOOTSTRAP_SEED))
    starts = rng.integers(
        0,
        length - BOOTSTRAP_BLOCK_WEEKS + 1,
        size=(BOOTSTRAP_DRAWS, blocks_per_draw),
    )
    offsets = np.arange(BOOTSTRAP_BLOCK_WEEKS)
    indices = (starts[:, :, None] + offsets[None, None, :]).reshape(BOOTSTRAP_DRAWS, -1)
    return indices[:, :length]


def _bootstrap_draw_means(values: np.ndarray, indices: np.ndarray) -> tuple[np.ndarray, int, int]:
    if indices.size:
        draws = np.asarray([values[row] for row in indices], dtype=float)
        finite = np.all(np.isfinite(draws), axis=1)
        means = np.mean(draws[finite], axis=1) if finite.any() else np.asarray([], dtype=float)
    else:
        finite = np.zeros(0, dtype=bool)
        means = np.asarray([], dtype=float)
    return means, int(finite.sum()), len(finite)


def _bootstrap_metric(values: np.ndarray, means: np.ndarray, *, finite_count: int, total_draws: int,
                      status: str, reason: str | None) -> dict:
    observed = values[np.isfinite(values)]
    alpha = (1.0 - BOOTSTRAP_INTERVAL) / 2.0
    return {
        "status": status,
        "reason": reason,
        "observed_mean": float(np.mean(observed)) if observed.size else None,
        "ci_lower": float(np.quantile(means, alpha)) if status == "TESTED" and means.size else None,
        "ci_upper": float(np.quantile(means, 1.0 - alpha)) if status == "TESTED" and means.size else None,
        "finite_draw_count": finite_count,
        "excluded_draw_count": total_draws - finite_count,
    }


def moving_block_bootstrap(weekly_ic: list[dict]) -> dict:
    """Run the frozen 4-week moving-block bootstrap over chronological weekly ICs."""
    ordered = sorted(weekly_ic, key=lambda record: _utc(record["decision_at"]))
    length = len(ordered)
    values = np.asarray([
        float(record["rank_ic"])
        if record.get("rank_ic") is not None and np.isfinite(float(record["rank_ic"]))
        else np.nan
        for record in ordered
    ], dtype=float)
    paired_values = np.asarray([
        float(record["paired_rank_ic"])
        if record.get("paired_rank_ic") is not None and np.isfinite(float(record["paired_rank_ic"]))
        else np.nan
        for record in ordered
    ], dtype=float) if any("paired_rank_ic" in record for record in ordered) else None
    valid_ic_count = int(np.isfinite(values).sum())
    indices = _bootstrap_draw_matrix(length) if length >= BOOTSTRAP_BLOCK_WEEKS else None
    if indices is not None:
        rank_means, rank_finite_count, rank_total_draws = _bootstrap_draw_means(values, indices)
        if paired_values is not None:
            delta_values = values - paired_values
            paired_means, paired_finite_count, paired_total_draws = _bootstrap_draw_means(paired_values, indices)
            delta_means, delta_finite_count, delta_total_draws = _bootstrap_draw_means(delta_values, indices)
        else:
            paired_means = delta_means = np.asarray([], dtype=float)
            paired_finite_count = delta_finite_count = 0
            paired_total_draws = delta_total_draws = 0
    else:
        rank_means = paired_means = delta_means = np.asarray([], dtype=float)
        rank_finite_count = paired_finite_count = delta_finite_count = 0
        rank_total_draws = paired_total_draws = delta_total_draws = 0
    if length < BOOTSTRAP_MIN_WEEKS:
        base_reason = "FEWER_THAN_EIGHT_WEEKS"
    elif valid_ic_count < BOOTSTRAP_MIN_VALID_ICS:
        base_reason = "FEWER_THAN_EIGHT_VALID_ICS"
    else:
        base_reason = None

    def metric_status(finite_count: int, *, paired: bool = False) -> tuple[str, str | None]:
        if base_reason is not None:
            return "NOT_TESTED", base_reason
        if finite_count < BOOTSTRAP_MIN_FINITE_DRAWS:
            return "NOT_TESTED", (
                "FEWER_THAN_1900_FINITE_PAIRED_DRAWS" if paired else "FEWER_THAN_1900_FINITE_DRAWS"
            )
        return "TESTED", None

    rank_status, rank_reason = metric_status(rank_finite_count)
    rank_summary = _bootstrap_metric(
        values, rank_means, finite_count=rank_finite_count,
        total_draws=rank_total_draws, status=rank_status, reason=rank_reason,
    )
    paired_summary = None
    delta_summary = None
    if paired_values is not None:
        paired_status, paired_reason = metric_status(paired_finite_count, paired=True)
        delta_status, delta_reason = metric_status(delta_finite_count, paired=True)
        paired_summary = _bootstrap_metric(
            paired_values, paired_means, finite_count=paired_finite_count,
            total_draws=paired_total_draws, status=paired_status, reason=paired_reason,
        )
        delta_summary = _bootstrap_metric(
            values - paired_values, delta_means, finite_count=delta_finite_count,
            total_draws=delta_total_draws, status=delta_status, reason=delta_reason,
        )
    if base_reason is not None:
        status, reason = "NOT_TESTED", base_reason
    elif rank_status != "TESTED":
        status, reason = "NOT_TESTED", rank_reason
    elif paired_summary is not None and (
        paired_summary["status"] != "TESTED" or delta_summary["status"] != "TESTED"
    ):
        status = "NOT_TESTED"
        reason = (
            paired_summary["reason"] if paired_summary["status"] != "TESTED"
            else delta_summary["reason"]
        )
    else:
        status, reason = "TESTED", None
    result = {
        "schema_version": "weekly-moving-block-bootstrap-v1",
        "status": status,
        "reason": reason,
        "config": {
            "block_weeks": BOOTSTRAP_BLOCK_WEEKS,
            "draws": BOOTSTRAP_DRAWS,
            "seed": BOOTSTRAP_SEED,
            "rng": "PCG64",
            "interval": BOOTSTRAP_INTERVAL,
            "percentile_method": "linear",
        },
        "week_count": length,
        "valid_ic_count": valid_ic_count,
        "finite_draw_count": rank_finite_count,
        "thresholds": {
            "minimum_weeks": BOOTSTRAP_MIN_WEEKS,
            "minimum_valid_ics": BOOTSTRAP_MIN_VALID_ICS,
            "minimum_finite_draws": BOOTSTRAP_MIN_FINITE_DRAWS,
        },
        "rank_ic": rank_summary,
        "paired_rank_ic": paired_summary,
        "paired_delta": delta_summary,
        "draw_alignment": "one shared moving-block index matrix for all paired metrics",
        "definition": "Current-week decision ICs; four-consecutive-week blocks sampled with replacement.",
    }
    return result


def _reason_list(record: dict) -> list[str]:
    reasons = record.get("exclusion_reasons")
    if isinstance(reasons, dict):
        return sorted(reasons)
    if isinstance(reasons, list):
        return list(reasons)
    return []


def evaluate_direct_signal(
    feature_rows: list[dict],
    label_rows: list,
    *,
    expected_instruments: list[str],
    expected_sessions: list[date | datetime],
    spec: DirectSignalSpec,
    decision_key: Callable[[datetime], object] | None = None,
) -> dict:
    """Evaluate a pre-signed factor without fitting a model.

    The default decision key groups exact feature/label timestamps by their UTC
    session date so a scheduled session is one cross-section even when rows
    carry distinct observation times.
    """
    if decision_key is None:
        decision_key = lambda value: _utc(value).date()
    instruments = sorted({str(value).upper() for value in expected_instruments})
    sessions = sorted({_session_date(value) for value in expected_sessions})
    if not sessions:
        raise ValueError("Expected at least one decision session")
    if not instruments:
        raise ValueError("Expected at least one instrument")

    predictions = []
    for row in feature_rows:
        if row.get("factor_name") != spec.factor_name:
            continue
        instrument = str(row["instrument_id"]).upper()
        if instrument not in instruments:
            continue
        decision_at = _utc(row["decision_at"])
        feature_available_at = _utc(row["feature_available_at"])
        if feature_available_at > decision_at:
            raise ValueError("Feature contains values available after its decision time")
        value = row.get("factor_value")
        prediction_value = (
            float(value)
            if row.get("eligible", True) and value is not None and np.isfinite(float(value))
            else float("nan")
        )
        predictions.append({
            "decision_at": decision_at,
            "instrument_id": instrument,
            "prediction_value": prediction_value * spec.score_multiplier,
        })

    coverage = attribute_evaluation_coverage(
        predictions,
        label_rows,
        expected_instruments=instruments,
        expected_decisions=sessions,
        eligible_universe=instruments,
        decision_key=decision_key,
    )
    records = []
    for record in coverage.summary["date_records"]:
        reasons = _reason_list(record)
        if record["rank_ic"] is None and not reasons:
            reasons = ["INSUFFICIENT_CROSS_SECTION"]
        records.append({**record, "exclusion_reasons": reasons})

    correlations = [record["rank_ic"] for record in records if record["rank_ic"] is not None]
    mean = float(np.mean(correlations)) if correlations else None
    std = float(np.std(correlations, ddof=1)) if len(correlations) >= 2 else None
    icir = mean / std if mean is not None and std not in (None, 0.0) else None
    label_versions = sorted({row.label_version for row in label_rows if isinstance(row.label_version, str)})
    first, last = sessions[0], sessions[-1]
    evaluation_window = {
        "start": datetime.combine(first, time.min, tzinfo=timezone.utc).isoformat(),
        "end": (datetime.combine(last, time.min, tzinfo=timezone.utc) + timedelta(days=1)).isoformat(),
    }
    summary = {
        "signal_spec": spec.model_dump(mode="json"),
        "signal_spec_hash": signal_spec_hash(spec),
        "label_version": label_versions[0] if len(label_versions) == 1 else "mixed",
        "evaluation_window": evaluation_window,
        "rank_ic_by_decision": records,
        "rank_ic_mean": mean,
        "rank_ic_std": std,
        "icir": icir,
        "coverage": coverage.summary,
        "excluded_opportunities": coverage.summary["excluded_opportunities"],
        "decision_count": len(records),
        "valid_ic_decision_count": len(correlations),
        "invalid_ic_decision_count": len(records) - len(correlations),
        "definition": (
            "Direct factor score = factor_value * score_multiplier; per-session Spearman IC; "
            "equal-decision mean; ICIR uses sample standard deviation."
        ),
    }
    return {
        "summary": summary,
        "rank_ic_by_decision": records,
        "coverage": coverage.summary,
    }


def evaluate_weekly_signal(
    feature_rows: list[dict],
    label_rows: list,
    *,
    expected_instruments: list[str],
    expected_sessions: list[date | datetime],
    spec: DirectSignalSpec,
) -> dict:
    """Evaluate one decision on the last actual session of each calendar week."""
    weekly_sessions = weekly_last_session_dates(expected_sessions)
    if not weekly_sessions:
        raise ValueError("Expected at least one weekly decision session")
    weekly = evaluate_direct_signal(
        feature_rows,
        label_rows,
        expected_instruments=expected_instruments,
        expected_sessions=weekly_sessions,
        spec=spec,
    )
    records = weekly["rank_ic_by_decision"]
    mean, std, icir = _ic_summary(records)
    coverage = weekly["coverage"]
    summary = {
        "evaluation_frequency": "weekly_last_trading_session",
        "weekly_decision_dates": [session.isoformat() for session in weekly_sessions],
        "weekly_decision_count": len(records),
        "weekly_valid_decision_count": sum(record["rank_ic"] is not None for record in records),
        "weekly_invalid_decision_count": sum(record["rank_ic"] is None for record in records),
        "weekly_sample_count": coverage["valid_matched_opportunities"],
        "weekly_prediction_coverage": coverage["prediction_coverage"],
        "weekly_label_coverage": coverage["label_coverage"],
        "weekly_rank_ic_mean": mean,
        "weekly_rank_ic_std": std,
        "weekly_icir": icir,
        "weekly_excluded_opportunities": coverage["excluded_opportunities"],
        "weekly_coverage": coverage,
    }
    return {"weekly_rank_ic": records, "summary": summary}


def _finite_prediction(row: dict) -> float | None:
    if not row.get("eligible", True):
        return None
    value = row.get("factor_value")
    if value is None:
        return None
    numeric = float(value)
    return numeric if np.isfinite(numeric) else None


def _weekly_signal_rows(
    feature_rows: list[dict],
    *,
    session: date,
    instruments: set[str],
    spec: DirectSignalSpec,
) -> dict[str, float | None]:
    rows: dict[str, float | None] = {}
    for row in feature_rows:
        if row.get("factor_name") != spec.factor_name:
            continue
        instrument = str(row["instrument_id"]).upper()
        if instrument not in instruments:
            continue
        decision_at = _utc(row["decision_at"])
        if decision_at.date() != session:
            continue
        if _utc(row["feature_available_at"]) > decision_at:
            raise ValueError("Feature contains values available after its decision time")
        if instrument in rows:
            raise ValueError(f"Duplicate prediction opportunity: {(session, instrument)}")
        value = _finite_prediction(row)
        rows[instrument] = None if value is None else value * spec.score_multiplier
    return rows


def _weekly_labels(
    label_rows: list,
    *,
    session: date,
    instruments: set[str],
) -> dict[str, float | None]:
    rows: dict[str, float | None] = {}
    for label in label_rows:
        instrument = str(label.instrument_id).upper()
        if instrument not in instruments or _utc(label.feature_decision_at).date() != session:
            continue
        if instrument in rows:
            raise ValueError(f"Duplicate label opportunity: {(session, instrument)}")
        value = label.label_value
        rows[instrument] = float(value) if value is not None and np.isfinite(float(value)) else None
    return rows


def top20_forward_diagnostics(
    feature_rows: list[dict],
    label_rows: list,
    *,
    expected_instruments: list[str],
    expected_sessions: list[date | datetime],
    spec: DirectSignalSpec,
) -> dict:
    """Describe frozen weekly Top20 forward-label groups without execution assumptions."""
    weekly_sessions = weekly_last_session_dates(expected_sessions)
    if not weekly_sessions:
        raise ValueError("Expected at least one weekly decision session")
    instruments = sorted({str(value).upper() for value in expected_instruments})
    instrument_set = set(instruments)
    records = []
    previous_complete: set[str] | None = None

    for session in weekly_sessions:
        signals = _weekly_signal_rows(
            feature_rows,
            session=session,
            instruments=instrument_set,
            spec=spec,
        )
        ranked = sorted(
            ((instrument, value) for instrument, value in signals.items() if value is not None),
            key=lambda item: (-item[1], item[0]),
        )
        selected = [instrument for instrument, _ in ranked[:TOP20_FORWARD_LABEL_GROUP_SIZE]]
        selected_set = set(selected)
        complete = len(selected) == TOP20_FORWARD_LABEL_GROUP_SIZE
        labels = _weekly_labels(label_rows, session=session, instruments=instrument_set)
        ranked_ids = [instrument for instrument, _ in ranked]

        top20_values = [labels[instrument] for instrument in selected if labels.get(instrument) is not None]
        universe_values = [labels[instrument] for instrument in ranked_ids if labels.get(instrument) is not None]
        rest_values = [
            labels[instrument]
            for instrument in ranked_ids
            if instrument not in selected_set and labels.get(instrument) is not None
        ]
        top20_mean = _optional_mean(top20_values) if complete else None
        universe_mean = _optional_mean(universe_values)
        rest_mean = _optional_mean(rest_values)
        reasons = []
        if not complete:
            reasons.append("INSUFFICIENT_TOP20_NAMES")
        elif len(top20_values) < TOP20_FORWARD_LABEL_GROUP_SIZE:
            reasons.append("MISSING_TOP20_LABELS")
        if len(universe_values) < len(ranked_ids):
            reasons.append("MISSING_UNIVERSE_LABELS")
        if complete and not rest_values:
            reasons.append("NO_REST_LABELS")

        turnover = None
        if previous_complete is not None and complete:
            turnover = 1.0 - len(previous_complete & selected_set) / TOP20_FORWARD_LABEL_GROUP_SIZE
        previous_complete = selected_set if complete else None
        valid_selected_count = len(top20_values)
        records.append({
            "decision_at": session.isoformat(),
            "analysis_label": ANALYSIS_LABEL,
            "eligible_name_count": len(ranked),
            "top20_member_ids": selected,
            "top20_member_count": len(selected),
            "top20_forward_label_count": valid_selected_count,
            "universe_forward_label_count": len(universe_values),
            "top20_rest_forward_label_count": len(rest_values),
            "top20_forward_label_mean": top20_mean,
            "universe_forward_label_mean": universe_mean,
            "top20_universe_spread": (
                top20_mean - universe_mean if top20_mean is not None and universe_mean is not None else None
            ),
            "top20_rest_spread": (
                top20_mean - rest_mean if top20_mean is not None and rest_mean is not None else None
            ),
            "turnover_proxy": turnover,
            "group_coverage": (
                valid_selected_count / TOP20_FORWARD_LABEL_GROUP_SIZE if complete else 0.0
            ),
            "selected_label_coverage": valid_selected_count / len(selected) if selected else 0.0,
            "missing_top20_label_ids": [instrument for instrument in selected if labels.get(instrument) is None],
            "exclusion_reasons": reasons,
        })

    complete_records = [record for record in records if record["top20_member_count"] == TOP20_FORWARD_LABEL_GROUP_SIZE]
    turnovers = [record["turnover_proxy"] for record in records if record["turnover_proxy"] is not None]
    summary = {
        "analysis_label": ANALYSIS_LABEL,
        "top20_size": TOP20_FORWARD_LABEL_GROUP_SIZE,
        "weekly_decision_dates": [session.isoformat() for session in weekly_sessions],
        "decision_count": len(records),
        "complete_selection_count": len(complete_records),
        "insufficient_name_count": sum("INSUFFICIENT_TOP20_NAMES" in record["exclusion_reasons"] for record in records),
        "top20_forward_label_mean": _optional_mean([
            record["top20_forward_label_mean"] for record in records
            if record["top20_forward_label_mean"] is not None
        ]),
        "universe_forward_label_mean": _optional_mean([
            record["universe_forward_label_mean"] for record in records
            if record["universe_forward_label_mean"] is not None
        ]),
        "top20_universe_spread": _optional_mean([
            record["top20_universe_spread"] for record in records
            if record["top20_universe_spread"] is not None
        ]),
        "top20_rest_spread": _optional_mean([
            record["top20_rest_spread"] for record in records
            if record["top20_rest_spread"] is not None
        ]),
        "turnover_proxy": _optional_mean(turnovers),
        "turnover_transition_count": len(turnovers),
        "group_coverage": _optional_mean([record["group_coverage"] for record in records]),
    }
    return {"top20_forward_labels": records, "summary": summary, "analysis_label": ANALYSIS_LABEL}
