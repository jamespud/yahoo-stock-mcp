"""Safe alpha model registry. Models are text artifacts, never Pickle."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from ...compiler import digest
from ..contracts import Hash
from ..errors import AlphaError, AlphaErrorCode


def bytes_hash(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


@dataclass(frozen=True)
class LoadedModel:
    model_id: str
    metadata: dict
    model_text: str


class AlphaModelRegistry:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def model_path(self, model_id: str) -> Path:
        if not isinstance(model_id, str) or not model_id.startswith("sha256:") or "/" in model_id or ".." in model_id:
            raise ValueError("Invalid model ID")
        return self.root / model_id.removeprefix("sha256:")

    def save(self, *, model_text: str, metadata: dict) -> dict:
        content = model_text.encode()
        model_hash = bytes_hash(content)
        complete = {**metadata, "model_hash": model_hash}
        model_id = digest(complete)
        model_set_id = digest({"model_id": model_id, "feature_set_id": complete["feature_set_id"], "label_definition_hash": complete["label_definition_hash"]})
        persisted = {**complete, "model_id": model_id, "model_set_id": model_set_id}
        directory = self.model_path(model_id)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        model_file = directory / "model.txt"
        metadata_file = directory / "metadata.json"
        model_file.write_bytes(content)
        metadata_file.write_text(json.dumps(persisted, sort_keys=True, separators=(",", ":"), allow_nan=False))
        return persisted

    def commit_from(self, staging: "AlphaModelRegistry", model_id: str) -> None:
        source = staging.model_path(model_id)
        destination = self.model_path(model_id)
        if destination.exists():
            self.load(model_id)
            return
        os.rename(source, destination)

    def load(self, model_id: str) -> LoadedModel:
        directory = self.model_path(model_id)
        metadata_file = directory / "metadata.json"
        model_file = directory / "model.txt"
        if not metadata_file.is_file() or not model_file.is_file():
            raise AlphaError(AlphaErrorCode.MODEL_NOT_FOUND, "Model artifact is missing", {"model_id": model_id})
        try:
            metadata = json.loads(metadata_file.read_text())
            content = model_file.read_bytes()
            complete = {key: value for key, value in metadata.items() if key not in {"model_id", "model_set_id"}}
            expected_set = digest({"model_id": model_id, "feature_set_id": complete["feature_set_id"], "label_definition_hash": complete["label_definition_hash"]})
            if metadata.get("model_id") != model_id or metadata.get("model_hash") != bytes_hash(content) or digest(complete) != model_id or metadata.get("model_set_id") != expected_set:
                raise ValueError("Model integrity check failed")
        except Exception as error:
            raise AlphaError(AlphaErrorCode.MODEL_NOT_FOUND, "Model artifact failed integrity validation", {"model_id": model_id, "reason": str(error)}) from error
        return LoadedModel(model_id=model_id, metadata=metadata, model_text=content.decode())
