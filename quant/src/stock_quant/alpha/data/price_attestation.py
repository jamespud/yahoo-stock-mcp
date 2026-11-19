"""Scoped price-basis attestation from independent raw OHLC references."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from ...compiler import digest
from ...contracts import StrictModel


class PriceReference(StrictModel):
    instrument_id: str = Field(min_length=1, max_length=64)
    start_at: AwareDatetime
    declared_basis: Literal["raw", "adjusted", "split_adjusted", "total_return", "unknown"]
    reference_open: float = Field(gt=0, strict=True)
    reference_high: float = Field(gt=0, strict=True)
    reference_low: float = Field(gt=0, strict=True)
    reference_close: float = Field(gt=0, strict=True)
    source: str = Field(min_length=1, max_length=128)
    source_version: str = Field(min_length=1, max_length=128)
    evidence_id: str = Field(min_length=1, max_length=256)
    absolute_tolerance: float = Field(default=1e-8, ge=0, strict=True)


class PriceAttestation(StrictModel):
    schema_version: str = "price-attestation-v1"
    attestation_id: str
    price_dataset_id: str
    price_basis: Literal["raw_attested", "price_basis_unverified"]
    source_and_source_version: dict
    verification_method: str
    reference_evidence_ids: list[str]
    calendar_identity: dict
    coverage_window: dict
    verified_symbols: list[str]
    unverified_symbols: list[str]
    verified_scopes: list[dict]
    attestation_status: Literal["VERIFIED", "PARTIAL", "BLOCKED"]

    @model_validator(mode="after")
    def identity(self):
        payload = self.model_dump(mode="json", exclude={"attestation_id"})
        if digest(payload) != self.attestation_id:
            raise ValueError("Price attestation identity mismatch")
        return self


def _utc(value) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def attest_price_dataset(
    price_dataset_id: str,
    rows: list[dict],
    references,
    *,
    start_at: datetime,
    end_at: datetime,
    source: str,
    source_version: str,
    calendar_identity: dict,
) -> PriceAttestation:
    """Verify raw OHLC only when independent evidence covers every dataset row."""
    rows_by_key = {
        (str(row["instrument_id"]).upper(), _utc(row["start_at"])): row
        for row in rows
    }
    all_symbols = sorted({symbol for symbol, _ in rows_by_key})
    references_by_key = defaultdict(list)
    for raw in references:
        reference = raw if isinstance(raw, PriceReference) else PriceReference.model_validate(raw)
        references_by_key[(reference.instrument_id.upper(), _utc(reference.start_at))].append(reference)
    verified_symbols = []
    verified_scopes = []
    evidence_ids = set()
    for symbol in all_symbols:
        symbol_rows = [(key, row) for key, row in rows_by_key.items() if key[0] == symbol]
        matched_dates = []
        symbol_valid = True
        for key, row in symbol_rows:
            candidates = references_by_key.get(key, [])
            matched = [
                candidate for candidate in candidates
                if candidate.declared_basis == "raw"
                and candidate.source.strip().lower() != str(source).strip().lower()
                and all(
                    abs(float(row[field]) - reference_value) <= candidate.absolute_tolerance
                    for field, reference_value in (
                        ("open", candidate.reference_open),
                        ("high", candidate.reference_high),
                        ("low", candidate.reference_low),
                        ("close", candidate.reference_close),
                    )
                )
            ]
            if not matched:
                symbol_valid = False
                continue
            matched_dates.append(key[1])
            evidence_ids.update(candidate.evidence_id for candidate in matched)
        if symbol_valid and len(matched_dates) == len(symbol_rows):
            verified_symbols.append(symbol)
            verified_scopes.append({
                "instrument_id": symbol,
                "start_at": min(matched_dates).isoformat(),
                "end_at": max(matched_dates).isoformat(),
            })
    unverified = sorted(set(all_symbols) - set(verified_symbols))
    status = "VERIFIED" if all_symbols and not unverified else ("PARTIAL" if verified_symbols else "BLOCKED")
    result = {
        "schema_version": "price-attestation-v1",
        "price_dataset_id": price_dataset_id,
        "price_basis": "raw_attested" if status == "VERIFIED" else "price_basis_unverified",
        "source_and_source_version": {"source": source, "source_version": source_version},
        "verification_method": "INDEPENDENT_RAW_OHLC_REFERENCE_V1",
        "reference_evidence_ids": sorted(evidence_ids),
        "calendar_identity": calendar_identity,
        "coverage_window": {"start_at": _utc(start_at).isoformat(), "end_at": _utc(end_at).isoformat()},
        "verified_symbols": verified_symbols,
        "unverified_symbols": unverified,
        "verified_scopes": verified_scopes,
        "attestation_status": status,
    }
    result["attestation_id"] = digest(result)
    return PriceAttestation.model_validate(result)
