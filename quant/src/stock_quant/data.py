"""Immutable observations and point-in-time slices, independent of provider transport."""
from __future__ import annotations

from bisect import bisect_right
from datetime import datetime, timezone
from typing import Literal, Protocol
from types import MappingProxyType

from pydantic import AwareDatetime, Field, model_validator

from .contracts import StrictModel, Timeframe
from .compiler import digest
from .actions import ACTION_ADAPTER, CorporateAction
from .calendar import CalendarSchedule


class MarketBar(StrictModel):
    symbol: str = Field(min_length=1)
    timeframe: Timeframe
    exchange: str = Field(min_length=1)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    start_time: AwareDatetime
    end_time: AwareDatetime
    available_at: AwareDatetime
    open: float = Field(gt=0, strict=True)
    high: float = Field(gt=0, strict=True)
    low: float = Field(gt=0, strict=True)
    close: float = Field(gt=0, strict=True)
    volume: float = Field(ge=0, strict=True)
    source: str = Field(min_length=1)
    adjustment: Literal["raw", "split", "total_return", "unknown"]
    dataset_version: str = Field(min_length=1)
    availability_policy: Literal["declared", "modeled_bar_end_plus_delay", "observed", "derived_constituent_availability"] = "declared"

    @model_validator(mode="after")
    def coherent(self):
        if not self.start_time < self.end_time <= self.available_at:
            raise ValueError("Expected start_time < end_time <= available_at")
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close) or self.low > self.high:
            raise ValueError("OHLC values are inconsistent")
        return self


class MarketDataProvider(Protocol):
    def capabilities(self) -> dict: ...
    def snapshot(self, symbols: list[str], timeframe: str, start: datetime, end: datetime) -> Snapshot: ...


class SnapshotLineage(StrictModel):
    parent_snapshot_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    symbol: str = Field(min_length=1, max_length=64)
    source_timeframe: Timeframe
    target_timeframe: Timeframe
    calendar_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    operator_version: Literal["session_ohlcv_v1"] = "session_ohlcv_v1"


class Snapshot:
    """Canonical snapshot; each series must have one version and adjustment policy.

    A symbol cannot alias multiple exchanges. Missing intervals are retained as gaps;
    history never fills gaps or substitutes another timeframe.
    """
    def __init__(self, bars: list[MarketBar], *, calendar_version: str, actions: list[CorporateAction | dict] | None = None,
                 calendars: list[CalendarSchedule | dict] | tuple[CalendarSchedule, ...] | None = None, lineage: SnapshotLineage | dict | None = None):
        if not calendar_version:
            raise ValueError("calendar_version is required")
        groups = {}
        identities = set()
        policies = {}
        exchanges = {}
        for candidate in bars:
            # Revalidate even model_copy/model_construct inputs before trusting them.
            bar = MarketBar.model_validate(candidate.model_dump())
            key = (bar.symbol, bar.timeframe)
            identity = (*key, bar.exchange, bar.start_time, bar.end_time)
            if identity in identities:
                raise ValueError("Duplicate bar identity")
            identities.add(identity)
            policy = (bar.source, bar.dataset_version, bar.adjustment, bar.availability_policy, bar.currency)
            if key in policies and policies[key] != policy:
                raise ValueError("Mixed source/version/adjustment in one series")
            policies[key] = policy
            if bar.symbol in exchanges and exchanges[bar.symbol] != bar.exchange:
                raise ValueError("Ambiguous symbol exchange")
            exchanges[bar.symbol] = bar.exchange
            groups.setdefault(key, []).append(bar)
        for rows in groups.values():
            ordered = sorted(rows, key=lambda b: (b.start_time, b.end_time))
            if any(a.end_time > b.start_time for a, b in zip(ordered, ordered[1:])):
                raise ValueError("Overlapping bars in one series")
        self._series = MappingProxyType({key: tuple(sorted(rows, key=lambda b: (b.available_at, b.end_time))) for key, rows in groups.items()})
        self._times = {key: tuple(b.available_at for b in rows) for key, rows in self._series.items()}
        canonical = []
        for key in sorted(self._series):
            for bar in self._series[key]:
                row = bar.model_dump(mode="json")
                for field in ("start_time", "end_time", "available_at"):
                    row[field] = getattr(bar, field).astimezone(timezone.utc).isoformat()
                canonical.append(row)
        parsed_actions = [ACTION_ADAPTER.validate_python(a.model_dump() if hasattr(a, "model_dump") else a) for a in (actions or [])]
        if len({a.id for a in parsed_actions}) != len(parsed_actions):
            raise ValueError("Duplicate corporate action ID")
        self._actions = tuple(sorted(parsed_actions, key=lambda a: (a.effective_at, a.symbol, a.id)))
        canonical_actions = []
        for action in self._actions:
            row = action.model_dump(mode="json")
            for field in ("effective_at", "available_at", "pay_at"):
                if field in row:
                    row[field] = getattr(action, field).astimezone(timezone.utc).isoformat()
            canonical_actions.append(row)
        parsed_calendars = [CalendarSchedule.model_validate(c.model_dump() if hasattr(c, "model_dump") else c) for c in (calendars or [])]
        if len(parsed_calendars) > 50 or len({c.exchange for c in parsed_calendars}) != len(parsed_calendars):
            raise ValueError("Expected at most 50 calendars with unique exchanges")
        self._calendars = tuple(sorted(parsed_calendars, key=lambda c: c.exchange))
        self._lineage = SnapshotLineage.model_validate(lineage.model_dump() if hasattr(lineage, "model_dump") else lineage) if lineage is not None else None
        identity = {"bars": canonical, "actions": canonical_actions, "calendar_version": calendar_version}
        if self._calendars:
            identity["calendars"] = [c.model_dump(mode="json") for c in self._calendars]
        if self._lineage:
            identity["lineage"] = self._lineage.model_dump(mode="json")
        self._id = digest(identity)
        self._calendar_version = calendar_version

    @property
    def calendars(self):
        return self._calendars

    @property
    def lineage(self):
        return self._lineage

    @property
    def actions(self):
        return self._actions

    @property
    def id(self):
        return self._id

    @property
    def calendar_version(self):
        return self._calendar_version

    @property
    def bars(self) -> tuple[MarketBar, ...]:
        return tuple(b for key in sorted(self._series) for b in self._series[key])

    def history(self, symbol: str, timeframe: str, now: datetime, bars: int) -> tuple[MarketBar, ...]:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Decision time must include timezone")
        if type(bars) is not int or not 1 <= bars <= 100000:
            raise ValueError("History window must be between 1 and 100000 bars")
        key = (symbol, timeframe)
        if key not in self._series:
            return ()
        stop = bisect_right(self._times[key], now)
        # Available observations are returned in market-time order, including delayed bars.
        return tuple(sorted(self._series[key][:stop], key=lambda b: b.end_time)[-bars:])
