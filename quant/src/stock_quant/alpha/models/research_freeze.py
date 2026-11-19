"""Content-addressed immutable research freeze records.

A freeze is a validation-selected research decision. The frozen record never
becomes a training input; it is only the permission boundary for one OOS job.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from pydantic import AwareDatetime, Field, model_validator

from ...compiler import digest
from ...contracts import StrictModel
from ..contracts import Hash
from ..data.catalog import DatasetStore
from ..errors import AlphaError, AlphaErrorCode
from .registry import AlphaModelRegistry


class FreezeRequest(StrictModel):
    research_protocol_hash: Hash
    candidate_ledger_hash: Hash
    selected_model_ids: list[Hash] = Field(min_length=1, max_length=1)
    test_feature_set_id: Hash
    test_label_source_dataset_id: Hash
    test_start: AwareDatetime
    test_end: AwareDatetime
    selection_reason: str = Field(min_length=1, max_length=1024)
    label_name: str = Field(min_length=1, max_length=128)
    target_type: str = Field(default="stock_return", pattern=r"^(stock_return|option_return|option_iv_change|stock_return_with_option_features)$")
    horizon_days: int = Field(default=5, ge=1, le=252, strict=True)

    @model_validator(mode="after")
    def ordered(self):
        if self.test_start >= self.test_end:
            raise ValueError("Expected test_start < test_end")
        if len(self.selected_model_ids) != len(set(self.selected_model_ids)):
            raise ValueError("Selected model IDs must be unique")
        return self


class FrozenResearchRecord(StrictModel):
    schema_version: str = "1.0"
    freeze_id: Hash
    research_protocol_hash: Hash
    candidate_ledger_hash: Hash
    frozen_model_ids: list[Hash]
    model_hashes: dict[Hash, Hash]
    test_dataset_hashes: dict[str, Hash]
    test_dataset_ids: dict[str, Hash]
    test_window: dict
    label_spec: dict
    selection_reason: str
    frozen_at: datetime


class FrozenResearchStore:
    """Append-only content-addressed JSON store. Rewrites of an existing ID are rejected."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def path(self, freeze_id: str) -> Path:
        if not isinstance(freeze_id, str) or not freeze_id.startswith("sha256:") or "/" in freeze_id or "\\" in freeze_id or ".." in freeze_id:
            raise ValueError("Invalid freeze ID")
        return self.root / (freeze_id.removeprefix("sha256:") + ".json")

    def load(self, freeze_id: str) -> FrozenResearchRecord:
        path = self.path(freeze_id)
        if not path.is_file():
            raise AlphaError(AlphaErrorCode.MODEL_NOT_FOUND, "Freeze record is missing", {"freeze_id": freeze_id})
        try:
            record = FrozenResearchRecord.model_validate_json(path.read_text())
            if record.freeze_id != freeze_id or freeze_identity(record) != freeze_id:
                raise ValueError("Freeze identity mismatch")
            return record
        except Exception as error:
            raise AlphaError(AlphaErrorCode.MODEL_NOT_FOUND, "Freeze record integrity check failed", {"freeze_id": freeze_id, "reason": str(error)}) from error

    def rewrite(self, freeze_id, request: FreezeRequest, *, model_hashes: dict[str, str], test_dataset_hashes: dict[str, str]) -> FrozenResearchRecord:
        if self.path(freeze_id).exists():
            raise AlphaError(AlphaErrorCode.MODEL_NOT_FOUND, "Freeze record is immutable", {"freeze_id": freeze_id})
        raise AlphaError(AlphaErrorCode.MODEL_NOT_FOUND, "Unknown freeze record", {"freeze_id": freeze_id})

    def save(self, record: FrozenResearchRecord, *, checkpoint: Callable[[], None] | None = None) -> FrozenResearchRecord:
        if checkpoint:
            checkpoint()
        path = self.path(record.freeze_id)
        if path.exists():
            existing = self.load(record.freeze_id)
            if freeze_identity(existing) != freeze_identity(record):
                raise AlphaError(AlphaErrorCode.MODEL_NOT_FOUND, "Freeze record is immutable", {"freeze_id": record.freeze_id})
            return existing
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        stage = Path(tempfile.mkstemp(prefix=".freeze-", suffix=".tmp", dir=self.root)[1])
        try:
            stage.write_text(json.dumps(record.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), allow_nan=False))
            with stage.open("rb") as stream:
                os.fsync(stream.fileno())
            if checkpoint:
                checkpoint()
            os.replace(stage, path)
        finally:
            if stage.exists():
                stage.unlink()
        return record


