"""Shared attempt-level time budgets for Alpha training and publication."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from .errors import AlphaError, AlphaErrorCode

TRAINING_DEFAULT_SECONDS = 120
TRAINING_MAX_SECONDS = 1800
PUBLICATION_SECONDS = 30
HARD_TERMINATION_GRACE_SECONDS = 5
TRAINING_RUNTIME_CONTRACT = "training-attempt-deadline-v1"


def normalize_training_request(raw: Any) -> Any:
    """Normalize a new training request before model validation and persistence.

    Existing persisted requests are deliberately never passed through this function:
    they retain their original top-level runtime cap and config hash. The scientific
    request identity already excludes both runtime-budget fields.
    """
    if not isinstance(raw, dict) or raw.get("kind") != "train_alpha_model":
        return raw
    request = dict(raw)
    nested_raw = request.get("resource_budget")
    if nested_raw is not None and not isinstance(nested_raw, dict):
        return request
    nested = dict(nested_raw) if isinstance(nested_raw, dict) else {}
    top_explicit = "max_seconds" in request
    nested_explicit = "max_seconds" in nested
    if top_explicit and nested_explicit and request["max_seconds"] != nested["max_seconds"]:
        raise ValueError("RESOURCE_LIMIT_EXCEEDED: conflicting max_seconds budgets")
    value = nested["max_seconds"] if nested_explicit else request.get("max_seconds", TRAINING_DEFAULT_SECONDS)
    if type(value) is not int or not 1 <= value <= TRAINING_MAX_SECONDS:
        raise ValueError(
            f"RESOURCE_LIMIT_EXCEEDED: training max_seconds must be an integer between 1 and {TRAINING_MAX_SECONDS}"
        )
    request["max_seconds"] = value
    nested["max_seconds"] = value
    request["resource_budget"] = nested
    request["budget_contract_version"] = TRAINING_RUNTIME_CONTRACT
    return request


def effective_training_seconds(request: Any) -> int:
    """Return the attempt execution cap without changing a persisted legacy request."""
    if isinstance(request, dict):
        top = request.get("max_seconds")
        nested = (request.get("resource_budget") or {}).get("max_seconds")
        contract = request.get("budget_contract_version")
    else:
        top = getattr(request, "max_seconds", None)
        budget = getattr(request, "resource_budget", None)
        nested = getattr(budget, "max_seconds", None)
        contract = getattr(request, "budget_contract_version", None)
    if contract == TRAINING_RUNTIME_CONTRACT:
        if type(top) is not int or top != nested:
            raise ValueError("RESOURCE_LIMIT_EXCEEDED: normalized training budget is inconsistent")
        value = top
    else:
        # Legacy requests used the top-level AlphaJobBase cap. Nested 1800 was
        # descriptive/model-local and must not silently enlarge an old Job.
        value = top if type(top) is int else nested
    if type(value) is not int or value < 1:
        raise ValueError("RESOURCE_LIMIT_EXCEEDED: training max_seconds is invalid")
    return value


def hard_attempt_seconds(request: dict[str, Any]) -> int:
    """Supervisor wall-clock cap for one claimed attempt."""
    if request.get("kind") != "train_alpha_model":
        return 125
    seconds = effective_training_seconds(request)
    if request.get("budget_contract_version") == TRAINING_RUNTIME_CONTRACT:
        return seconds + PUBLICATION_SECONDS + HARD_TERMINATION_GRACE_SECONDS
    return seconds + HARD_TERMINATION_GRACE_SECONDS


@dataclass
class AttemptDeadline:
    """Monotonic deadlines consumed by every stage of one training attempt."""

    started: float
    compute_seconds: int
    stage: str = "request"
    publish_deadline: float | None = None
    clock: Callable[[], float] = time.monotonic

    @classmethod
    def for_training_request(
        cls, request: dict[str, Any], *, started: float | None = None, clock: Callable[[], float] = time.monotonic,
    ) -> "AttemptDeadline":
        return cls(started=clock() if started is None else started,
                   compute_seconds=effective_training_seconds(request), clock=clock)

    @property
    def compute_deadline(self) -> float:
        return self.started + self.compute_seconds

    @property
    def hard_deadline(self) -> float:
        grace = PUBLICATION_SECONDS + HARD_TERMINATION_GRACE_SECONDS
        return self.started + self.compute_seconds + grace

    def enter(self, stage: str) -> None:
        self.stage = stage
        self.check(stage)

    def check(self, stage: str | None = None) -> None:
        now = self.clock()
        current = stage or self.stage
        self.stage = current
        if now > self.compute_deadline:
            self._raise(current, now, self.compute_seconds, "computation")

    def begin_publish(self) -> None:
        if self.publish_deadline is None:
            self.publish_deadline = self.clock() + PUBLICATION_SECONDS
        self.enter("publish")

    def check_publish(self, stage: str = "publish") -> None:
        self.stage = stage
        if self.publish_deadline is None:
            raise RuntimeError("Publication budget was not started")
        now = self.clock()
        if now > self.publish_deadline:
            self._raise(stage, now, PUBLICATION_SECONDS, "publication")

    def _raise(self, stage: str, now: float, budget: int, phase: str) -> None:
        raise AlphaError(
            AlphaErrorCode.RESOURCE_LIMIT_EXCEEDED,
            f"Alpha {phase} deadline exceeded during {stage}",
            {
                "stage": stage,
                "elapsed_seconds": round(now - self.started, 6),
                "effective_budget_seconds": budget,
                "phase": phase,
            },
        )
