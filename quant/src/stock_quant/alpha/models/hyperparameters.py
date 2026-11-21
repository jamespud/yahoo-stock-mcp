"""Shared admission contract for LightGBM jobs and direct trainer calls."""
from pydantic import Field

from ...contracts import StrictModel

INT32_MAX = 2_147_483_647


class LightGBMHyperparameters(StrictModel):
    learning_rate: float = Field(0.05, strict=True, gt=0, allow_inf_nan=False)
    num_leaves: int = Field(31, strict=True, ge=2, le=131072)
    max_depth: int = Field(-1, strict=True, ge=-INT32_MAX - 1, le=INT32_MAX)
    min_child_samples: int = Field(20, strict=True, ge=0, le=INT32_MAX)
    n_estimators: int = Field(100, strict=True, ge=1, le=INT32_MAX)


def validate_hyperparameters(value):
    LightGBMHyperparameters.model_validate(value)
    # Keep omitted defaults and submitted JSON values out of identity changes.
    return value
