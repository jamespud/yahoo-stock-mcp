"""Canonical dataset identities and manifest persistence."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ...compiler import digest
from ..contracts import DatasetPartition


def content_hash(partitions: list[DatasetPartition]) -> str:
    return digest([partition.model_dump(mode="json") for partition in partitions])


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    if isinstance(value, str) and value.endswith("+00:00"):
        return value[:-6] + "Z"
    return value


def dataset_identity(manifest_body: dict[str, Any], partitions: list[DatasetPartition]) -> str:
    """Identity excludes capture time, but binds normalized metadata and every file hash."""
    body = {key: value for key, value in manifest_body.items() if key not in {"dataset_id", "content_hash", "captured_at", "partition_manifest"}}
    return digest({"manifest": _canonical(body), "partitions": _canonical([partition.model_dump(mode="json") for partition in partitions])})


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
