"""Versioned trading-session index used to locate label entry and exit sessions."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class SessionPoint:
    session_date: date
    start_at: datetime
    end_at: datetime


class SessionLabelIndex:
    """Lookup expected sessions without falling through to the next observed bar."""

    def __init__(self, sessions):
        points = tuple(
            SessionPoint(
                session_date=session.date,
                start_at=_utc(session.intervals[0].start),
                end_at=_utc(session.intervals[-1].end),
            )
            for session in sorted(sessions, key=lambda item: item.date)
        )
        if len({point.session_date for point in points}) != len(points):
            raise ValueError("Duplicate session date in label calendar")
        self.points = points
        self._by_date = {point.session_date: index for index, point in enumerate(points)}
        self._by_start = {point.start_at: index for index, point in enumerate(points)}

    def index_for_bar(self, row: dict) -> int | None:
        start_at = _utc(row["start_at"])
        index = self._by_start.get(start_at)
        if index is not None:
            return index
        return self._by_date.get(start_at.date())

    def index_for_date(self, value: date) -> int | None:
        return self._by_date.get(value)

    def point(self, index: int) -> SessionPoint:
        if index < 0 or index >= len(self.points):
            raise IndexError(index)
        return self.points[index]

    def has(self, index: int) -> bool:
        return 0 <= index < len(self.points)
