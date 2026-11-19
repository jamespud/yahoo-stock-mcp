from datetime import datetime, timedelta, timezone

import numpy as np

from stock_quant.alpha.models.evaluation_coverage import attribute_evaluation_coverage
from stock_quant.alpha.models.labels import LabelRow

D1 = datetime(2024, 1, 2, 21, 5, tzinfo=timezone.utc)
D2 = datetime(2024, 1, 3, 21, 5, tzinfo=timezone.utc)
D3 = datetime(2024, 1, 4, 21, 5, tzinfo=timezone.utc)
D4 = datetime(2024, 1, 5, 21, 5, tzinfo=timezone.utc)


def _pred(symbol, decision, value):
    return {"instrument_id": symbol, "decision_at": decision, "prediction_value": value}


def _label(symbol, decision, value, quality=None):
    return LabelRow(
        feature_decision_at=decision,
        entry_at=decision + timedelta(days=1),
        exit_at=decision + timedelta(days=6),
        label_available_at=decision + timedelta(days=6),
        label_value=value,
        label_quality=quality or [],
        instrument_id=symbol,
        label_name="stock_forward_return",
    )


def test_unmatched_prediction_is_counted():
    result = attribute_evaluation_coverage(
        [_pred("AAPL", D1, 0.1)],
        [_label("AAPL", D1, 0.01), _label("MSFT", D1, 0.02)],
        expected_instruments=["AAPL", "MSFT"],
        expected_decisions=[D1],
    ).summary
    assert result["excluded_samples_by_reason"]["NO_PREDICTION"] == 1
    assert result["expected_opportunities"] == 2
    assert result["valid_matched_opportunities"] == 0
    assert result["excluded_opportunities"] == 2
    assert result["conservation_holds"] is True


def test_missing_label_is_counted():
    result = attribute_evaluation_coverage(
        [_pred("AAPL", D1, 0.1), _pred("MSFT", D1, 0.2)],
        [_label("AAPL", D1, 0.01)],
        expected_instruments=["AAPL", "MSFT"],
        expected_decisions=[D1],
    ).summary
    assert result["excluded_samples_by_reason"]["NO_LABEL"] == 1


def test_invalid_label_is_counted():
    result = attribute_evaluation_coverage(
        [_pred("AAPL", D1, 0.1), _pred("MSFT", D1, 0.2)],
        [_label("AAPL", D1, 0.01), _label("MSFT", D1, None, ["INVALID_SOURCE"])],
        expected_instruments=["AAPL", "MSFT"],
        expected_decisions=[D1],
    ).summary
    assert result["excluded_samples_by_reason"]["INVALID_LABEL"] == 1


def test_sample_level_counts_reconcile():
    result = attribute_evaluation_coverage(
        [_pred("AAPL", D1, 0.1), _pred("MSFT", D1, 0.2), _pred("AAPL", D2, 0.3)],
        [_label("AAPL", D1, 0.01), _label("MSFT", D1, 0.02), _label("AAPL", D2, 0.03)],
        expected_instruments=["AAPL", "MSFT"],
        expected_decisions=[D1, D2],
    ).summary
    assert result["expected_opportunities"] == result["valid_matched_opportunities"] + result["excluded_opportunities"]
    assert sum(result["excluded_samples_by_reason"].values()) == result["excluded_opportunities"]


def test_date_level_counts_reconcile():
    result = attribute_evaluation_coverage(
        [
            _pred("AAPL", D1, 0.1), _pred("MSFT", D1, 0.2),
            _pred("AAPL", D2, 0.3), _pred("MSFT", D2, 0.4),
            _pred("AAPL", D3, 0.5),
        ],
        [
            _label("AAPL", D1, 0.01), _label("MSFT", D1, 0.02),
            _label("AAPL", D2, 0.03), _label("MSFT", D2, 0.04),
            _label("AAPL", D3, 0.05), _label("MSFT", D3, 0.06),
        ],
        expected_instruments=["AAPL", "MSFT"],
        expected_decisions=[D1, D2, D3, D4],
    ).summary
    assert result["expected_date_count"] == 4
    assert result["valid_ic_date_count"] == 2
    assert result["invalid_ic_date_count"] == 2
    assert result["expected_date_count"] == result["valid_ic_date_count"] + result["invalid_ic_date_count"]
    assert sum(result["excluded_dates_by_reason"].values()) == result["invalid_ic_date_count"]


def test_no_double_count_in_exclusion_categories():
    result = attribute_evaluation_coverage(
        [_pred("AAPL", D1, 0.1), _pred("MSFT", D1, 0.1)],
        [_label("AAPL", D1, 0.01), _label("MSFT", D1, 0.02)],
        expected_instruments=["AAPL", "MSFT"],
        expected_decisions=[D1],
    ).summary
    assert result["valid_matched_opportunities"] == 0
    assert result["excluded_opportunities"] == 2
    assert result["excluded_samples_by_reason"] == {"CONSTANT_PREDICTION": 2}
    assert result["secondary_flags_by_reason"].get("CONSTANT_PREDICTION", {}).get("CONSTANT_LABEL") is None
    assert sum(result["excluded_samples_by_reason"].values()) == 2


def test_nonfinite_and_universe_categories_are_explicit():
    result = attribute_evaluation_coverage(
        [_pred("AAPL", D1, np.inf), _pred("MSFT", D1, 0.2)],
        [_label("AAPL", D1, 0.01), _label("MSFT", D1, 0.02)],
        expected_instruments=["AAPL", "MSFT"], expected_decisions=[D1],
        eligible_universe=["AAPL"],
    ).summary
    assert result["excluded_samples_by_reason"]["OUTSIDE_ELIGIBLE_UNIVERSE"] == 1
    assert result["excluded_samples_by_reason"]["NONFINITE_PREDICTION"] == 1
