"""Bounded data-quality findings for Alpha datasets."""
from __future__ import annotations

from typing import Any

from pydantic import Field

from ...contracts import StrictModel
from ..contracts import Hash


class DataQualityReport(StrictModel):
    dataset_id: Hash
    valid: bool
    row_count: int = Field(ge=0)
    partition_count: int = Field(ge=0)
    coverage: float = Field(ge=0, le=1)
    symbol_presence_coverage: float | None = Field(default=None, ge=0, le=1)
    session_coverage: float | None = Field(default=None, ge=0, le=1)
    session_coverage_by_symbol: list[dict[str, Any]] = Field(default_factory=list)
    issues: list[dict[str, Any]] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
