import math

import numpy as np
import pytest

from stock_quant.alpha.models.evaluation import metrics, rank_ic_by_decision

D1, D2, D3 = "2024-01-02", "2024-01-03", "2024-01-04"


def test_constant_predictions_produce_null_ic():
    records = rank_ic_by_decision(
        np.array([1.0, 2.0, 3.0]),
        np.array([0.5, 0.5, 0.5]),
        [D1, D1, D1],
    )
    assert records[0]["rank_ic"] is None
    assert "CONSTANT_PREDICTION" in records[0]["exclusion_reasons"]
    assert records[0]["decision_at"] == D1


def test_invalid_ic_day_is_retained():
    records = rank_ic_by_decision(
        np.array([1.0, 2.0]),
        np.array([0.1, 0.2]),
        [D1, D1],
        expected_decisions=[D1, D2],
    )
    assert [row["decision_at"] for row in records] == [D1, D2]
    assert records[1]["rank_ic"] is None
    assert "NO_OBSERVATIONS" in records[1]["exclusion_reasons"]


def test_missing_decision_date_reduces_date_coverage():
    result = metrics(
        np.array([1.0, 2.0, 3.0, 4.0]),
        np.array([0.1, 0.2, 0.3, 0.4]),
        [D1, D1, D2, D2],
        expected_decisions=[D1, D2, D3],
    )
    assert result["expected_ic_date_count"] == 3
    assert result["valid_ic_date_count"] == 2
    assert result["invalid_ic_date_count"] == 1
    assert result["valid_ic_date_coverage"] == pytest.approx(2 / 3)


def test_prediction_coverage_differs_from_ic_coverage():
    result = metrics(
        np.array([1.0, 2.0, 3.0, 4.0]),
        np.array([0.1, 0.2, 0.5, 0.5]),
        [D1, D1, D2, D2],
        expected_decisions=[D1, D2],
    )
    assert result["prediction_coverage"] == 1.0
    assert result["valid_label_coverage"] == 1.0
    assert result["matched_prediction_label_coverage"] == 1.0
    assert result["valid_ic_date_coverage"] == pytest.approx(0.5)
    assert result["valid_ic_date_count"] == 1
    assert result["invalid_ic_date_count"] == 1


def test_ic_aggregation_preserves_null_semantics():
    result = metrics(
        np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0]),
        np.array([0.1, 0.2, 0.6, 0.7, 9.0, 9.0]),
        [D1, D1, D2, D2, D3, D3],
        expected_decisions=[D1, D2, D3],
    )
    assert result["rank_ic"] == pytest.approx([1.0, 1.0])
    assert result["rank_ic_mean"] == pytest.approx(1.0)
    assert result["valid_ic_date_count"] == 2
    assert result["invalid_ic_date_count"] == 1
    assert result["rank_ic_by_decision"][2]["rank_ic"] is None
    assert math.isnan(float("nan")) is True
