"""Joint price-attestation and corporate-action execution admission."""
from __future__ import annotations

from datetime import datetime, timezone

from pydantic import AwareDatetime

from ...compiler import digest
from ...contracts import StrictModel
from .corporate_actions import CorporateActionDataset
from .price_attestation import PriceAttestation


class ExecutionValidityRecord(StrictModel):
    schema_version: str = "execution-validity-v1"
    validation_id: str
    price_dataset_id: str
    corporate_action_dataset_id: str
    price_attestation_id: str
    execution_status: str
    total_return_claim_allowed: bool
    coverage_window: dict
    verified_symbols: list[str]
    blocking_reasons: list[dict]
    calendar_identity: dict


def _utc(value) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def validate_execution_inputs(
    price_attestation: PriceAttestation,
    action_dataset: CorporateActionDataset,
    *,
    instruments: list[str],
    start_at: datetime,
    end_at: datetime,
) -> ExecutionValidityRecord:
    symbols = sorted({str(value).upper() for value in instruments})
    start, end = _utc(start_at), _utc(end_at)
    blockers = []
    if price_attestation.attestation_status != "VERIFIED":
        blockers.append({"reason": "PRICE_ATTESTATION_NOT_VERIFIED", "status": price_attestation.attestation_status})
    missing_price = sorted(set(symbols) - set(price_attestation.verified_symbols))
    if missing_price:
        blockers.append({"reason": "PRICE_SYMBOLS_UNVERIFIED", "instruments": missing_price})
    claim_start = _utc(price_attestation.coverage_window["start_at"])
    claim_end = _utc(price_attestation.coverage_window["end_at"])
    if claim_start > start or claim_end < end:
        blockers.append({"reason": "PRICE_COVERAGE_WINDOW_INCOMPLETE"})
    if action_dataset.coverage_status != "VERIFIED":
        blockers.append({"reason": "CORPORATE_ACTION_COVERAGE_NOT_VERIFIED"})
    missing_actions = sorted(set(symbols) - set(action_dataset.instrument_ids))
    if missing_actions:
        blockers.append({"reason": "CORPORATE_ACTION_SYMBOLS_UNVERIFIED", "instruments": missing_actions})
    body = {
        "schema_version": "execution-validity-v1",
        "price_dataset_id": price_attestation.price_dataset_id,
        "corporate_action_dataset_id": action_dataset.action_dataset_id,
        "price_attestation_id": price_attestation.model_dump().get("attestation_id", digest(price_attestation.model_dump(mode="json"))),
        "execution_status": "BLOCKED" if blockers else "ELIGIBLE",
        "total_return_claim_allowed": not blockers and action_dataset.total_return_claim_allowed,
        "coverage_window": {"start_at": start.isoformat(), "end_at": end.isoformat()},
        "verified_symbols": symbols if not blockers else [],
        "blocking_reasons": blockers,
        "calendar_identity": price_attestation.calendar_identity,
    }
    return ExecutionValidityRecord.model_validate({**body, "validation_id": digest(body)})
