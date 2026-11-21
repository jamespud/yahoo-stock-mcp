"""Versioned per-instrument source-exchange to trading-calendar mapping."""
from __future__ import annotations

import hashlib
import warnings
from datetime import date
from functools import lru_cache
from importlib.metadata import version
from typing import Iterable

import exchange_calendars as xc
from pandas.errors import Pandas4Warning

from ...calendar import XNYS_DAILY_EARLY_CLOSES, calendar_shard_ranges, exchange_calendar_version

MAPPING_VERSION = "exchange-calendar-mapping-v1"
_PROVIDER_VERSION = version("exchange-calendars")
_SOURCE_CANONICAL = {
    "NYSE": "XNYS",
    "NYQ": "XNYS",
    "XNYS": "XNYS",
    "NASDAQ": "XNAS",
    "NASDAQGS": "XNAS",
    "NMS": "XNAS",
    "NGM": "XNAS",
    "NCM": "XNAS",
    "NAS": "XNAS",
    "NASDAQCM": "XNAS",
    "NASDAQGM": "XNAS",
    "XNAS": "XNAS",
    "NYSEARCA": "ARCX",
    "PCX": "ARCX",
    "ARCX": "ARCX",
    "ASE": "XASE",
    "XASE": "XASE",
    "BATS": "BATS",
    "CBOEBZX": "BATS",
}


def _normalise_source_exchange(value: str | None) -> str:
    return " ".join(str(value or "").strip().split())


def _candidate_calendar(source_exchange: str) -> str | None:
    compact = "".join(source_exchange.upper().split())
    if compact in _SOURCE_CANONICAL:
        return _SOURCE_CANONICAL[compact]
    names = set(xc.get_calendar_names())
    return compact if compact in names else None


@lru_cache(maxsize=32)
def _calendar_dates(name: str, start_date: date, end_date: date):
    sessions, early_closes = [], []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", Pandas4Warning)
        for left, right in calendar_shard_ranges(start_date, end_date):
            # Use the same padding as generate_calendar for holiday-only shards.
            from datetime import timedelta
            oracle = xc.get_calendar(name, start=left - timedelta(days=7), end=right + timedelta(days=7))
            sessions.extend(str(day.date()) for day in oracle.sessions if left <= day.date() <= right)
            early_closes.extend(str(day.date()) for day in oracle.early_closes if left <= day.date() <= right)
    if any(a >= b for a, b in zip(sessions, sessions[1:])):
        raise ValueError("Calendar shard session dates must increase strictly")
    return tuple(sessions), tuple(early_closes)


def build_exchange_mapping(
    instrument_id: str,
    source_exchange: str | None,
    *,
    start_date: date,
    end_date: date,
    historical_exchanges: Iterable[str] | None = None,
    transition_evidence: dict | None = None,
    xnys_daily_bounds: bool = False,
) -> dict:
    """Map a source exchange without erasing identity or alias uncertainty."""
    source = _normalise_source_exchange(source_exchange)
    base = {
        "instrument_id": instrument_id.upper(),
        "source_exchange": source,
        "exchange_identity_status": "UNVERIFIED",
        "calendar_name": None,
        "calendar_version": exchange_calendar_version(),
        "calendar_mapping_status": "UNVERIFIED",
        "mapping_version": MAPPING_VERSION,
        "mapping_source": "exchange_calendars",
        "mapping_evidence": {
            "provider": "exchange_calendars",
            "provider_version": _PROVIDER_VERSION,
            "historical_exchanges": list(historical_exchanges or []),
            "transition_evidence": transition_evidence,
        },
        "session_dates": [],
        "early_close_dates": [],
    }
    candidate = _candidate_calendar(source)
    if candidate is None:
        base["calendar_mapping_status"] = "UNSUPPORTED"
        base["mapping_evidence"]["reason"] = "SOURCE_EXCHANGE_NOT_RECOGNIZED"
        return base

    resolved_name = xc.resolve_alias(candidate)
    names = set(xc.get_calendar_names())
    if resolved_name not in names:
        base["calendar_mapping_status"] = "UNSUPPORTED"
        base["mapping_evidence"].update({"requested_calendar_name": candidate, "reason": "RESOLVED_CALENDAR_UNAVAILABLE"})
        return base
    sessions, early_closes = _calendar_dates(resolved_name, start_date, end_date)
    alias_resolved = candidate != resolved_name
    base_status = "PROXY_ONLY" if alias_resolved else "VERIFIED"
    history = [str(value) for value in (historical_exchanges or [])]
    if len(set(history)) > 1 and transition_evidence is None:
        mapping_status = "EXCHANGE_TRANSITION_UNVERIFIED"
        identity_status = "UNVERIFIED"
    elif len(set(history)) > 1:
        mapping_status = base_status
        identity_status = "VERIFIED_WITH_TRANSITION_EVIDENCE"
    else:
        mapping_status = base_status
        identity_status = base_status
    if xnys_daily_bounds and candidate == resolved_name == "XNYS" and mapping_status == "VERIFIED":
        extra_closes = {day.isoformat() for day in XNYS_DAILY_EARLY_CLOSES}
        early_closes = tuple(sorted(set(early_closes) | (extra_closes & set(sessions))))
        base["calendar_version"] = exchange_calendar_version(xnys_daily_bounds=True)
    base.update({
        "exchange_identity_status": identity_status,
        "calendar_name": resolved_name,
        "calendar_mapping_status": mapping_status,
        "mapping_source": "exchange_calendars_alias" if alias_resolved else "exchange_calendars_exact",
        "session_dates": list(sessions),
        "early_close_dates": list(early_closes),
        "mapping_evidence": {
            **base["mapping_evidence"],
            "requested_calendar_name": candidate,
            "resolved_calendar_name": resolved_name,
            "alias_resolved": alias_resolved,
            "session_count": len(sessions),
            "early_close_count": len(early_closes),
            "session_fingerprint": hashlib.sha256("|".join(sessions).encode()).hexdigest(),
        },
    })
    return base
