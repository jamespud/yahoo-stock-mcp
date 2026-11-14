"""Calendar-aware coverage diagnostics and explicit complete-component resampling."""
from __future__ import annotations

from bisect import bisect_right
from datetime import datetime
import math

from pydantic import AwareDatetime, TypeAdapter

from .actions import Split
from .calendar import SECONDS, expected_intervals
from .compiler import digest
from .data import MarketBar, Snapshot, SnapshotLineage

CLOCK = TypeAdapter(AwareDatetime)
RANK = {**SECONDS, "1d": 86400, "1w": 604800}


def series(snapshot, symbol, timeframe):
    rows = [b for b in snapshot.bars if b.symbol == symbol and b.timeframe == timeframe]
    exchanges = {b.exchange for b in snapshot.bars if b.symbol == symbol}
    if not exchanges:
        raise ValueError("UNKNOWN_REFERENCE: symbol is absent from the snapshot")
    calendar = next((c for c in snapshot.calendars if c.exchange in exchanges), None)
    return sorted(rows, key=lambda b: b.start_time), calendar


def data_quality(snapshot: Snapshot, *, symbol: str, timeframe: str, as_of: datetime, max_staleness_seconds: int | None = None):
    as_of = CLOCK.validate_python(as_of)
    if timeframe not in RANK or (max_staleness_seconds is not None and (type(max_staleness_seconds) is not int or max_staleness_seconds < 0)):
        raise ValueError("Invalid quality timeframe/staleness tolerance")
    rows, calendar = series(snapshot, symbol, timeframe)
    available = [b for b in rows if b.available_at <= as_of]
    expected = [i for i in expected_intervals(calendar, timeframe) if i.end <= as_of] if calendar else None
    observed = {(b.start_time, b.end_time) for b in available}
    expected_keys = {(i.start, i.end) for i in expected} if expected is not None else None
    missing = sorted(expected_keys - observed) if expected_keys is not None else None
    extra = sorted(observed - expected_keys) if expected_keys is not None else None
    latest_end = max((b.end_time for b in available), default=None)
    staleness = (as_of - latest_end).total_seconds() if latest_end else None
    def samples(intervals):
        return [{"start": a.isoformat(), "end": b.isoformat()} for a, b in intervals[:10]] if intervals is not None else None
    return {"data_snapshot_id": snapshot.id, "symbol": symbol, "timeframe": timeframe, "as_of": as_of.isoformat(),
            "calendar_id": calendar.id if calendar else None, "calendar_version": calendar.version if calendar else None,
            "exchange_timezone": calendar.timezone if calendar else None, "observed_count": len(available),
            "first_start_time": available[0].start_time.isoformat() if available else None,
            "latest_end_time": latest_end.isoformat() if latest_end else None,
            "latest_available_at": max((b.available_at for b in available), default=None).isoformat() if available else None,
            "expected_completed_count": len(expected) if expected is not None else None,
            "unavailable_completed_count": len(missing) if missing is not None else None,
            "unavailable_samples": samples(missing), "out_of_schedule_count": len(extra) if extra is not None else None,
            "out_of_schedule_samples": samples(extra),
            "coverage_ratio": len(observed & expected_keys) / len(expected_keys) if expected_keys else (1. if expected is not None and not observed else None),
            "staleness_seconds": staleness, "max_staleness_seconds": max_staleness_seconds,
            "stale": staleness > max_staleness_seconds if staleness is not None and max_staleness_seconds is not None else None,
            "availability_policies": sorted({b.availability_policy for b in available}),
            "adjustment_policies": sorted({b.adjustment for b in available}),
            "publication_delay_seconds": {"min": min((b.available_at - b.end_time).total_seconds() for b in available),
                                          "max": max((b.available_at - b.end_time).total_seconds() for b in available)} if available else None,
            "interpretation": "Unavailability may be a publication delay, missing observation or suspension; no cause is inferred. Staleness uses wall-clock seconds. Partial ISO weeks require full calendar coverage.",
            "quality_grade": "schedule_checked" if calendar else "schema_only_no_schedule"}


