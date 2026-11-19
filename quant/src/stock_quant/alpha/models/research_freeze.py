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
from typing import Iterable

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

    def save(self, record: FrozenResearchRecord) -> FrozenResearchRecord:
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
            os.replace(stage, path)
        finally:
            if stage.exists():
                stage.unlink()
        return record


def freeze_identity(record: FrozenResearchRecord) -> str:
    body = record.model_dump(mode="json", exclude={"freeze_id", "frozen_at"})
    return digest(body)


def _model_hash(registry: AlphaModelRegistry, model_id: str) -> str:
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
    return metadata["model_hash"]


def freeze_alpha_selection(
    request: FreezeRequest,
    *,
    store: DatasetStore,
    model_registry: AlphaModelRegistry,
    freeze_store: FrozenResearchStore,
) -> FrozenResearchRecord:
    model_hashes = {model_id: _model_hash(model_registry, model_id) for model_id in request.selected_model_ids}
    test_feature = store.load_manifest(request.test_feature_set_id)
    test_label = store.load_manifest(request.test_label_source_dataset_id)
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
        "label_spec": {"label_name": request.label_name, "target_type": request.target_type, "horizon_days": request.horizon_days},
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
        label_spec={"label_name": request.label_name, "target_type": request.target_type, "horizon_days": request.horizon_days},
        selection_reason=request.selection_reason,
        frozen_at=datetime.now(timezone.utc),
    )
    return freeze_store.save(record)
