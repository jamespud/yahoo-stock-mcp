"""Historical realized volatility without implicit fills or forward data."""
from __future__ import annotations

import math


def realized_volatility(closes: list[float], *, annualization: int = 252) -> float | None:
    if len(closes) < 3:
        return None
    returns = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
    return math.sqrt(variance * annualization)
