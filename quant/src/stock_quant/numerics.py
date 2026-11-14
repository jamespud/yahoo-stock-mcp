"""Versioned binary64 summation, independent of Python's evolving sum algorithm."""
from collections import deque
from math import isfinite


def ordered_sum(values) -> float:
    total = 0.
    for value in values:
        total += value
    return total


class RollingState:
    """Causal scalar recurrence; retains only the indicator's seed/window."""
    def __init__(self, operator: str, period: int):
        self.operator, self.period = operator, period
        self.reset()

    def reset(self):
        self.window = deque(maxlen=self.period)
        self.gains, self.losses = deque(maxlen=self.period), deque(maxlen=self.period)
        self.smooth = self.gain = self.loss = self.previous = None

    def push(self, value):
        if value is None:
            self.reset()
            return None
        self.window.append(value)
        period, result = self.period, None
        if self.operator == "sma" and len(self.window) == period:
            result = ordered_sum(self.window) / period
        elif self.operator == "ema" and len(self.window) == period:
            self.smooth = ordered_sum(self.window) / period if self.smooth is None else self.smooth + (value - self.smooth) * 2 / (period + 1)
            result = self.smooth
        elif self.operator == "rsi" and self.previous is not None:
            delta = value - self.previous
            self.gains.append(max(delta, 0.))
            self.losses.append(max(-delta, 0.))
            if len(self.gains) == period:
                self.gain = ordered_sum(self.gains) / period if self.gain is None else (self.gain * (period - 1) + self.gains[-1]) / period
                self.loss = ordered_sum(self.losses) / period if self.loss is None else (self.loss * (period - 1) + self.losses[-1]) / period
                result = (50. if self.gain == 0 else 100.) if self.loss == 0 else 100 - 100 / (1 + self.gain / self.loss)
        self.previous = value
        return result if result is None or isfinite(result) else None
