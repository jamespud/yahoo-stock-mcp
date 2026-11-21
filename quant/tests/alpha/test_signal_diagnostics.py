from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pytest
from pydantic import ValidationError
from scipy.stats import spearmanr

from stock_quant.alpha.models.evaluation_coverage import attribute_evaluation_coverage
from stock_quant.alpha.models.labels import LabelRow
from stock_quant.alpha.models.signal_diagnostics import (
    DirectSignalSpec,
    evaluate_direct_signal,
    expected_session_dates,
    signal_spec_hash,
)

UTC = timezone.utc
SESSIONS = [date(2024, 1, day) for day in (2, 3, 4, 5)]
DECISION_AT = {
    session: datetime.combine(session, time(21, 5), tzinfo=UTC)
    for session in SESSIONS
}


def _feature(session: date, instrument: str, value: float | None, *, factor_name="mom63") -> dict:
    return {
        "decision_at": DECISION_AT[session],
        "instrument_id": instrument,
        "factor_name": factor_name,
        "factor_value": value,
        "feature_available_at": DECISION_AT[session],
        "eligible": value is not None,
        "quality_flags": [] if value is not None else ["NULL_FACTOR"],
        "factor_version": "fixture-v1",
    }


def _label(
    session: date,
    instrument: str,
    value: float | None,
    *,
    quality: list[str] | None = None,
) -> LabelRow:
    decision_at = DECISION_AT[session]
    return LabelRow(
        feature_decision_at=decision_at,
        entry_at=decision_at + timedelta(days=1),
        exit_at=decision_at + timedelta(days=5),
        label_available_at=decision_at + timedelta(days=5),
        label_value=value,
        label_quality=quality or ([] if value is not None else ["MISSING_LABEL"]),
        instrument_id=instrument,
        label_name="stock_forward_return",
        label_version="session-aligned-v2",
    )


def test_expected_session_dates_uses_half_open_interval_in_calendar_order():
    calendar = SimpleNamespace(sessions=[
        SimpleNamespace(date=date(2024, 1, 5)),
        SimpleNamespace(date=date(2024, 1, 2)),
        SimpleNamespace(date=date(2024, 1, 3)),
        SimpleNamespace(date=date(2024, 1, 4)),
        SimpleNamespace(date=date(2024, 1, 8)),
    ])

    assert expected_session_dates(calendar, date(2024, 1, 2), date(2024, 1, 5)) == [
        date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4),
    ]
    assert expected_session_dates(calendar, date(2024, 1, 5), date(2024, 1, 9)) == [
        date(2024, 1, 5), date(2024, 1, 8),
    ]


def test_mom63_matches_scipy_and_uses_equal_decision_mean_and_sample_icir():
    instruments = ["AAPL", "MSFT", "NVDA"]
    labels_by_session = {
        SESSIONS[0]: [0.01, 0.03, 0.02],
        SESSIONS[1]: [0.04, -0.01, 0.02],
        SESSIONS[2]: [0.02, 0.01, 0.05],
    }
    factors_by_session = {
        SESSIONS[0]: [0.10, 0.30, 0.20],
        SESSIONS[1]: [0.10, -0.20, 0.15],
        SESSIONS[2]: [0.20, 0.05, 0.40],
    }
    features, labels = [], []
    expected_ics = []
    for session in SESSIONS[:3]:
        features.extend(
            _feature(session, instrument, value)
            for instrument, value in zip(instruments, factors_by_session[session], strict=True)
        )
        labels.extend(
            _label(session, instrument, value)
            for instrument, value in zip(instruments, labels_by_session[session], strict=True)
        )
        expected_ics.append(float(spearmanr(
            labels_by_session[session], factors_by_session[session]
        ).statistic))

    result = evaluate_direct_signal(
        features,
        labels,
        expected_instruments=instruments,
        expected_sessions=SESSIONS[:3],
        spec=DirectSignalSpec(factor_name="mom63", score_multiplier=1, version="direct-factor-v1"),
    )

    assert [row["rank_ic"] for row in result["rank_ic_by_decision"]] == pytest.approx(expected_ics)
    assert result["summary"]["rank_ic_mean"] == pytest.approx(float(np.mean(expected_ics)))
    expected_icir = float(np.mean(expected_ics) / np.std(expected_ics, ddof=1))
    assert result["summary"]["icir"] == pytest.approx(expected_icir)


def test_ties_use_scipy_average_ranks():
    session = SESSIONS[0]
    instruments = ["AAPL", "MSFT", "NVDA", "JPM"]
    factors = [3.0, 1.0, 1.0, 2.0]
    labels = [4.0, 1.0, 2.0, 3.0]
    result = evaluate_direct_signal(
        [_feature(session, symbol, value) for symbol, value in zip(instruments, factors, strict=True)],
        [_label(session, symbol, value) for symbol, value in zip(instruments, labels, strict=True)],
        expected_instruments=instruments,
        expected_sessions=[session],
        spec=DirectSignalSpec(factor_name="mom63", score_multiplier=-1, version="direct-factor-v1"),
    )

    expected = float(spearmanr(labels, [-value for value in factors]).statistic)
    assert result["rank_ic_by_decision"][0]["rank_ic"] == pytest.approx(expected)


