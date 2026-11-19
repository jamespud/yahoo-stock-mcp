"""Versioned explicit trading intervals; never infer holidays from observed bars."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, Field, model_validator

from .compiler import digest
from .contracts import StrictModel, Timeframe

SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}
MAX_EXPECTED = 100000
CALENDAR_PROVIDER_VERSION = "4.13.2"


class TradingInterval(StrictModel):
    start: AwareDatetime
    end: AwareDatetime

    @model_validator(mode="after")
    def order(self):
        if self.start >= self.end or (self.end - self.start).total_seconds() > 86400:
            raise ValueError("A trading interval must be positive and at most 24 hours")
        object.__setattr__(self, "start", self.start.astimezone(timezone.utc))
        object.__setattr__(self, "end", self.end.astimezone(timezone.utc))
        return self


class TradingSession(StrictModel):
    date: date
    intervals: tuple[TradingInterval, ...] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def order(self):
        intervals = tuple(sorted(self.intervals, key=lambda i: i.start))
        if any(a.end > b.start for a, b in zip(intervals, intervals[1:])):
            raise ValueError("Overlapping session intervals")
        if (intervals[-1].end - intervals[0].start).total_seconds() > 86400:
            raise ValueError("A trading session must fit within 24 hours")
        object.__setattr__(self, "intervals", intervals)
        return self


class CalendarSchedule(StrictModel):
    exchange: str = Field(min_length=1, max_length=64)
    timezone: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=128)
    provider: Literal["declared", "exchange_calendars"] = "declared"
    provider_version: str | None = Field(default=None, max_length=128)
    calendar_name: str | None = Field(default=None, max_length=64)
    start_date: date
    end_date: date
    sessions: tuple[TradingSession, ...] = Field(max_length=5000)
    long_calendar_shards: tuple[dict, ...] = ()
    calendar_validation_status: str | None = None

    @model_validator(mode="after")
    def coherent(self):
        try:
            zone = ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("Unknown exchange timezone") from error
        if not self.start_date <= self.end_date or (self.end_date - self.start_date).days > 3660:
            raise ValueError("Calendar coverage must be ordered and at most ten years")
        if self.provider == "exchange_calendars" and (not self.provider_version or not self.calendar_name):
            raise ValueError("Generated calendars require provider version/name")
        if self.provider == "exchange_calendars":
            import exchange_calendars as xc
            names = xc.get_calendar_names()
            if (self.provider_version != CALENDAR_PROVIDER_VERSION or self.exchange not in names or self.calendar_name not in names
                    or xc.resolve_alias(self.exchange) != xc.resolve_alias(self.calendar_name)):
                raise ValueError("Generated calendar metadata has incompatible provider version or exchange mapping")
        sessions = tuple(sorted(self.sessions, key=lambda s: s.date))
        if len({s.date for s in sessions}) != len(sessions):
            raise ValueError("Duplicate trading session date")
        for session in sessions:
            if not self.start_date <= session.date <= self.end_date:
                raise ValueError("Session is outside calendar coverage")
            # Trading date labels may differ from the local opening date for
            # overnight markets, but cannot alias an unrelated calendar date.
            if abs((session.intervals[0].start.astimezone(zone).date() - session.date).days) > 1:
                raise ValueError("Session label does not match exchange timezone/date")
        intervals = sorted((i for s in sessions for i in s.intervals), key=lambda i: i.start)
        if any(a.end > b.start for a, b in zip(intervals, intervals[1:])):
            raise ValueError("Overlapping trading sessions")
        object.__setattr__(self, "sessions", sessions)
        return self

    @property
    def id(self):
        return digest(self.model_dump(mode="json", exclude={"long_calendar_shards", "calendar_validation_status"}))


@dataclass(frozen=True)
class ExpectedInterval:
    start: datetime
    end: datetime


def expected_intervals(calendar: CalendarSchedule, timeframe: Timeframe) -> tuple[ExpectedInterval, ...]:
    result = []
    if timeframe == "1w":
        weeks = {}
        for session in calendar.sessions:
            year, week, _ = session.date.isocalendar()
            weeks.setdefault((year, week), []).append(session)
        for (year, week), sessions in sorted(weeks.items()):
            monday = date.fromisocalendar(year, week, 1)
            # Incomplete calendar coverage cannot establish that a Friday is
            # the last trading day of its ISO week.
            if (monday - calendar.start_date).days < 0 or (calendar.end_date - monday).days < 6:
                continue
            result.append(ExpectedInterval(sessions[0].intervals[0].start, sessions[-1].intervals[-1].end))
    else:
        for session in calendar.sessions:
            if timeframe == "1d":
                result.append(ExpectedInterval(session.intervals[0].start, session.intervals[-1].end))
            elif timeframe in SECONDS:
                duration = timedelta(seconds=SECONDS[timeframe])
                for interval in session.intervals:
                    current = interval.start
                    while current < interval.end:
                        # Avoid overflowing datetime.max on a final short bar.
                        following = current + min(duration, interval.end - current)
                        result.append(ExpectedInterval(current, following))
                        if len(result) > MAX_EXPECTED:
                            raise ValueError("RESOURCE_LIMIT_EXCEEDED: calendar exceeds 100000 intervals")
                        current = following
            else:
                raise ValueError("UNSUPPORTED_EXECUTION_MODEL: unsupported calendar timeframe")
    return tuple(sorted(result, key=lambda i: (i.start, i.end)))


def generate_calendar(*, calendar_name: str, exchange: str, start_date: str | date, end_date: str | date) -> CalendarSchedule:
    """Freeze library rules as explicit data, not historical announcement knowledge."""
    from importlib.metadata import version
    import exchange_calendars as xc
    import pandas as pd

    if version("exchange-calendars") != CALENDAR_PROVIDER_VERSION:
        raise ValueError("DATA_UNAVAILABLE: calendar provider version differs from accepted " + CALENDAR_PROVIDER_VERSION)
    begin = date.fromisoformat(start_date) if isinstance(start_date, str) else start_date
    finish = date.fromisoformat(end_date) if isinstance(end_date, str) else end_date
    if not begin <= finish or (finish - begin).days > 3660 or begin.year < 1900 or finish.year > 2100:
        raise ValueError("RESOURCE_LIMIT_EXCEEDED: calendar request must cover at most ten years within 1900..2100")
    names = xc.get_calendar_names()
    if calendar_name not in names or exchange not in names:
        raise ValueError("UNSUPPORTED_ASSET: calendar/exchange has no installed provider mapping")
    # Padding permits requests consisting only of holidays/weekends. It does not
    # extend the returned declared coverage and is never used as trading data.
    try:
        calendar = xc.get_calendar(calendar_name, start=begin - timedelta(days=7), end=finish + timedelta(days=7))
        mapped = xc.get_calendar(exchange, start=begin - timedelta(days=7), end=finish + timedelta(days=7))
        if mapped.name != calendar.name:
            raise ValueError("Calendar name does not resolve to the requested exchange")
    except (ValueError, OverflowError) as error:
        raise ValueError("DATA_UNAVAILABLE: provider cannot generate the requested exchange calendar") from error
    sessions = []
    for label, row in calendar.schedule.iterrows():
        if not begin <= label.date() <= finish:
            continue
        breaks = (row["break_start"], row["break_end"])
        if pd.isna(breaks[0]) != pd.isna(breaks[1]):
            raise ValueError("DATA_UNAVAILABLE: provider returned an incomplete trading break")
        if pd.isna(breaks[0]):
            intervals = [{"start": row["open"].to_pydatetime(), "end": row["close"].to_pydatetime()}]
        else:
            intervals = [{"start": row["open"].to_pydatetime(), "end": breaks[0].to_pydatetime()},
                         {"start": breaks[1].to_pydatetime(), "end": row["close"].to_pydatetime()}]
        sessions.append({"date": label.date(), "intervals": intervals})
    return CalendarSchedule(exchange=exchange, timezone=str(calendar.tz),
                            version=f"exchange_calendars:{version('exchange-calendars')}|pandas:{version('pandas')}|tzdata:{version('tzdata')}",
                            provider="exchange_calendars", provider_version=version("exchange-calendars"), calendar_name=calendar.name,
                            start_date=begin, end_date=finish, sessions=tuple(sessions))


def generate_long_calendar(*, calendar_name: str, exchange: str, start_date: str | date, end_date: str | date) -> CalendarSchedule:
    """Prepare >10-year research ranges as deterministic, non-overlapping calendar shards."""
    begin = date.fromisoformat(start_date) if isinstance(start_date, str) else start_date
    finish = date.fromisoformat(end_date) if isinstance(end_date, str) else end_date
    if not begin <= finish:
        raise ValueError("Calendar coverage must be ordered")
    if (finish - begin).days <= 3660:
        calendar = generate_calendar(calendar_name=calendar_name, exchange=exchange, start_date=begin, end_date=finish)
        shards = ({"calendar_name": calendar_name, "exchange": exchange, "start": begin.isoformat(), "end": finish.isoformat(), "provider_version": calendar.provider_version},)
        return calendar.model_copy(update={"long_calendar_shards": shards, "calendar_validation_status": "SINGLE_SHARD_VERIFIED"})
    shards: list[dict] = []
    sessions: list[TradingSession] = []
    current = begin
    provider_version = None
    while current <= finish:
        # Deterministic natural-year chunks avoid rolling provider state and
        # keep later inspections independently reproducible.
        chunk_end = min(date(current.year, 12, 31), finish)
        shard = generate_calendar(calendar_name=calendar_name, exchange=exchange, start_date=current, end_date=chunk_end)
        provider_version = shard.provider_version
        sessions.extend(shard.sessions)
        shards.append({"calendar_name": calendar_name, "exchange": exchange,
                       "start": shard.start_date.isoformat(), "end": shard.end_date.isoformat(),
                       "provider_version": shard.provider_version})
        current = date(current.year + 1, 1, 1)
    sorted_sessions = tuple(sorted(sessions, key=lambda s: s.date))
    if len({session.date for session in sorted_sessions}) != len(sorted_sessions):
        raise ValueError("Long calendar merge created duplicate trading sessions")
    first = generate_calendar(calendar_name=calendar_name, exchange=exchange, start_date=begin, end_date=begin)
    return first.model_copy(update={
        "start_date": begin,
        "end_date": finish,
        "sessions": sorted_sessions,
        "long_calendar_shards": tuple(shards),
        "calendar_validation_status": "MULTI_SHARD_MERGED",
    })