def resample_snapshot(snapshot: Snapshot, *, symbol: str, source_timeframe: str, target_timeframe: str, as_of: datetime | None = None):
    if source_timeframe not in RANK or target_timeframe not in RANK or RANK[source_timeframe] >= RANK[target_timeframe] or RANK[target_timeframe] % RANK[source_timeframe]:
        raise ValueError("TIMEFRAME_MISMATCH: resampling requires an explicitly larger integral timeframe")
    rows, calendar = series(snapshot, symbol, source_timeframe)
    if calendar is None:
        raise ValueError("DATA_UNAVAILABLE: a versioned exchange calendar is required")
    if not rows:
        raise ValueError("DATA_UNAVAILABLE: source timeframe is absent; no substitution performed")
    if any(b.symbol == symbol and b.timeframe == target_timeframe for b in snapshot.bars):
        raise ValueError("SCHEMA_INVALID: target series already exists; resampling never replaces it")
    if rows[0].adjustment != "raw":
        raise ValueError("UNSUPPORTED_EXECUTION_MODEL: resampling requires raw source observations")
    cutoff = CLOCK.validate_python(as_of) if as_of is not None else max(b.available_at for b in rows)
    source_expected = expected_intervals(calendar, source_timeframe)
    targets = [i for i in expected_intervals(calendar, target_timeframe) if i.end <= cutoff]
    lookup = {(b.start_time, b.end_time): b for b in rows if b.available_at <= cutoff}
    expected_keys = {(i.start, i.end) for i in source_expected}
    version = digest({"parent": snapshot.id, "symbol": symbol, "source": source_timeframe, "target": target_timeframe,
                      "calendar": calendar.id, "operator": "session_ohlcv_v1"})
    new, incomplete, blocked, samples = [], 0, 0, []
    position = 0
    splits = sorted(a.effective_at for a in snapshot.actions if isinstance(a, Split) and a.symbol == symbol)
    for target in targets:
        while position < len(source_expected) and source_expected[position].end <= target.start:
            position += 1
        stop = position
        parts = []
        while stop < len(source_expected) and source_expected[stop].start < target.end:
            part = source_expected[stop]
            if part.start < target.start or part.end > target.end:
                raise ValueError("TIMEFRAME_MISMATCH: constituent interval crosses target boundary")
            if (bar := lookup.get((part.start, part.end))) is not None:
                parts.append(bar)
            stop += 1
        expected_count = stop - position
        split_index = bisect_right(splits, target.start)
        if split_index < len(splits) and splits[split_index] < target.end:
            blocked += 1
            reason = "SPLIT_INSIDE_BUCKET"
        elif not parts or len(parts) != expected_count:
            incomplete += 1
            reason = "INCOMPLETE_CONSTITUENTS"
        else:
            try:
                volume = math.fsum(b.volume for b in parts)
                if not math.isfinite(volume):
                    raise ValueError("Nonfinite volume")
            except (OverflowError, ValueError) as error:
                raise ValueError("DATA_UNAVAILABLE: aggregate volume is nonfinite") from error
            new.append(MarketBar(symbol=symbol, timeframe=target_timeframe, exchange=rows[0].exchange,
                                 currency=rows[0].currency, start_time=target.start, end_time=target.end,
                                 available_at=max(target.end, *(b.available_at for b in parts)),
                                 open=parts[0].open, high=max(b.high for b in parts), low=min(b.low for b in parts), close=parts[-1].close,
                                 volume=volume, source=rows[0].source + ":session_ohlcv", dataset_version=version, adjustment="raw",
                                 availability_policy="derived_constituent_availability"))
            continue
        if len(samples) < 10:
            samples.append({"start": target.start.isoformat(), "end": target.end.isoformat(), "code": reason,
                            "expected_constituents": expected_count, "available_constituents": len(parts)})
    report = {"calendar_id": calendar.id, "as_of": cutoff.isoformat(), "source_timeframe": source_timeframe,
              "target_timeframe": target_timeframe, "eligible_target_count": len(targets), "emitted_count": len(new), "incomplete_count": incomplete,
              "split_blocked_count": blocked, "unrecognized_source_count": len(set(lookup) - expected_keys), "samples": samples,
              "policy": "session_ohlcv_v1; complete exact constituents only; no gap filling; source bars retained; internal split buckets omitted; weekly requires full Monday-Sunday calendar coverage"}
    if not new:
        return None, report
    if len(snapshot.bars) + len(new) > 10000:
        raise ValueError("RESOURCE_LIMIT_EXCEEDED: derived snapshot exceeds 10000 bars")
    return Snapshot([*snapshot.bars, *new], calendar_version=snapshot.calendar_version, calendars=snapshot.calendars,
                    actions=list(snapshot.actions), lineage=SnapshotLineage(parent_snapshot_id=snapshot.id, symbol=symbol,
                    source_timeframe=source_timeframe, target_timeframe=target_timeframe, calendar_id=calendar.id)), report
