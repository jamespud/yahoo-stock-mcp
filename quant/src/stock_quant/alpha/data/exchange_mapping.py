"""Versioned per-instrument source-exchange to trading-calendar mapping."""
from __future__ import annotations

import hashlib
import warnings
from datetime import date
from importlib.metadata import version
from typing import Iterable

import exchange_calendars as xc
from pandas.errors import Pandas4Warning

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


def _calendar_version() -> str:
    return f"exchange_calendars:{_PROVIDER_VERSION}|pandas:{version('pandas')}|tzdata:{version('tzdata')}"


def _normalise_source_exchange(value: str | None) -> str:
    return " ".join(str(value or "").strip().split())


def _candidate_calendar(source_exchange: str) -> str | None:
    compact = "".join(source_exchange.upper().split())
    if compact in _SOURCE_CANONICAL:
        return _SOURCE_CANONICAL[compact]
    names = set(xc.get_calendar_names())
    return compact if compact in names else None


def build_exchange_mapping(
    instrument_id: str,
    source_exchange: str | None,
    *,
    start_date: date,
    end_date: date,
    historical_exchanges: Iterable[str] | None = None,
    transition_evidence: dict | None = None,
) -> dict:
    """Map a source exchange without erasing identity or alias uncertainty."""
    source = _normalise_source_exchange(source_exchange)
    base = {
        "instrument_id": instrument_id.upper(),
        "source_exchange": source,
        "exchange_identity_status": "UNVERIFIED",
        "calendar_name": None,
        "calendar_version": _calendar_version(),
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
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", Pandas4Warning)
        oracle = xc.get_calendar(resolved_name, start=start_date.isoformat(), end=end_date.isoformat())
        early_closes = oracle.early_closes
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
    base.update({
        "exchange_identity_status": identity_status,
        "calendar_name": oracle.name,
        "calendar_mapping_status": mapping_status,
        "mapping_source": "exchange_calendars_alias" if alias_resolved else "exchange_calendars_exact",
        "session_dates": [str(session.date()) for session in oracle.sessions],
        "early_close_dates": [str(day.date()) for day in early_closes],
        "mapping_evidence": {
            **base["mapping_evidence"],
            "requested_calendar_name": candidate,
            "resolved_calendar_name": oracle.name,
            "alias_resolved": alias_resolved,
            "session_count": len(oracle.sessions),
            "early_close_count": len(early_closes),
            "session_fingerprint": hashlib.sha256("|".join(str(day.date()) for day in oracle.sessions).encode()).hexdigest(),
        },
    })
    return base
