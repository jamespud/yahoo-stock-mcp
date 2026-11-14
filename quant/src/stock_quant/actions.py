"""Versioned corporate-action terms, separate from historical price adjustment."""
from typing import Annotated, Literal
from pydantic import AwareDatetime, Field, TypeAdapter, model_validator
from .contracts import StrictModel


class Action(StrictModel):
    id: str = Field(min_length=1, max_length=128)
    symbol: str = Field(min_length=1, max_length=64)
    effective_at: AwareDatetime
    available_at: AwareDatetime

    @model_validator(mode="after")
    def announced(self):
        if self.available_at > self.effective_at:
            raise ValueError("Late corporate actions require a revised snapshot; available_at must not exceed effective_at")
        return self


class Split(Action):
    kind: Literal["split"]
    ratio: float = Field(gt=0, strict=True)


class Dividend(Action):
    kind: Literal["cash_dividend"]
    amount: float = Field(ge=0, strict=True)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    pay_at: AwareDatetime

    @model_validator(mode="after")
    def payment(self):
        if self.pay_at < self.effective_at:
            raise ValueError("pay_at must be on or after entitlement time")
        return self


class CashDelisting(Action):
    kind: Literal["cash_delisting"]
    amount: float = Field(ge=0, strict=True)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")


CorporateAction = Annotated[Split | Dividend | CashDelisting, Field(discriminator="kind")]
ACTION_ADAPTER = TypeAdapter(CorporateAction)
