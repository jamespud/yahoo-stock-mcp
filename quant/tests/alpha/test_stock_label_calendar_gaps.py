from stock_quant.alpha.models.labels import stock_forward_labels


def _row(day, close=100.0, volume=1000.0):
    from datetime import datetime, timedelta, timezone
    start = datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(days=day)
    return {"instrument_id": "AAPL", "start_at": start, "end_at": start + timedelta(hours=6),
            "available_at": start + timedelta(hours=6, minutes=1), "open": close, "high": close + 1,
            "low": close - 1, "close": close, "volume": volume, "adjustment": "raw",
            "source": "fixture", "source_version": "v1", "currency": "USD"}


def test_missing_session_does_not_silently_extend_holding_period():
    labels = stock_forward_labels([_row(day) for day in (0, 1, 2, 3, 5, 6, 7, 8, 9, 10)], horizon=5)
    assert labels[0].label_value is None
    assert "MISSING_LABEL_SESSIONS" in labels[0].label_quality


def test_complete_observed_path_matures_normally():
    labels = stock_forward_labels([_row(day) for day in range(10)], horizon=5)
    assert labels[0].label_value is not None
    assert labels[0].label_quality == []
