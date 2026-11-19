"""Chronological train/validation/test splits with purge and embargo."""
from __future__ import annotations

from datetime import timedelta

from pydantic import AwareDatetime, Field, model_validator

from ...contracts import StrictModel
from .labels import LabelRow


class ModelDevelopmentSplit(StrictModel):
    """Train/validation-only research split. Test boundaries stay outside the training contract."""
    train_start: AwareDatetime
    train_end: AwareDatetime
    validation_start: AwareDatetime
    validation_end: AwareDatetime
    embargo_days: int = Field(default=0, ge=0, le=252, strict=True)

    @model_validator(mode="after")
    def ordered(self):
        if not self.train_start < self.train_end <= self.validation_start < self.validation_end:
            raise ValueError("Expected ordered non-overlapping train/validation intervals")
        return self


class TemporalSplit(StrictModel):
    train_start: AwareDatetime
    train_end: AwareDatetime
    validation_start: AwareDatetime
    validation_end: AwareDatetime
    test_start: AwareDatetime
    test_end: AwareDatetime
    embargo_days: int = Field(default=0, ge=0, le=252, strict=True)

    @model_validator(mode="after")
    def ordered(self):
        if not self.train_start < self.train_end <= self.validation_start < self.validation_end <= self.test_start < self.test_end:
            raise ValueError("Expected ordered non-overlapping train/validation/test intervals")
        return self


def split_matured_labels(rows: list[LabelRow], split: TemporalSplit) -> dict[str, list[LabelRow]]:
    result = {}
    for name, start, end in (
        ("train", split.train_start, split.train_end),
        ("validation", split.validation_start, split.validation_end),
        ("test", split.test_start, split.test_end),
    ):
        effective_end = end - timedelta(days=split.embargo_days)
        result[name] = [
            row for row in rows
            if row.label_value is not None
            and start <= row.feature_decision_at < effective_end
            and row.label_available_at <= effective_end
        ]
    return result


def split_matured_development(rows: list[LabelRow], split: ModelDevelopmentSplit) -> dict[str, list[LabelRow]]:
    """Return only matured development labels; no test labels are generated or read."""
    result = {}
    for name, start, end in (
        ("train", split.train_start, split.train_end),
        ("validation", split.validation_start, split.validation_end),
    ):
        effective_end = end - timedelta(days=split.embargo_days)
        result[name] = [
            row for row in rows
            if row.label_value is not None
            and start <= row.feature_decision_at < effective_end
            and row.label_available_at <= effective_end
        ]
    return result
