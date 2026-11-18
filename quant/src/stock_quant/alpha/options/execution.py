"""Executable option quote lookup. No mid/last/intrinsic substitution."""
from __future__ import annotations

from datetime import datetime

from ..errors import AlphaError, AlphaErrorCode
from .quotes import OptionQuote


def next_ask(quotes: list[OptionQuote], after: datetime) -> OptionQuote | None:
    for quote in sorted(quotes, key=lambda item: item.quote_at or item.observed_at):
        at = max(quote.quote_at or quote.observed_at, quote.available_at)
        if at > after and quote.ask is not None and quote.ask > 0 and quote.ask_size is not None and quote.ask_size > 0:
            return quote
    return None


def next_exit_bid(quotes: list[OptionQuote], *, after: datetime, expiration_at: datetime, close_before_expiry_days: int = 0) -> OptionQuote | None:
    cutoff = expiration_at
    if close_before_expiry_days:
        from datetime import timedelta
        cutoff -= timedelta(days=close_before_expiry_days)
    for quote in sorted(quotes, key=lambda item: item.quote_at or item.observed_at):
        at = max(quote.quote_at or quote.observed_at, quote.available_at)
        if at >= after and at < cutoff and quote.bid is not None and quote.bid > 0 and quote.bid_size is not None and quote.bid_size > 0:
            return quote
    return None
