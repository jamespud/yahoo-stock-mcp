"""Option quote row with explicit observation and quote-time semantics."""
from __future__ import annotations

from pydantic import AwareDatetime, Field, model_validator

from ...contracts import StrictModel
from .contracts import OptionContract


class OptionQuote(StrictModel):
    contract: OptionContract
    observed_at: AwareDatetime
    quote_at: AwareDatetime | None = None
    available_at: AwareDatetime
    bid: float | None = Field(default=None, ge=0, strict=True)
    ask: float | None = Field(default=None, ge=0, strict=True)
    bid_size: int | None = Field(default=None, ge=0, strict=True)
    ask_size: int | None = Field(default=None, ge=0, strict=True)
    last_price: float | None = Field(default=None, ge=0, strict=True)
    last_trade_at: AwareDatetime | None = None
    volume: int | None = Field(default=None, ge=0, strict=True)
    open_interest: int | None = Field(default=None, ge=0, strict=True)
    implied_volatility: float | None = Field(default=None, ge=0, strict=True)
    source: str = Field(min_length=1, max_length=128)
    source_version: str = Field(min_length=1, max_length=128)
    quality_flags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def coherent(self):
        if self.observed_at > self.available_at:
            raise ValueError("observed_at must not exceed available_at")
        if self.quote_at is not None and self.quote_at > self.available_at:
            raise ValueError("quote_at must not exceed available_at")
        if self.bid is not None and self.ask is not None:
            if self.bid > 0 and self.ask > 0 and self.bid > self.ask:
                raise ValueError("Crossed option quote")
        return self

    def dataset_row(self) -> dict:
        contract = self.contract
        return {
            "contract_id": contract.contract_id,
            "underlying_id": contract.underlying_id,
            "option_type": contract.option_type,
            "strike": contract.strike,
            "expiration": contract.expiration,
            "exercise_style": contract.exercise_style,
            "settlement_type": contract.settlement_type,
            "multiplier": contract.multiplier,
            "currency": contract.currency,
            "deliverable": contract.deliverable,
            "adjustment_version": contract.adjustment_version,
            "observed_at": self.observed_at,
            "quote_at": self.quote_at,
            "available_at": self.available_at,
            "bid": self.bid,
            "ask": self.ask,
            "bid_size": self.bid_size,
            "ask_size": self.ask_size,
            "last_price": self.last_price,
            "last_trade_at": self.last_trade_at,
            "volume": self.volume,
            "open_interest": self.open_interest,
            "implied_volatility": self.implied_volatility,
            "source": self.source,
            "source_version": self.source_version,
            "quality_flags": self.quality_flags,
        }


def quote_from_dataset_row(row: dict) -> OptionQuote:
    fields = {
        "underlying_id": row["underlying_id"], "option_type": row["option_type"], "strike": row["strike"],
        "expiration": row["expiration"], "exercise_style": row["exercise_style"],
        "settlement_type": row["settlement_type"], "multiplier": row["multiplier"],
        "currency": row["currency"], "deliverable": row["deliverable"],
        "adjustment_version": row["adjustment_version"],
    }
    from .contracts import option_contract_id
    contract = OptionContract(contract_id=option_contract_id(**fields), **fields)
    def utc(value):
        if value is None:
            return None
        if isinstance(value, str):
            from datetime import datetime
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value
    return OptionQuote(
        contract=contract, observed_at=utc(row["observed_at"]), quote_at=utc(row["quote_at"]),
        available_at=utc(row["available_at"]), bid=row["bid"], ask=row["ask"],
        bid_size=row["bid_size"], ask_size=row["ask_size"], last_price=row["last_price"],
        last_trade_at=utc(row["last_trade_at"]), volume=row["volume"],
        open_interest=row["open_interest"], implied_volatility=row["implied_volatility"],
        source=row["source"], source_version=row["source_version"], quality_flags=row["quality_flags"],
    )
