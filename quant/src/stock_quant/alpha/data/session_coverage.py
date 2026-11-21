"""Per-instrument trading-session coverage independent of symbol presence."""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime
from typing import Iterable

from ...calendar import CALENDAR_PROVIDER_VERSION, XNYS_DAILY_BOUNDS_VERSION, CalendarShardManifest, generate_long_calendar
from ..errors import AlphaError, AlphaErrorCode


def _as_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value[:10])
    raise TypeError(f"Unsupported session date: {value!r}")


def _row_session(row) -> date:
    start = row["start_at"] if isinstance(row, dict) else row.start_at
    return _as_date(start)


def audit_session_coverage(
    rows: Iterable,
    *,
    requested_symbols: Iterable[str],
    calendar_session_dates: Iterable[date | datetime | str],
    calendar_id: str,
    session_dates_by_symbol: dict[str, Iterable[date | datetime | str]] | None = None,
) -> list[dict]:
    """Audit observed bars against an explicit trading calendar.

    The audit only certifies coverage inside each instrument's observed range.
    Dates outside that range remain UNVERIFIED: the function never infers
    pre-IPO, suspension, delisting, or whether an instrument existed.
    """
    normalized_symbols = list(dict.fromkeys(str(symbol).upper() for symbol in requested_symbols))
    calendar_sessions = sorted({_as_date(value) for value in calendar_session_dates})
    calendar_set = set(calendar_sessions)
    raw_by_symbol: dict[str, list[date]] = {symbol: [] for symbol in normalized_symbols}
    for row in rows:
        symbol = row["instrument_id"] if isinstance(row, dict) else row.instrument_id
        symbol = str(symbol).upper()
        raw_by_symbol.setdefault(symbol, []).append(_row_session(row))
    if session_dates_by_symbol is not None:
        for symbol, values in session_dates_by_symbol.items():
            normalized = str(symbol).upper()
            raw_by_symbol[normalized] = [_as_date(value) for value in values]

    reports: list[dict] = []
    for symbol in normalized_symbols:
        observed = raw_by_symbol.get(symbol, [])
        counts = Counter(observed)
        duplicates = sorted(session for session, count in counts.items() if count > 1)
        first = min(observed) if observed else None
        last = max(observed) if observed else None
        if first is None or last is None:
            reports.append({
                "instrument_id": symbol,
                "calendar_id": calendar_id,
                "first_observed_session": None,
                "last_observed_session": None,
                "expected_sessions_within_observed_range": 0,
                "observed_sessions_within_observed_range": 0,
                "missing_sessions": [],
                "extra_sessions": [],
                "duplicate_sessions": [],
                "missing_session_count": 0,
                "extra_session_count": 0,
                "duplicate_session_count": 0,
                "session_coverage": None,
                "session_status": "NO_BARS",
                "outside_observed_range_status": "UNVERIFIED",
            })
            continue
        expected = [session for session in calendar_sessions if first <= session <= last]
        expected_set = set(expected)
        observed_set = set(observed)
        missing = sorted(expected_set - observed_set)
        matched = sorted(expected_set & observed_set)
        extra = sorted(observed_set - calendar_set)
        if not calendar_sessions:
            status = "UNVERIFIED_CALENDAR"
        elif missing:
            status = "MISSING_SESSIONS"
        elif extra or duplicates:
            status = "SESSION_ANOMALIES"
        else:
            status = "COMPLETE"
        reports.append({
            "instrument_id": symbol,
            "calendar_id": calendar_id,
            "first_observed_session": first.isoformat(),
            "last_observed_session": last.isoformat(),
            "expected_sessions_within_observed_range": len(expected),
            "observed_sessions_within_observed_range": len(matched),
            "missing_sessions": [session.isoformat() for session in missing],
            "extra_sessions": [session.isoformat() for session in extra],
            "duplicate_sessions": [session.isoformat() for session in duplicates],
            "missing_session_count": len(missing),
            "extra_session_count": len(extra),
            "duplicate_session_count": sum(counts[session] - 1 for session in duplicates),
            "session_coverage": (len(matched) / len(expected)) if expected else None,
            "session_status": status,
            "outside_observed_range_status": "UNVERIFIED",
        })
    return reports


