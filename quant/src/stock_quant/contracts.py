"""Strict external DSL contracts. Bool and numeric constants are distinct."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, model_validator

Timeframe = Literal["1m", "5m", "15m", "1h", "1d", "1w"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class Const(StrictModel):
    kind: Literal["const"]
    value: StrictBool | StrictInt | StrictFloat


class Ref(StrictModel):
    kind: Literal["ref"]
    symbol: str = Field(min_length=1, max_length=64)
    field: Literal["open", "high", "low", "close", "volume"]
    timeframe: Timeframe
    adjustment: Literal["raw", "split_asof"] = "raw"


class Param(StrictModel):
    kind: Literal["param"]
    name: str = Field(min_length=1, max_length=64)


class ParameterDefinition(StrictModel):
    value_type: Literal["integer", "number"]
    default: StrictInt | StrictFloat
    minimum: StrictInt | StrictFloat
    maximum: StrictInt | StrictFloat

    @model_validator(mode="after")
    def bounds(self):
        if self.minimum > self.maximum or not self.minimum <= self.default <= self.maximum:
            raise ValueError("Invalid parameter bounds/default")
        if self.value_type == "integer" and any(type(v) is not int for v in (self.default, self.minimum, self.maximum)):
            raise ValueError("Integer parameters require integer bounds/default")
        return self


class Signal(StrictModel):
    kind: Literal["signal"]
    name: str = Field(min_length=1, max_length=64)


class Binary(StrictModel):
    kind: Literal["binary"]
    op: Literal["add", "sub", "mul", "div", "gt", "gte", "lt", "lte", "eq", "and", "or"]
    left: Expr
    right: Expr


class Call(StrictModel):
    kind: Literal["call"]
    operator: str
    args: list[Expr] = Field(min_length=1, max_length=2)
    params: dict[str, StrictInt | StrictFloat | str | StrictBool | Param] = Field(default_factory=dict)


class Align(StrictModel):
    kind: Literal["align"]
    value: Expr
    timeframe: Timeframe
    policy: Literal["last_completed"]


Expr = Annotated[Const | Ref | Signal | Param | Binary | Call | Align, Field(discriminator="kind")]
for model in (Binary, Call, Align):
    model.model_rebuild()


class Strategy(StrictModel):
    schema_version: Literal["1.0"]
    name: str = Field(min_length=1, max_length=128)
    decision_timeframe: Timeframe
    parameters: dict[str, ParameterDefinition] = Field(default_factory=dict, max_length=16)
    signals: dict[str, Expr] = Field(default_factory=dict, max_length=256)
    entry: Expr
    exit: Expr


class Period(StrictModel):
    period: StrictInt = Field(ge=1, le=10000)
