"""Convert the existing Yahoo live option-chain payload into append-only quote rows."""
from __future__ import annotations

from datetime import date, datetime, timezone

from ..errors import AlphaError, AlphaErrorCode
from ..options.contracts import OptionContract, option_contract_id
from ..options.quotes import OptionQuote


class YahooOptionChainImport:
    def __init__(self, *, observed_at: datetime | None = None, source_version: str = "yahoo-option-chain-v1"):
        self.observed_at = (observed_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        self.source_version = source_version

    def convert(self, payload: dict) -> list[OptionQuote]:
        symbol = str(payload.get("symbol", "")).upper()
        if not symbol:
            raise AlphaError(AlphaErrorCode.DATA_UNAVAILABLE, "Yahoo option chain has no underlying")
        as_of = payload.get("asOf")
        observed = datetime.fromisoformat(as_of.replace("Z", "+00:00")).astimezone(timezone.utc) if as_of else self.observed_at
        currency = str(payload.get("underlying", {}).get("currency") or "USD").upper()
        rows = []
        for leg in payload.get("legs", []):
            expiration = date.fromisoformat(leg["expiration"])
            fields = {
                "underlying_id": symbol, "option_type": leg["optionType"],
                "strike": float(leg["strike"]), "expiration": expiration,
                "exercise_style": "american", "settlement_type": "physical",
                "multiplier": 100, "currency": currency,
                "deliverable": f"100 shares {symbol}", "adjustment_version": "yahoo-standard-v1",
            }
            contract = OptionContract(contract_id=option_contract_id(**fields), **fields)
            rows.append(OptionQuote(
                contract=contract, observed_at=observed, quote_at=None, available_at=observed,
                bid=leg.get("bid"), ask=leg.get("ask"), bid_size=leg.get("bidSize"), ask_size=leg.get("askSize"),
                last_price=leg.get("lastPrice"), last_trade_at=datetime.fromisoformat(leg["lastTradeDate"].replace("Z", "+00:00")) if leg.get("lastTradeDate") else None,
                volume=leg.get("volume"), open_interest=leg.get("openInterest"),
                implied_volatility=leg.get("impliedVol"), source="yahoo", source_version=self.source_version,
                quality_flags=["QUOTE_EVENT_TIME_UNKNOWN"],
            ))
        return rows