@pytest.mark.parametrize(
    ("kind", "reason"),
    [
        ("null_factor", "NONFINITE_PREDICTION"),
        ("nonfinite_factor", "NONFINITE_PREDICTION"),
        ("constant_factor", "CONSTANT_PREDICTION"),
        ("constant_label", "CONSTANT_LABEL"),
        ("missing_feature", "NO_PREDICTION"),
        ("missing_label", "NO_LABEL"),
        ("null_label", "INVALID_LABEL"),
    ],
)
def test_invalid_cross_sections_are_retained_with_explicit_reason(kind, reason):
    session = SESSIONS[0]
    instruments = ["AAPL", "MSFT", "NVDA"]
    factors: list[float | None] = [0.1, 0.2, 0.3]
    labels: list[float | None] = [0.01, 0.02, 0.03]
    label_quality = None
    if kind == "null_factor":
        factors = [None, None, None]
    elif kind == "nonfinite_factor":
        factors = [float("nan"), float("nan"), float("nan")]
    elif kind == "constant_factor":
        factors = [0.2, 0.2, 0.2]
    elif kind == "constant_label":
        labels = [0.01, 0.01, 0.01]
    elif kind == "null_label":
        labels = [None, None, None]
        label_quality = ["MISSING_LABEL"]
    features = [] if kind == "missing_feature" else [
        _feature(session, symbol, value) for symbol, value in zip(instruments, factors, strict=True)
    ]
    label_rows = [] if kind == "missing_label" else [
        _label(session, symbol, value, quality=label_quality)
        for symbol, value in zip(instruments, labels, strict=True)
    ]

    result = evaluate_direct_signal(
        features,
        label_rows,
        expected_instruments=instruments,
        expected_sessions=[session],
        spec=DirectSignalSpec(factor_name="mom63", score_multiplier=1, version="direct-factor-v1"),
    )

    record = result["rank_ic_by_decision"][0]
    assert record["rank_ic"] is None
    assert reason in record["exclusion_reasons"]
    assert result["summary"]["rank_ic_mean"] is None
    assert result["summary"]["icir"] is None
    assert result["summary"]["excluded_opportunities"] > 0


def test_one_pair_cross_section_is_retained_as_null_ic():
    session = SESSIONS[0]
    result = evaluate_direct_signal(
        [_feature(session, "AAPL", 0.1)],
        [_label(session, "AAPL", 0.01)],
        expected_instruments=["AAPL", "MSFT", "NVDA"],
        expected_sessions=[session],
        spec=DirectSignalSpec(factor_name="mom63", score_multiplier=1, version="direct-factor-v1"),
    )

    record = result["rank_ic_by_decision"][0]
    assert record["rank_ic"] is None
    assert "INSUFFICIENT_CROSS_SECTION" in record["exclusion_reasons"]


def test_decision_key_groups_exact_timestamps_without_changing_default_coverage():
    session = SESSIONS[0]
    later = datetime(2024, 1, 2, 21, 6, tzinfo=UTC)
    predictions = [
        {"decision_at": DECISION_AT[session], "instrument_id": "AAPL", "prediction_value": 0.1},
        {"decision_at": later, "instrument_id": "MSFT", "prediction_value": 0.2},
    ]
    labels = [_label(session, "AAPL", 0.01), _label(session, "MSFT", 0.02)]
    labels[1] = labels[1].model_copy(update={"feature_decision_at": later})
    default = attribute_evaluation_coverage(
        predictions,
        labels,
        expected_instruments=["AAPL", "MSFT"],
        expected_decisions=[DECISION_AT[session], later],
    )
    identity = attribute_evaluation_coverage(
        predictions,
        labels,
        expected_instruments=["AAPL", "MSFT"],
        expected_decisions=[DECISION_AT[session], later],
        decision_key=lambda value: value,
    )
    grouped = attribute_evaluation_coverage(
        predictions,
        labels,
        expected_instruments=["AAPL", "MSFT"],
        expected_decisions=[session],
        decision_key=lambda value: value.date(),
    )

    assert default.summary == identity.summary
    assert [row["rank_ic"] for row in default.summary["rank_ic_by_decision"]] == [None, None]
    assert grouped.summary["rank_ic_by_decision"][0]["rank_ic"] == pytest.approx(1.0)
    assert grouped.summary["expected_date_count"] == 1


def test_signal_spec_hash_is_stable_and_direction_sensitive():
    long = DirectSignalSpec(factor_name="mom63", score_multiplier=1, version="direct-factor-v1")
    short = DirectSignalSpec(factor_name="mom63", score_multiplier=-1, version="direct-factor-v1")
    assert signal_spec_hash(long) == signal_spec_hash(
        DirectSignalSpec(factor_name="mom63", score_multiplier=1, version="direct-factor-v1")
    )
    assert signal_spec_hash(long) != signal_spec_hash(short)


@pytest.mark.parametrize("value", [0, 2, 1.0, True])
def test_direct_signal_spec_rejects_non_literal_multipliers(value):
    with pytest.raises(ValidationError):
        DirectSignalSpec(factor_name="mom63", score_multiplier=value, version="direct-factor-v1")