def freeze_identity(record: FrozenResearchRecord) -> str:
    body = record.model_dump(mode="json", exclude={"freeze_id", "frozen_at"})
    return digest(body)


def _parse_window_time(value, field: str, model_id: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise AlphaError(
            AlphaErrorCode.DATASET_MISMATCH,
            "Selected model has an invalid validation window",
            {"model_id": model_id, "field": field},
        ) from error
    if parsed.tzinfo is None:
        raise AlphaError(
            AlphaErrorCode.DATASET_MISMATCH,
            "Selected model has a timezone-naive validation window",
            {"model_id": model_id, "field": field},
        )
    return parsed


def _model_hash(
    registry: AlphaModelRegistry,
    model_id: str,
    *,
    request: FreezeRequest,
    store: DatasetStore,
    test_feature,
    test_label,
) -> str:
    try:
        loaded = registry.load(model_id)
    except AlphaError as error:
        raise AlphaError(AlphaErrorCode.MODEL_NOT_FOUND, "Selected model artifact is unreadable", {"model_id": model_id, "reason": str(error)}) from error
    metadata = loaded.metadata
    if metadata.get("evaluation_mode") != "validation_only":
        raise AlphaError(AlphaErrorCode.DATASET_MISMATCH, "Selected model lacks validation-only evidence", {"model_id": model_id})
    metrics = metadata.get("validation_metrics", {})
    if "oos" in metadata or "oos" in metrics:
        raise AlphaError(AlphaErrorCode.DATASET_MISMATCH, "Validation-only model contains OOS metrics", {"model_id": model_id})
    if not isinstance(metadata.get("feature_schema"), list) or not metadata["feature_schema"]:
        raise AlphaError(AlphaErrorCode.MODEL_NOT_FOUND, "Model has no frozen feature schema", {"model_id": model_id})

    expected_label_definition = {
        "label_name": request.label_name,
        "target_type": request.target_type,
        "horizon_days": request.horizon_days,
    }
    metadata_label_spec = metadata.get("label_spec") if isinstance(metadata.get("label_spec"), dict) else {}
    actual_label_definition = {key: metadata_label_spec.get(key) for key in expected_label_definition}
    training_label_source_id = metadata.get("label_source_dataset_id")

    mismatches = []
    def mismatch(field, expected, actual):
        if expected != actual:
            mismatches.append({"field": field, "expected": expected, "actual": actual})

    mismatch("research_protocol_hash", request.research_protocol_hash, metadata.get("research_protocol_hash"))
    mismatch("label_spec", expected_label_definition, actual_label_definition)
    mismatch("label_spec.label_source_dataset_id", training_label_source_id, metadata_label_spec.get("label_source_dataset_id"))
    mismatch("factor_spec_hash", test_feature.source_version, metadata.get("factor_spec_hash"))
    mismatch("calendar_version", test_feature.calendar_version, metadata.get("calendar_version"))

    if isinstance(training_label_source_id, str):
        try:
            training_labels = store.load_manifest(training_label_source_id)
        except Exception:
            training_labels = None
        if training_labels is None:
            mismatches.append({
                "field": "label_source_dataset_id", "expected": "loadable dataset", "actual": training_label_source_id,
            })
        else:
            for field in ("dataset_kind", "source", "source_version", "calendar_version"):
                mismatch(f"label_source.{field}", getattr(test_label, field), getattr(training_labels, field))
    else:
        mismatches.append({
            "field": "label_source_dataset_id", "expected": "sha256:...", "actual": training_label_source_id,
        })

    training_feature_set_id = metadata.get("feature_set_id")
    if isinstance(training_feature_set_id, str):
        try:
            training_feature = store.load_manifest(training_feature_set_id)
        except Exception:
            training_feature = None
        if training_feature is None:
            mismatches.append({
                "field": "feature_set_id", "expected": "loadable dataset", "actual": training_feature_set_id,
            })
        else:
            mismatch("training_factor_spec_hash", training_feature.source_version, metadata.get("factor_spec_hash"))
    else:
        mismatches.append({"field": "feature_set_id", "expected": "sha256:...", "actual": training_feature_set_id})

    validation_window = metadata.get("validation_window")
    validation_end = validation_window.get("validation_end") if isinstance(validation_window, dict) else None
    parsed_validation_end = _parse_window_time(validation_end, "validation_end", model_id)
    if request.test_start < parsed_validation_end:
        mismatches.append({
            "field": "test_start",
            "expected": f">={parsed_validation_end.isoformat()}",
            "actual": request.test_start.isoformat(),
        })
    mismatch("test_feature_kind", "feature_panel", test_feature.dataset_kind)
    mismatch("test_label_kind", "stock_bars", test_label.dataset_kind)

    if mismatches:
        raise AlphaError(
            AlphaErrorCode.DATASET_MISMATCH,
            "Freeze request does not match the validation-selected model",
            {"model_id": model_id, "mismatches": mismatches},
        )
    return metadata["model_hash"]


def freeze_alpha_selection(
    request: FreezeRequest,
    *,
    store: DatasetStore,
    model_registry: AlphaModelRegistry,
    freeze_store: FrozenResearchStore | None = None,
    checkpoint: Callable[[], None] | None = None,
) -> FrozenResearchRecord:
    test_feature = store.load_manifest(request.test_feature_set_id)
    test_label = store.load_manifest(request.test_label_source_dataset_id)
    model_hashes = {
        model_id: _model_hash(
            model_registry, model_id, request=request, store=store,
            test_feature=test_feature, test_label=test_label,
        )
        for model_id in request.selected_model_ids
    }
    test_dataset_hashes = {
        "test_feature_set_id": test_feature.content_hash,
        "test_label_source_dataset_id": test_label.content_hash,
    }
    body = {
        "schema_version": "1.0",
        "research_protocol_hash": request.research_protocol_hash,
        "candidate_ledger_hash": request.candidate_ledger_hash,
        "frozen_model_ids": list(request.selected_model_ids),
        "model_hashes": model_hashes,
        "test_dataset_hashes": test_dataset_hashes,
        "test_dataset_ids": {"test_feature_set_id": request.test_feature_set_id, "test_label_source_dataset_id": request.test_label_source_dataset_id},
        "test_window": {"start": request.test_start.isoformat(), "end": request.test_end.isoformat()},
        "label_spec": {
            "label_name": request.label_name, "target_type": request.target_type,
            "horizon_days": request.horizon_days,
            "label_source_dataset_id": request.test_label_source_dataset_id,
        },
        "selection_reason": request.selection_reason,
    }
    freeze_id = digest(body)
    record = FrozenResearchRecord(
        freeze_id=freeze_id,
        research_protocol_hash=request.research_protocol_hash,
        candidate_ledger_hash=request.candidate_ledger_hash,
        frozen_model_ids=list(request.selected_model_ids),
        model_hashes=model_hashes,
        test_dataset_hashes=test_dataset_hashes,
        test_dataset_ids={"test_feature_set_id": request.test_feature_set_id, "test_label_source_dataset_id": request.test_label_source_dataset_id},
        test_window={"start": request.test_start.isoformat(), "end": request.test_end.isoformat()},
        label_spec={
            "label_name": request.label_name, "target_type": request.target_type,
            "horizon_days": request.horizon_days,
            "label_source_dataset_id": request.test_label_source_dataset_id,
        },
        selection_reason=request.selection_reason,
        frozen_at=datetime.now(timezone.utc),
    )
    return freeze_store.save(record, checkpoint=checkpoint) if freeze_store is not None else record