def aggregate_session_coverage(reports: Iterable[dict]) -> float | None:
    expected = sum(int(report["expected_sessions_within_observed_range"]) for report in reports)
    observed = sum(int(report["observed_sessions_within_observed_range"]) for report in reports)
    return (observed / expected) if expected else None


def mapped_stock_calendar(manifest):
    """Rebuild the exact evidenced calendar carried by a stock dataset."""
    mappings = manifest.data_quality.get("exchange_mappings") if isinstance(manifest.data_quality, dict) else None
    if not mappings:
        return None
    identities = {(item.get("calendar_name"), item.get("calendar_version")) for item in mappings.values()}
    statuses = {item.get("calendar_mapping_status") for item in mappings.values()}
    if len(identities) != 1 or not statuses <= {"VERIFIED", "PROXY_ONLY"}:
        raise AlphaError(AlphaErrorCode.DATA_UNAVAILABLE, "Stock dataset calendar mappings are inconsistent or unverified")
    calendar_name, calendar_version = next(iter(identities))
    mapping = next(iter(mappings.values()))
    evidence = mapping.get("mapping_evidence") or {}
    if not calendar_name or evidence.get("provider_version") != CALENDAR_PROVIDER_VERSION:
        raise AlphaError(AlphaErrorCode.DATA_UNAVAILABLE, "Stock dataset calendar provider version is not executable")
    xnys_daily_bounds = isinstance(calendar_version, str) and calendar_version.endswith(f"|{XNYS_DAILY_BOUNDS_VERSION}")
    if xnys_daily_bounds and (calendar_name != "XNYS" or statuses != {"VERIFIED"}):
        raise AlphaError(AlphaErrorCode.DATA_UNAVAILABLE, f"{XNYS_DAILY_BOUNDS_VERSION} requires an exact XNYS mapping")
    shards = manifest.data_quality.get("calendar_shards")
    start, end, exchange = manifest.start_at.date(), manifest.end_at.date(), calendar_name
    if shards is not None:
        if not isinstance(shards, list) or not shards or any(not isinstance(shard, dict) for shard in shards):
            raise AlphaError(AlphaErrorCode.DATA_UNAVAILABLE, "Invalid stock dataset calendar shard manifest")
        start, end, exchange = shards[0].get("start"), shards[-1].get("end"), shards[0].get("exchange")
        if not all(isinstance(value, str) for value in (start, end, exchange)):
            raise AlphaError(AlphaErrorCode.DATA_UNAVAILABLE, "Invalid stock dataset calendar shard bounds or exchange")
    try:
        calendar = generate_long_calendar(calendar_name=calendar_name, exchange=exchange, start_date=start,
                                          end_date=end, xnys_daily_bounds=xnys_daily_bounds)
    except ValueError as error:
        raise AlphaError(AlphaErrorCode.DATA_UNAVAILABLE, "Cannot rebuild stock dataset calendar") from error
    if calendar.version != calendar_version:
        raise AlphaError(AlphaErrorCode.DATA_UNAVAILABLE, "Stock dataset calendar version does not match its mapping")
    if xnys_daily_bounds and (
        manifest.calendar_version != calendar.version or manifest.data_quality.get("calendar_id") != calendar.id
    ):
        raise AlphaError(AlphaErrorCode.DATA_UNAVAILABLE, "Stock dataset calendar identity does not match its manifest")
    if xnys_daily_bounds and isinstance(calendar, CalendarShardManifest) and shards is None:
        raise AlphaError(AlphaErrorCode.DATA_UNAVAILABLE, "Stock dataset calendar shard manifest is missing")
    if shards is not None and (shards != list(calendar.long_calendar_shards)
                               or manifest.data_quality.get("calendar_id") != calendar.id):
        raise AlphaError(AlphaErrorCode.DATA_UNAVAILABLE, "Stock dataset calendar shards do not match their manifest")
    return calendar
