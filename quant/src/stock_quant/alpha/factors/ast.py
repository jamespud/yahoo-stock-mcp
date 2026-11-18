"""FactorSpec v1: a flat, ID-referenced DAG."""
from __future__ import annotations

from typing import Literal

from pydantic import Field, StrictFloat, StrictInt, model_validator

from ...contracts import StrictModel


class FactorNode(StrictModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    operator: str = Field(min_length=1, max_length=64)
    inputs: list[str] = Field(min_length=1, max_length=2)
    params: dict[str, StrictInt | StrictFloat] = Field(default_factory=dict)


class FactorSpec(StrictModel):
    schema_version: Literal["1.0"]
    timeframe: Literal["1d"]
    nodes: list[FactorNode] = Field(min_length=1, max_length=128)
    outputs: list[str] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def outputs_exist(self):
        ids = {node.id for node in self.nodes}
        if len(ids) != len(self.nodes):
            # Compiler owns the stable diagnostic.
            return self
        if len(set(self.outputs)) != len(self.outputs) or any(output not in ids for output in self.outputs):
            raise ValueError("Outputs must be unique node IDs")
        return self
