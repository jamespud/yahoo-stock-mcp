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
