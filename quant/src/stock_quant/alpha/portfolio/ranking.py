"""Deterministic ranking of frozen prediction rows."""
from __future__ import annotations

import math
from collections import defaultdict


def rank_all_predictions(rows: list[dict], top_k: int = 20) -> dict:
    groups = defaultdict(list)
    for row in rows:
        value = row.get("prediction_value")
        if row.get("eligible", True) and value is not None and math.isfinite(float(value)):
            groups[row["decision_at"]].append(row)
    return {
        decision: sorted(values, key=lambda row: (-float(row["prediction_value"]), row["instrument_id"]))[:top_k]
        for decision, values in sorted(groups.items())
    }


def rank_predictions(rows: list[dict], top_k: int = 20) -> list[dict]:
    groups = defaultdict(list)
    for row in rows:
        value = row.get("prediction_value")
        if row.get("eligible", True) and value is not None and math.isfinite(float(value)):
            groups[row["decision_at"]].append(row)
    if len({row["decision_at"] for row in rows}) != 1:
        raise ValueError("rank_predictions expects one decision time; use rank_all_predictions")
    values = next(iter(groups.values()), [])
    return sorted(values, key=lambda row: (-float(row["prediction_value"]), row["instrument_id"]))[:top_k]
