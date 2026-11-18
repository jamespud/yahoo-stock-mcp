"""Provider-independent option contract identity."""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import Field, field_validator, model_validator

from ...compiler import digest
from ...contracts import StrictModel


def option_contract_id(**fields) -> str:
    canonical = {
        "underlying_id": str(fields["underlying_id"]).upper(),
        "option_type": fields["option_type"],
        "strike": float(fields["strike"]),
        "expiration": str(fields["expiration"]),
        "exercise_style": fields["exercise_style"],
        "settlement_type": fields["settlement_type"],
        "multiplier": int(fields["multiplier"]),
        "currency": str(fields["currency"]).upper(),
        "deliverable": fields["deliverable"],
        "adjustment_version": fields["adjustment_version"],
    }
    return digest(canonical)


class OptionContract(StrictModel):
    contract_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    underlying_id: str = Field(min_length=1, max_length=64)
    option_type: Literal["CALL", "PUT"]
    strike: float = Field(gt=0, strict=True)
    expiration: date
    exercise_style: Literal["american", "european"]
    settlement_type: Literal["physical", "cash"]
    multiplier: int = Field(gt=0, strict=True)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    deliverable: str = Field(min_length=1, max_length=512)
    adjustment_version: str = Field(default="standard", min_length=1, max_length=128)

    @field_validator("underlying_id")
    @classmethod
    def safe_underlying(cls, value: str) -> str:
        value = value.strip().upper()
        if not value or any(char in value for char in "/\\") or value in {".", ".."}:
            raise ValueError("Unsafe option underlying identifier")
        return value

    @model_validator(mode="after")
    def identity(self):
        expected = option_contract_id(**self.model_dump(exclude={"contract_id"}))
        if self.contract_id != expected:
            raise ValueError("Option contract_id does not match canonical identity")
        return self
