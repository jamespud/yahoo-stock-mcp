"""Cross-sectional scope marker; execution lives in the engine to share one aligned frame."""
from __future__ import annotations

from .operators import OPERATORS


def is_cross_section(operator: str) -> bool:
    return OPERATORS[operator].scope == "cross_section"
