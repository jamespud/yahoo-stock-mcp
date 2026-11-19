import numpy as np
import pytest

from stock_quant.alpha.models.evaluation import rank_ic_by_decision


def test_rank_ic_by_decision_returns_traceable_and_ordered_records():
    records = rank_ic_by_decision(
        [1.0, 2.0, 3.0, 4.0, np.nan, 5.0, 6.0],
        [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7],
        ["2024-01-01", "2024-01-01", "2024-01-02", "2024-01-02", "2024-01-02", "2024-01-03", "2024-01-03"],
    )
    assert [row["decision_at"] for row in records] == ["2024-01-01", "2024-01-02", "2024-01-03"]
    assert records[0]["rank_ic"] == pytest.approx(1.0)
    assert records[0]["valid_pair_count"] == 2
    assert records[1]["rank_ic"] == pytest.approx(1.0)
    assert records[1]["excluded_count"] == 0
    assert records[2]["rank_ic"] == pytest.approx(1.0)
    assert records[2]["eligible_count"] == 2


def test_rank_ic_null_for_constant_or_small_cross_section():
    records = rank_ic_by_decision(
        [1.0, 1.0, 2.0],
        [0.1, 0.2, 0.3],
        ["2024-01-01", "2024-01-01", "2024-01-02"],
    )
    assert records[0]["rank_ic"] is None
    assert records[0]["reason"] == "CONSTANT_ARRAY"
    assert records[1]["rank_ic"] is None
    assert records[1]["reason"] == "INSUFFICIENT_VALID_PAIRS"
    assert records[1]["excluded_count"] == 1
