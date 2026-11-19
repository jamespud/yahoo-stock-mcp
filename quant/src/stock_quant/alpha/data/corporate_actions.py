"""Versioned corporate-action evidence and fixture-validated accounting."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from ...compiler import digest
from ...contracts import StrictModel
from ..errors import AlphaError, AlphaErrorCode

ActionType = Literal[
    "split", "reverse_split", "cash_dividend", "symbol_change", "delisting",
    "merger", "special_distribution",
]
SUPPORTED_ACTION_TYPES = {"split", "reverse_split", "cash_dividend", "symbol_change", "delisting"}


class CorporateAction(StrictModel):
    action_id: str = Field(min_length=1, max_length=128)
    instrument_id: str = Field(min_length=1, max_length=64)
    action_type: ActionType
    ex_date: AwareDatetime
    payment_date: AwareDatetime | None = None
    effective_at: AwareDatetime
    source: str = Field(min_length=1, max_length=128)
    source_version: str = Field(min_length=1, max_length=128)
    quality_status: Literal["VERIFIED", "UNVERIFIED", "INVALID"] = "UNVERIFIED"
    terms: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def dividend_dates(self):
        if self.action_type == "cash_dividend" and self.payment_date is None:
            raise ValueError("Cash dividends require an explicit payment date")
        if self.payment_date is not None and self.payment_date < self.ex_date:
            raise ValueError("Dividend payment date cannot precede ex-date")
        return self


class CorporateActionCoverage(StrictModel):
    instrument_id: str = Field(min_length=1, max_length=64)
    start_at: AwareDatetime
    end_at: AwareDatetime
    source: str = Field(min_length=1, max_length=128)
    source_version: str = Field(min_length=1, max_length=128)
    status: Literal["VERIFIED", "UNVERIFIED", "BLOCKED"]


class CorporateActionDataset(StrictModel):
    schema_version: str = "corporate-actions-v1"
    action_dataset_id: str
    instrument_ids: list[str]
    start_at: AwareDatetime
    end_at: AwareDatetime
    actions: list[CorporateAction]
    coverage: list[CorporateActionCoverage]
    coverage_status: Literal["VERIFIED", "BLOCKED"]
    missing_coverage_instruments: list[str]
    total_return_claim_allowed: bool
    blockers: list[dict]

    @model_validator(mode="after")
    def identity(self):
        payload = self.model_dump(mode="json", exclude={"action_dataset_id"})
        if digest(payload) != self.action_dataset_id:
            raise ValueError("Corporate action dataset identity mismatch")
        return self


def _utc(value) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def build_corporate_action_dataset(
    actions,
    *,
    expected_instruments: list[str],
    start_at: datetime,
    end_at: datetime,
    coverage,
) -> CorporateActionDataset:
    normalized_actions = [item if isinstance(item, CorporateAction) else CorporateAction.model_validate(item) for item in actions]
    normalized_coverage = [item if isinstance(item, CorporateActionCoverage) else CorporateActionCoverage.model_validate(item) for item in coverage]
    identifiers = sorted({str(value).upper() for value in expected_instruments})
    action_ids = [action.action_id for action in normalized_actions]
    coverage_instruments = [item.instrument_id.upper() for item in normalized_coverage]
    if len(action_ids) != len(set(action_ids)):
        raise AlphaError(AlphaErrorCode.DATASET_MISMATCH, "Duplicate corporate action identity", {"reason": "DUPLICATE_CORPORATE_ACTION"})
    if len(coverage_instruments) != len(set(coverage_instruments)):
        raise AlphaError(AlphaErrorCode.DATASET_MISMATCH, "Duplicate corporate-action coverage", {"reason": "DUPLICATE_CORPORATE_ACTION_COVERAGE"})
    for action in normalized_actions:
        if action.action_type not in SUPPORTED_ACTION_TYPES:
            raise AlphaError(
                AlphaErrorCode.DATASET_MISMATCH,
                "Unsupported corporate action",
                {"reason": "UNSUPPORTED_CORPORATE_ACTION", "action_type": action.action_type, "action_id": action.action_id},
            )
    by_instrument = {item.instrument_id.upper(): item for item in normalized_coverage}
    start, end = _utc(start_at), _utc(end_at)
    missing = []
    for symbol in identifiers:
        item = by_instrument.get(symbol)
        if item is None or item.status != "VERIFIED" or _utc(item.start_at) > start or _utc(item.end_at) < end:
            missing.append(symbol)
    blockers = []
    if missing:
        blockers.append({"reason": "CORPORATE_ACTION_COVERAGE_INCOMPLETE", "instruments": missing})
    unverified_actions = [action.action_id for action in normalized_actions if action.quality_status != "VERIFIED"]
    if unverified_actions:
        blockers.append({"reason": "CORPORATE_ACTION_QUALITY_UNVERIFIED", "action_ids": unverified_actions})
    body = {
        "schema_version": "corporate-actions-v1",
        "instrument_ids": identifiers,
        "start_at": start.isoformat().replace("+00:00", "Z"),
        "end_at": end.isoformat().replace("+00:00", "Z"),
        "actions": [action.model_dump(mode="json") for action in normalized_actions],
        "coverage": [item.model_dump(mode="json") for item in normalized_coverage],
        "coverage_status": "VERIFIED" if not missing else "BLOCKED",
        "missing_coverage_instruments": missing,
        "total_return_claim_allowed": not blockers,
        "blockers": blockers,
    }
    return CorporateActionDataset.model_validate({**body, "action_dataset_id": digest(body)})


def apply_split(position: dict, action: CorporateAction) -> dict:
    if action.action_type not in {"split", "reverse_split"}:
        raise ValueError("Split accounting requires a split action")
    ratio = float(action.terms.get("ratio", 0))
    quantity = float(position["quantity"])
    unit_cost = float(position["unit_cost"])
    if ratio <= 0 or quantity < 0:
        raise ValueError("Split ratio must be positive and quantity non-negative")
    new_quantity = quantity * ratio
    new_unit_cost = (unit_cost / ratio) if quantity else unit_cost
    return {**position, "quantity": new_quantity, "unit_cost": new_unit_cost, "last_action_id": action.action_id}


def cash_dividend_entitlement(position: dict, action: CorporateAction) -> float:
    if action.action_type != "cash_dividend":
        raise ValueError("Dividend entitlement requires a cash dividend action")
    cash_per_share = float(action.terms.get("cash_per_share", 0))
    if cash_per_share < 0 or float(position["quantity"]) < 0:
        raise ValueError("Dividend terms and entitled quantity must be non-negative")
    return float(position["quantity"]) * cash_per_share


def apply_cash_dividend(account: dict, quantities: dict[str, float], action: CorporateAction, *, as_of: datetime) -> dict:
    if action.payment_date is None or _utc(as_of) < _utc(action.payment_date):
        return account
    amount = cash_dividend_entitlement({"quantity": quantities.get(action.instrument_id.upper(), 0)}, action)
    updated = {
        **account,
        "cash": float(account.get("cash", 0)) + amount,
        "ledger": [
            *account.get("ledger", []),
            {
                "action_id": action.action_id,
                "instrument_id": action.instrument_id.upper(),
                "type": "cash_dividend",
                "amount": amount,
                "payment_date": _utc(action.payment_date).isoformat(),
            },
        ],
    }
    return updated
