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
    evaluate_weekly_signal,
    expected_session_dates,
    moving_block_bootstrap,
    regime_breakdown,
    sign_consistency,
    signal_spec_hash,
    stability_by_subperiod,
    stability_by_year,
    top20_forward_diagnostics,
    weekly_last_session_dates,
)

UTC = timezone.utc
SESSIONS = [date(2024, 1, day) for day in (2, 3, 4, 5)]
DECISION_AT = {
    session: datetime.combine(session, time(21, 5), tzinfo=UTC)
    for session in SESSIONS
}


def _decision_at(session: date) -> datetime:
    return DECISION_AT.get(session, datetime.combine(session, time(21, 5), tzinfo=UTC))


def _feature(session: date, instrument: str, value: float | None, *, factor_name="mom63") -> dict:
    return {
        "decision_at": _decision_at(session),
        "instrument_id": instrument,
        "factor_name": factor_name,
        "factor_value": value,
        "feature_available_at": _decision_at(session),
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
    decision_at = _decision_at(session)
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


def _spec(multiplier: int = 1) -> DirectSignalSpec:
    return DirectSignalSpec(factor_name="mom63", score_multiplier=multiplier, version="direct-factor-v1")


def test_weekly_last_session_dates_uses_actual_holiday_shortened_week():
    sessions = [
        date(2009, 6, 29),
        date(2009, 6, 30),
        date(2009, 7, 1),
        date(2009, 7, 2),
        date(2009, 7, 6),
        date(2009, 7, 7),
        date(2009, 7, 8),
        date(2009, 7, 9),
        date(2009, 7, 10),
    ]

    assert weekly_last_session_dates(sessions) == [date(2009, 7, 2), date(2009, 7, 10)]


def test_weekly_missing_final_session_row_does_not_backfill_an_earlier_session():
    sessions = [
        date(2009, 7, 1), date(2009, 7, 2), date(2009, 7, 3),
        date(2009, 7, 6), date(2009, 7, 7), date(2009, 7, 8),
    ]
    instruments = ["AAPL", "MSFT", "NVDA"]
    features = []
    labels = []
    for session in sessions:
        for index, instrument in enumerate(instruments):
            labels.append(_label(session, instrument, 0.01 * (index + 1)))
    for index, instrument in enumerate(instruments):
        features.append(_feature(date(2009, 7, 2), instrument, float(index + 1)))
        features.append(_feature(date(2009, 7, 8), instrument, float(index + 1)))

    result = evaluate_weekly_signal(
        features,
        labels,
        expected_instruments=instruments,
        expected_sessions=sessions,
        spec=_spec(),
    )

    records = result["weekly_rank_ic"]
    assert [record["decision_at"] for record in records] == ["2009-07-03", "2009-07-08"]
    assert records[0]["rank_ic"] is None
    assert "NO_PREDICTION" in records[0]["exclusion_reasons"]
    assert records[1]["rank_ic"] is not None
    summary = result["summary"]
    assert summary["weekly_decision_count"] == 2
    assert summary["weekly_valid_decision_count"] == 1
    assert summary["weekly_sample_count"] == 3
    assert summary["weekly_prediction_coverage"] == pytest.approx(0.5)
    assert summary["weekly_label_coverage"] == pytest.approx(1.0)
    assert summary["weekly_coverage"]["expected_date_count"] == 2


def test_weekly_denominators_are_separate_from_daily_denominators():
    sessions = [
        date(2009, 7, 1), date(2009, 7, 2), date(2009, 7, 3),
        date(2009, 7, 6), date(2009, 7, 7), date(2009, 7, 8),
    ]
    instruments = ["AAPL", "MSFT", "NVDA"]
    features = []
    labels = []
    for session in sessions:
        for index, instrument in enumerate(instruments):
            features.append(_feature(session, instrument, float(index + 1)))
            labels.append(_label(session, instrument, float(index + 1) / 10))

    daily = evaluate_direct_signal(
        features,
        labels,
        expected_instruments=instruments,
        expected_sessions=sessions,
        spec=_spec(),
    )
    weekly = evaluate_weekly_signal(
        features,
        labels,
        expected_instruments=instruments,
        expected_sessions=sessions,
        spec=_spec(),
    )

    assert daily["coverage"]["expected_date_count"] == 6
    assert daily["coverage"]["expected_opportunities"] == 18
    assert weekly["summary"]["weekly_coverage"]["expected_date_count"] == 2
    assert weekly["summary"]["weekly_coverage"]["expected_opportunities"] == 6
    assert weekly["summary"]["weekly_sample_count"] == 6
    assert weekly["summary"]["weekly_decision_count"] == 2
    assert weekly["summary"]["weekly_valid_decision_count"] == 2


def test_top20_membership_uses_finite_signals_and_ascending_instrument_ties():
    session = date(2009, 7, 2)
    instruments = ["S00", *[f"S{index:02d}" for index in range(1, 21)], "S21"]
    values = {"S00": float("nan"), "S21": 2.0}
    values.update({f"S{index:02d}": 1.0 for index in range(1, 21)})
    features = [_feature(session, instrument, values[instrument]) for instrument in instruments]
    labels = [_label(session, instrument, 0.01) for instrument in instruments]

    result = top20_forward_diagnostics(
        features,
        labels,
        expected_instruments=instruments,
        expected_sessions=[session],
        spec=_spec(),
    )

    row = result["top20_forward_labels"][0]
    assert row["top20_member_ids"] == ["S21", *[f"S{index:02d}" for index in range(1, 20)]]
    assert row["top20_member_count"] == 20
    assert row["top20_forward_label_mean"] == pytest.approx(0.01)
    assert "S00" not in row["top20_member_ids"]


def test_top20_missing_selected_labels_do_not_change_membership():
    session = date(2009, 7, 2)
    instruments = [f"S{index:02d}" for index in range(1, 22)]
    values = {f"S{index:02d}": 2.0 for index in range(1, 21)} | {"S21": 1.0}
    features = [_feature(session, instrument, values[instrument]) for instrument in instruments]
    all_labels = [_label(session, instrument, 0.01 if instrument != "S21" else 0.10) for instrument in instruments]
    rest_only_labels = [_label(session, "S21", 0.10)]

    complete = top20_forward_diagnostics(
        features,
        all_labels,
        expected_instruments=instruments,
        expected_sessions=[session],
        spec=_spec(),
    )["top20_forward_labels"][0]
    missing = top20_forward_diagnostics(
        features,
        rest_only_labels,
        expected_instruments=instruments,
        expected_sessions=[session],
        spec=_spec(),
    )["top20_forward_labels"][0]

    assert missing["top20_member_ids"] == complete["top20_member_ids"]
    assert missing["top20_forward_label_count"] == 0
    assert missing["top20_forward_label_mean"] is None
    assert missing["universe_forward_label_mean"] == pytest.approx(0.10)
    assert missing["top20_universe_spread"] is None
    assert missing["top20_rest_spread"] is None
    assert "MISSING_TOP20_LABELS" in missing["exclusion_reasons"]


def test_top20_fewer_than_20_names_keeps_decision_with_reason():
    session = date(2009, 7, 2)
    instruments = ["AAPL", "MSFT", "NVDA", "JPM"]
    result = top20_forward_diagnostics(
        [_feature(session, instrument, float(index + 1)) for index, instrument in enumerate(instruments)],
        [_label(session, instrument, float(index + 1) / 100) for index, instrument in enumerate(instruments)],
        expected_instruments=instruments,
        expected_sessions=[session],
        spec=_spec(),
    )

    row = result["top20_forward_labels"][0]
    assert row["top20_member_ids"] == list(reversed(instruments))
    assert row["top20_member_count"] == 4
    assert row["top20_forward_label_mean"] is None
    assert "INSUFFICIENT_TOP20_NAMES" in row["exclusion_reasons"]
    assert result["summary"]["decision_count"] == 1
    assert result["summary"]["insufficient_name_count"] == 1


def test_top20_turnover_uses_intersection_over_complete_groups_and_is_null_otherwise():
    weeks = [
        [date(2009, 7, 1), date(2009, 7, 2), date(2009, 7, 3)],
        [date(2009, 7, 6), date(2009, 7, 7), date(2009, 7, 8)],
        [date(2009, 7, 13), date(2009, 7, 14)],
    ]
    instruments = [f"A{index:02d}" for index in range(1, 26)]
    features = []
    labels = []
    for week_index, week in enumerate(weeks):
        active = instruments if week_index < 2 else instruments[:19]
        for index, instrument in enumerate(active):
            if week_index == 0:
                value = 2.0 if index < 20 else 1.0
            elif week_index == 1:
                value = 1.0 if index < 5 else 2.0
            else:
                value = 2.0
            features.append(_feature(week[-1], instrument, value))
            labels.append(_label(week[-1], instrument, 0.01))

    result = top20_forward_diagnostics(
        features,
        labels,
        expected_instruments=instruments,
        expected_sessions=[session for week in weeks for session in week],
        spec=_spec(),
    )

    rows = result["top20_forward_labels"]
    assert rows[0]["top20_member_ids"] == instruments[:20]
    assert rows[1]["top20_member_ids"] == instruments[5:]
    assert rows[0]["turnover_proxy"] is None
    assert rows[1]["turnover_proxy"] == pytest.approx(0.25)
    assert rows[2]["turnover_proxy"] is None
    assert "INSUFFICIENT_TOP20_NAMES" in rows[2]["exclusion_reasons"]
    assert result["summary"]["turnover_proxy"] == pytest.approx(0.25)
    assert result["summary"]["turnover_transition_count"] == 1


def test_top20_analysis_label_is_present_on_every_payload():
    session = date(2009, 7, 2)
    instruments = [f"S{index:02d}" for index in range(1, 22)]
    result = top20_forward_diagnostics(
        [_feature(session, instrument, float(index)) for index, instrument in enumerate(instruments)],
        [_label(session, instrument, float(index) / 100) for index, instrument in enumerate(instruments)],
        expected_instruments=instruments,
        expected_sessions=[session],
        spec=_spec(),
    )

    payloads = [result, result["summary"], *result["top20_forward_labels"]]
    assert all(payload["analysis_label"] == "predictive_label_analysis_not_executable_backtest" for payload in payloads)


def test_stability_by_year_retains_periods_timestamps_denominators_and_null_ics():
    records = [
        {
            "decision_at": "2009-12-30T21:05:00+00:00", "rank_ic": 0.2,
            "expected_count": 3, "valid_count": 3, "excluded_count": 0,
            "exclusion_reasons": [],
        },
        {
            "decision_at": "2009-12-31T21:05:00+00:00", "rank_ic": None,
            "expected_count": 3, "valid_count": 1, "excluded_count": 2,
            "exclusion_reasons": ["NO_LABEL"],
        },
        {
            "decision_at": "2010-01-04T21:05:00+00:00", "rank_ic": 0.4,
            "expected_count": 4, "valid_count": 4, "excluded_count": 0,
            "exclusion_reasons": [],
        },
    ]

    annual = stability_by_year(records)

    assert [record["year"] for record in annual] == [2009, 2010]
    assert annual[0]["period_start"] == "2009-12-30T21:05:00+00:00"
    assert annual[0]["period_end"] == "2009-12-31T21:05:00+00:00"
    assert annual[0]["decision_count"] == 2
    assert annual[0]["expected_count"] == 6
    assert annual[0]["valid_count"] == 4
    assert annual[0]["excluded_count"] == 2
    assert annual[0]["valid_ic_decision_count"] == 1
    assert annual[0]["invalid_ic_decision_count"] == 1
    assert annual[0]["rank_ic_mean"] == pytest.approx(0.2)
    assert [row["rank_ic"] for row in annual[0]["decisions"]] == [0.2, None]
    assert annual[0]["decisions"][1]["expected_count"] == 3


def test_stability_by_subperiod_keeps_chronological_chunks_and_null_records():
    records = [
        {
            "decision_at": f"2010-01-0{index + 1}T21:05:00+00:00",
            "rank_ic": None if index == 2 else index / 10,
            "expected_count": 2,
            "valid_count": 1 if index == 2 else 2,
            "excluded_count": 1 if index == 2 else 0,
            "exclusion_reasons": ["NO_LABEL"] if index == 2 else [],
        }
        for index in range(6)
    ]

    periods = stability_by_subperiod(records, period_count=3)

    assert [period["period_index"] for period in periods] == [0, 1, 2]
    assert [period["decision_count"] for period in periods] == [2, 2, 2]
    assert periods[1]["decisions"][0]["rank_ic"] is None
    assert periods[1]["rank_ic_mean"] == pytest.approx(0.3)


def test_regime_breakdown_uses_only_trailing_63_session_benchmark_data_available_by_decision():
    sessions = [
        date(2024, 1, 2) + timedelta(days=index)
        for index in range(65)
    ]
    benchmark_rows = []
    for index, session in enumerate(sessions):
        decision_at = datetime.combine(session, time(21, 5), tzinfo=UTC)
        benchmark_rows.append({
            "instrument_id": "SPY",
            "start_at": datetime.combine(session, time(14, 30), tzinfo=UTC),
            "end_at": datetime.combine(session, time(21, 0), tzinfo=UTC),
            "available_at": decision_at if index != 64 else decision_at + timedelta(hours=1),
            "close": 100.0 + index,
        })
    ic_rows = [
        {
            "decision_at": (datetime.combine(sessions[62], time(21, 5), tzinfo=UTC)).isoformat(),
            "rank_ic": None, "expected_count": 4, "valid_count": 1, "excluded_count": 3,
            "exclusion_reasons": ["NO_LABEL"],
        },
        {
            "decision_at": (datetime.combine(sessions[63], time(21, 5), tzinfo=UTC)).isoformat(),
            "rank_ic": 0.3, "expected_count": 4, "valid_count": 4, "excluded_count": 0,
            "exclusion_reasons": [],
        },
        {
            "decision_at": (datetime.combine(sessions[64], time(21, 5), tzinfo=UTC)).isoformat(),
            "rank_ic": 0.1, "expected_count": 4, "valid_count": 4, "excluded_count": 0,
            "exclusion_reasons": [],
        },
    ]

    regimes = regime_breakdown(ic_rows, benchmark_rows, benchmark_symbol="SPY")
    by_name = {record["regime"]: record for record in regimes}

    assert [row["decision_at"][:10] for row in by_name["UP"]["decisions"]] == [sessions[63].isoformat()]
    assert by_name["UP"]["rank_ic_mean"] == pytest.approx(0.3)
    assert [row["decision_at"][:10] for row in by_name["UNKNOWN"]["decisions"]] == [
        sessions[62].isoformat(), sessions[64].isoformat(),
    ]
    assert by_name["UNKNOWN"]["decisions"][0]["rank_ic"] is None
    assert {
        by_name["UNKNOWN"]["decisions"][0]["regime_reason"],
        by_name["UNKNOWN"]["decisions"][1]["regime_reason"],
    } == {"INCOMPLETE_63_SESSION_WINDOW", "BENCHMARK_NOT_AVAILABLE_BY_DECISION"}


def test_sign_consistency_reports_direction_without_dropping_null_periods():
    positive = sign_consistency([
        {"period": "2009", "rank_ic": 0.1},
        {"period": "2010", "rank_ic": 0.2},
    ])
    mixed = sign_consistency([
        {"period": "2009", "rank_ic": 0.1},
        {"period": "2010", "rank_ic": None},
        {"period": "2011", "rank_ic": -0.2},
    ])

    assert positive["status"] == "CONSISTENT_POSITIVE"
    assert positive["positive_count"] == 2
    assert positive["null_count"] == 0
    assert mixed["status"] == "MIXED"
    assert mixed["period_count"] == 3
    assert mixed["valid_period_count"] == 2
    assert mixed["null_count"] == 1


def test_moving_block_bootstrap_is_reproducible_and_reuses_paired_draws_for_delta():
    weekly = [
        {
            "decision_at": f"2010-01-{index + 1:02d}T21:05:00+00:00",
            "rank_ic": 0.01 * (index - 4),
            "paired_rank_ic": -0.01 * (index - 4),
        }
        for index in range(12)
    ]

    first = moving_block_bootstrap(weekly)
    second = moving_block_bootstrap(weekly)

    assert first == second
    assert first["status"] == "TESTED"
    assert first["config"] == {
        "block_weeks": 4,
        "draws": 2000,
        "seed": 20261120,
        "rng": "PCG64",
        "interval": 0.95,
        "percentile_method": "linear",
    }
    assert first["finite_draw_count"] == 2000
    assert first["rank_ic"]["ci_lower"] is not None
    assert first["paired_delta"]["observed_mean"] == pytest.approx(2 * first["rank_ic"]["observed_mean"])
    assert first["paired_delta"]["ci_lower"] == pytest.approx(2 * first["rank_ic"]["ci_lower"])
    assert first["paired_delta"]["ci_upper"] == pytest.approx(2 * first["rank_ic"]["ci_upper"])


def test_moving_block_bootstrap_returns_not_tested_below_each_hard_threshold():
    seven_weeks = [
        {"decision_at": f"2010-01-{index + 1:02d}T21:05:00+00:00", "rank_ic": 0.1}
        for index in range(7)
    ]
    seven_valid = [
        {"decision_at": f"2010-02-{index + 1:02d}T21:05:00+00:00", "rank_ic": None if index == 0 else 0.1}
        for index in range(8)
    ]
    insufficient_finite_draws = [
        {"decision_at": f"2010-03-{index + 1:02d}T21:05:00+00:00", "rank_ic": None if index == 5 else 0.1}
        for index in range(10)
    ]

    too_short = moving_block_bootstrap(seven_weeks)
    few_valid = moving_block_bootstrap(seven_valid)
    few_finite = moving_block_bootstrap(insufficient_finite_draws)

    assert (too_short["status"], too_short["reason"]) == ("NOT_TESTED", "FEWER_THAN_EIGHT_WEEKS")
    assert (few_valid["status"], few_valid["reason"]) == ("NOT_TESTED", "FEWER_THAN_EIGHT_VALID_ICS")
    assert few_finite["status"] == "NOT_TESTED"
    assert few_finite["reason"] == "FEWER_THAN_1900_FINITE_DRAWS"
    assert few_finite["finite_draw_count"] < 1900
    assert few_finite["rank_ic"]["ci_lower"] is None
