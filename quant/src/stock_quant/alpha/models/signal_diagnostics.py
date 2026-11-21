"""Deterministic direct-factor diagnostics over a scheduled decision grid."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Callable, Literal

import numpy as np
from pydantic import Field, field_validator

from ...compiler import digest
from ...contracts import StrictModel
from .evaluation_coverage import attribute_evaluation_coverage


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
