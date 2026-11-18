"""Option lifecycle policy: close before expiry; no synthetic assignment model."""
from __future__ import annotations

from datetime import datetime

from ..errors import AlphaError, AlphaErrorCode


def require_close_before_expiry(decision_at: datetime, expiration_at: datetime, *, close_before_expiry: bool = True) -> None:
    if close_before_expiry and decision_at >= expiration_at:
        raise AlphaError(AlphaErrorCode.UNSUPPORTED_EXECUTION_MODEL, "Option position reached expiration without a valid close quote")
