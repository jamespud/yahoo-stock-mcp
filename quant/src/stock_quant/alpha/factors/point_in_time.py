"""As-of helpers for factor inputs."""
from __future__ import annotations

from datetime import datetime


def available_by(rows: list[dict], decision_at: datetime) -> list[dict]:
    return [row for row in rows if row["end_at"] <= decision_at and row["available_at"] <= decision_at]
