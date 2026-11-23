"""Authoritative research admission policy shared by jobs, workers and trainers."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .data.catalog import DatasetStore
from .errors import AlphaError, AlphaErrorCode
from .models.registry import AlphaModelRegistry
from .models.research_freeze import (
    FrozenResearchRecord,
    FrozenResearchStore,
    validate_frozen_record,
)
from .models.temporal_split import ModelDevelopmentSplit


class ResearchAdmissionError(AlphaError):
    """Stable admission failure with the request fields that caused it."""

    def __init__(self, code: AlphaErrorCode, message: str, fields: list[str], context: dict | None = None):
        self.fields = fields
        super().__init__(code, message, context)


_OOS_GRANT_SEAL = object()


@dataclass(frozen=True)
class _FrozenOosGrant:
    record: FrozenResearchRecord
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self):
        if self._seal is not _OOS_GRANT_SEAL:
            raise ValueError("Frozen OOS grant was not issued by ResearchAdmission")


def _issue_frozen_oos_grant(record: FrozenResearchRecord) -> _FrozenOosGrant:
    return _FrozenOosGrant(record=record, _seal=_OOS_GRANT_SEAL)


def _training_mode(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def require_training_submission_raw(raw: Any) -> None:
    """Reject unsafe research modes before Pydantic can supply or normalize defaults."""
    if not isinstance(raw, dict):
        return
    mode = _training_mode(raw.get("evaluation_mode"))
    if mode is None:
        raise ResearchAdmissionError(
            AlphaErrorCode.RESEARCH_MODE_REQUIRED,
            "Alpha training requires evaluation_mode=validation_only",
            ["evaluation_mode"],
        )
    if mode == "legacy_inline_test":
        raise ResearchAdmissionError(
            AlphaErrorCode.LEGACY_INLINE_TEST_DISABLED,
            "legacy_inline_test is readable for historical artifacts but cannot start a new job",
            ["evaluation_mode"],
        )
    if mode != "validation_only":
        raise ResearchAdmissionError(
            AlphaErrorCode.RESEARCH_MODE_REQUIRED,
            "Alpha training requires evaluation_mode=validation_only",
            ["evaluation_mode"],
        )
    split = raw.get("split")
    if isinstance(split, dict):
        test_fields = [f"split.{field}" for field in ("test_start", "test_end") if field in split]
        if test_fields:
            raise ResearchAdmissionError(
                AlphaErrorCode.TEST_WINDOW_NOT_ALLOWED,
                "Formal Alpha training accepts only train and validation windows",
                test_fields,
            )
    if not raw.get("research_protocol_hash"):
        raise ResearchAdmissionError(
            AlphaErrorCode.RESEARCH_PROTOCOL_REQUIRED,
            "Formal Alpha training requires research_protocol_hash",
            ["research_protocol_hash"],
        )
    if not raw.get("stock_dataset_id"):
        raise ResearchAdmissionError(
            AlphaErrorCode.STOCK_LABEL_DATASET_REQUIRED,
            "Formal stock Alpha training requires a stock label dataset",
            ["stock_dataset_id"],
        )


def require_training_execution(request: Any) -> None:
    """Authorize a validated training request before it can read training data."""
    mode = _training_mode(getattr(request, "evaluation_mode", None))
    if mode is None:
        raise ResearchAdmissionError(
            AlphaErrorCode.RESEARCH_MODE_REQUIRED,
            "Alpha training requires evaluation_mode=validation_only",
            ["evaluation_mode"],
        )
    if mode == "legacy_inline_test":
        raise ResearchAdmissionError(
            AlphaErrorCode.LEGACY_INLINE_TEST_DISABLED,
            "legacy_inline_test cannot execute a new training run",
            ["evaluation_mode"],
        )
    if mode != "validation_only":
        raise ResearchAdmissionError(
            AlphaErrorCode.RESEARCH_MODE_REQUIRED,
            "Alpha training requires evaluation_mode=validation_only",
            ["evaluation_mode"],
        )
    split = getattr(request, "split", None)
    if not isinstance(split, ModelDevelopmentSplit) or hasattr(split, "test_start") or hasattr(split, "test_end"):
        raise ResearchAdmissionError(
            AlphaErrorCode.TEST_WINDOW_NOT_ALLOWED,
            "Formal Alpha training requires a train/validation split without Test windows",
            ["split"],
        )
    if not getattr(request, "research_protocol_hash", None):
        raise ResearchAdmissionError(
            AlphaErrorCode.RESEARCH_PROTOCOL_REQUIRED,
            "Formal Alpha training requires research_protocol_hash",
            ["research_protocol_hash"],
        )
    label_source = getattr(request, "stock_dataset_id", None) or getattr(request, "label_source_dataset_id", None)
    if not label_source:
        raise ResearchAdmissionError(
            AlphaErrorCode.STOCK_LABEL_DATASET_REQUIRED,
            "Formal stock Alpha training requires a stock label dataset",
            ["stock_dataset_id"],
        )


def require_public_prediction(metadata: dict) -> None:
    """Keep formal research models behind the completed-freeze OOS boundary.

    Historical legacy or mode-less models remain intentionally available for
    compatibility inference. They cannot be selected by a new validation-only
    Freeze and therefore cannot provide formal OOS evidence.
    """
    mode = metadata.get("evaluation_mode")
    if mode == "validation_only":
        raise AlphaError(
            AlphaErrorCode.RESEARCH_PREDICTION_REQUIRES_FREEZE,
            "Validation-only research models cannot be used through ordinary prediction",
            {"model_set_id": metadata.get("model_set_id")},
        )
    if mode not in {None, "legacy_inline_test"}:
        raise AlphaError(
            AlphaErrorCode.RESEARCH_PREDICTION_REQUIRES_FREEZE,
            "Model research mode is not authorized for ordinary prediction",
            {"model_set_id": metadata.get("model_set_id"), "evaluation_mode": mode},
        )


def authorize_oos(
    freeze_id: str,
    *,
    job_store,
    datasets: DatasetStore,
    model_registry: AlphaModelRegistry,
) -> _FrozenOosGrant:
    """Resolve the only trusted OOS grant: a completed Freeze Job and immutable record."""
    completed = job_store.completed_freeze_job(freeze_id)
    if completed is None:
        raise AlphaError(
            AlphaErrorCode.FROZEN_OOS_AUTHORIZATION_INVALID,
            "OOS evaluation requires a completed Alpha freeze job",
            {"freeze_id": freeze_id},
        )
    try:
        completed_record = FrozenResearchRecord.model_validate(completed["record"])
        if completed_record.freeze_id != freeze_id:
            raise ValueError("Completed freeze job returned a different freeze ID")
        freeze_store = FrozenResearchStore(Path(job_store.path).with_suffix(".freezes"))
        record = freeze_store.materialize(completed_record)
        validated = validate_frozen_record(record, store=datasets, model_registry=model_registry)
        return _issue_frozen_oos_grant(validated)
    except AlphaError as error:
        if error.code == AlphaErrorCode.FROZEN_OOS_AUTHORIZATION_INVALID:
            raise
        raise AlphaError(
            AlphaErrorCode.FROZEN_OOS_AUTHORIZATION_INVALID,
            "Frozen OOS authorization is invalid",
            {
                "freeze_id": freeze_id,
                "reason": error.code.value,
                "message": error.message,
                "context": error.context,
            },
        ) from error
    except Exception as error:
        raise AlphaError(
            AlphaErrorCode.FROZEN_OOS_AUTHORIZATION_INVALID,
            "Frozen OOS authorization is invalid",
            {"freeze_id": freeze_id, "reason": str(error)},
        ) from error


_WALK_FORWARD_FORBIDDEN_RAW_FIELDS = frozenset({
    "test_start",
    "test_end",
    "test_feature_set_id",
    "test_label_source_dataset_id",
    "freeze_id",
    "oos_grant",
})


def _forbidden_walk_forward_fields(value: Any, path: str = "") -> list[str]:
    fields: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}" if path else key
            if key in _WALK_FORWARD_FORBIDDEN_RAW_FIELDS:
                fields.append(child)
            fields.extend(_forbidden_walk_forward_fields(item, child))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            fields.extend(_forbidden_walk_forward_fields(item, f"{path}[{index}]"))
    return sorted(set(fields))


def require_walk_forward_submission_raw(raw: Any) -> None:
    """Reject unsafe Walk-forward submissions before Pydantic normalizes defaults."""
    if not isinstance(raw, dict):
        return
    mode = _training_mode(raw.get("evaluation_mode"))
    if mode is None or mode not in {"validation_only", "legacy_inline_test"}:
        raise ResearchAdmissionError(
            AlphaErrorCode.RESEARCH_MODE_REQUIRED,
            "Alpha walk-forward requires evaluation_mode=validation_only",
            ["evaluation_mode"],
        )
    if mode == "legacy_inline_test":
        raise ResearchAdmissionError(
            AlphaErrorCode.LEGACY_INLINE_TEST_DISABLED,
            "legacy_inline_test cannot start a Walk-forward job",
            ["evaluation_mode"],
        )
    forbidden = _forbidden_walk_forward_fields(raw)
    if forbidden:
        raise ResearchAdmissionError(
            AlphaErrorCode.TEST_WINDOW_NOT_ALLOWED,
            "Walk-forward accepts only a development window and cannot carry Test, Freeze or OOS fields",
            forbidden[:20],
        )
    if not raw.get("research_protocol_hash"):
        raise ResearchAdmissionError(
            AlphaErrorCode.RESEARCH_PROTOCOL_REQUIRED,
            "Walk-forward requires research_protocol_hash",
            ["research_protocol_hash"],
        )
    label_spec = raw.get("label_spec") if isinstance(raw.get("label_spec"), dict) else {}
    if not raw.get("stock_dataset_id") and not label_spec.get("stock_dataset_id"):
        raise ResearchAdmissionError(
            AlphaErrorCode.STOCK_LABEL_DATASET_REQUIRED,
            "Walk-forward requires a stock label dataset",
            ["stock_dataset_id"],
        )


def require_walk_forward_execution(request: Any) -> None:
    """Authorize a validated Walk-forward request before any dataset read."""
    mode = _training_mode(getattr(request, "evaluation_mode", None))
    if mode is None or mode not in {"validation_only", "legacy_inline_test"}:
        raise ResearchAdmissionError(
            AlphaErrorCode.RESEARCH_MODE_REQUIRED,
            "Alpha walk-forward requires evaluation_mode=validation_only",
            ["evaluation_mode"],
        )
    if mode == "legacy_inline_test":
        raise ResearchAdmissionError(
            AlphaErrorCode.LEGACY_INLINE_TEST_DISABLED,
            "legacy_inline_test cannot execute a Walk-forward job",
            ["evaluation_mode"],
        )
    forbidden = [
        field for field in _WALK_FORWARD_FORBIDDEN_RAW_FIELDS
        if hasattr(request, field)
    ]
    development = getattr(request, "development_window", None)
    if development is None or any(hasattr(development, field) for field in _WALK_FORWARD_FORBIDDEN_RAW_FIELDS):
        forbidden.append("development_window")
    if forbidden:
        raise ResearchAdmissionError(
            AlphaErrorCode.TEST_WINDOW_NOT_ALLOWED,
            "Walk-forward request contains a forbidden Test, Freeze or OOS field",
            sorted(set(forbidden)),
        )
    if not getattr(request, "research_protocol_hash", None):
        raise ResearchAdmissionError(
            AlphaErrorCode.RESEARCH_PROTOCOL_REQUIRED,
            "Walk-forward requires research_protocol_hash",
            ["research_protocol_hash"],
        )
    label_spec = getattr(request, "label_spec", None)
    stock_dataset_id = getattr(request, "stock_dataset_id", None)
    label_stock_id = getattr(label_spec, "stock_dataset_id", None)
    if not stock_dataset_id or not label_stock_id:
        raise ResearchAdmissionError(
            AlphaErrorCode.STOCK_LABEL_DATASET_REQUIRED,
            "Walk-forward requires a stock label dataset",
            ["stock_dataset_id"],
        )
    if stock_dataset_id != label_stock_id:
        raise ResearchAdmissionError(
            AlphaErrorCode.DATASET_MISMATCH,
            "Walk-forward stock_dataset_id and label_spec.stock_dataset_id must match",
            ["stock_dataset_id", "label_spec.stock_dataset_id"],
        )
