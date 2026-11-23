"""Stable error vocabulary shared by jobs, Python runtime and MCP tools."""
from __future__ import annotations

from enum import StrEnum
from typing import Any


class AlphaErrorCode(StrEnum):
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"
    PRICE_BASIS_UNVERIFIED = "PRICE_BASIS_UNVERIFIED"
    OPTION_HISTORY_UNAVAILABLE = "OPTION_HISTORY_UNAVAILABLE"
    INVALID_OPTION_QUOTE = "INVALID_OPTION_QUOTE"
    LABEL_NOT_MATURED = "LABEL_NOT_MATURED"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    MODEL_NOT_FOUND = "MODEL_NOT_FOUND"
    DATASET_MISMATCH = "DATASET_MISMATCH"
    UNSUPPORTED_EXECUTION_MODEL = "UNSUPPORTED_EXECUTION_MODEL"
    RESOURCE_LIMIT_EXCEEDED = "RESOURCE_LIMIT_EXCEEDED"
    PROVIDER_ACCESS_DENIED = "PROVIDER_ACCESS_DENIED"
    DATA_LICENSE_REQUIRED = "DATA_LICENSE_REQUIRED"
    RESEARCH_MODE_REQUIRED = "RESEARCH_MODE_REQUIRED"
    LEGACY_INLINE_TEST_DISABLED = "LEGACY_INLINE_TEST_DISABLED"
    TEST_WINDOW_NOT_ALLOWED = "TEST_WINDOW_NOT_ALLOWED"
    RESEARCH_PROTOCOL_REQUIRED = "RESEARCH_PROTOCOL_REQUIRED"
    STOCK_LABEL_DATASET_REQUIRED = "STOCK_LABEL_DATASET_REQUIRED"
    RESEARCH_PREDICTION_REQUIRES_FREEZE = "RESEARCH_PREDICTION_REQUIRES_FREEZE"
    FROZEN_OOS_AUTHORIZATION_INVALID = "FROZEN_OOS_AUTHORIZATION_INVALID"
    QUALITY_SCOPE_NOT_ISOLATED = "QUALITY_SCOPE_NOT_ISOLATED"


class AlphaError(Exception):
    def __init__(self, code: AlphaErrorCode | str, message: str, context: dict[str, Any] | None = None):
        self.code = AlphaErrorCode(code)
        self.message = message
        self.context = context or {}
        super().__init__(f"{self.code.value}: {message}")

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code.value, "message": self.message}
        if self.context:
            result["context"] = self.context
        return result
