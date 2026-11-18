"""Target weights remain separate from actual account positions."""
from __future__ import annotations


def equal_target_weights(ranked: list[dict], *, max_name_weight: float = 0.05, max_gross_exposure: float = 1.0) -> dict[str, float]:
    if not ranked:
        return {}
    if not 0 < max_name_weight <= 1 or not 0 < max_gross_exposure <= 1:
        raise ValueError("Invalid weight constraints")
    equal = min(max_name_weight, max_gross_exposure / len(ranked))
    return {row["instrument_id"]: equal for row in ranked}
