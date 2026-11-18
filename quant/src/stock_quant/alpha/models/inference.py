"""Frozen-model inference over an existing feature set."""
from __future__ import annotations

from datetime import datetime

import lightgbm as lgb
import pandas as pd

from ..contracts import DatasetManifest, Hash
from ..data.catalog import DatasetStore
from ..errors import AlphaError, AlphaErrorCode
from .registry import AlphaModelRegistry


def predict_alpha(
    model_set_id: str,
    feature_set_id: str,
    start: datetime,
    end: datetime,
    *,
    store: DatasetStore,
    registry: AlphaModelRegistry,
) -> DatasetManifest:
    if start >= end:
        raise ValueError("Prediction start must be before end")
    # model_set_id and model_id are both content hashes; resolve by scanning metadata.
    loaded = None
    for directory in registry.root.iterdir():
        metadata_file = directory / "metadata.json"
        if metadata_file.is_file():
            import json
            metadata = json.loads(metadata_file.read_text())
            if metadata.get("model_set_id") == model_set_id:
                loaded = registry.load("sha256:" + directory.name)
                break
    if loaded is None:
        raise AlphaError(AlphaErrorCode.MODEL_NOT_FOUND, "Model set was not found", {"model_set_id": model_set_id})
    validation_end = datetime.fromisoformat(loaded.metadata["validation_window"]["validation_end"].replace("Z", "+00:00"))
    if start < validation_end:
        raise AlphaError(AlphaErrorCode.DATASET_MISMATCH, "Prediction period overlaps model training or validation history")
    feature_manifest = store.load_manifest(feature_set_id)
    if feature_manifest.dataset_kind != "feature_panel":
        raise AlphaError(AlphaErrorCode.DATASET_MISMATCH, "Feature set is not a feature_panel")
    rows = store.read_rows(feature_set_id)
    frame = pd.DataFrame(rows)
    if (pd.to_datetime(frame["feature_available_at"], utc=True) > pd.to_datetime(frame["decision_at"], utc=True)).any():
        raise AlphaError(AlphaErrorCode.DATASET_MISMATCH, "Feature contains values available after its decision time")
    pivot = frame.pivot_table(index=["decision_at", "instrument_id"], columns="factor_name", values="factor_value", aggfunc="first").reset_index()
    pivot["decision_at"] = pd.to_datetime(pivot["decision_at"], utc=True)
    period = pivot[(pivot["decision_at"] >= pd.Timestamp(start)) & (pivot["decision_at"] < pd.Timestamp(end))].copy()
    schema = list(loaded.metadata["feature_schema"])
    present = [name for name in pivot.columns if name not in {"decision_at", "instrument_id"}]
    if set(present) != set(schema):
        raise AlphaError(AlphaErrorCode.DATASET_MISMATCH, "Prediction feature schema differs from training schema", {"expected": schema, "actual": sorted(present)})
    if period.empty:
        raise AlphaError(AlphaErrorCode.DATA_UNAVAILABLE, "No features in prediction period")
    booster = lgb.Booster(model_str=loaded.model_text)
    values = booster.predict(period[schema], num_iteration=loaded.metadata["selected_iteration"])
    prediction_rows = []
    for row, value in zip(period.itertuples(index=False), values, strict=True):
        prediction_rows.append({
            "decision_at": row.decision_at.to_pydatetime(), "instrument_id": row.instrument_id,
            "prediction_value": float(value), "model_id": loaded.model_id,
            "model_set_id": model_set_id, "fold": loaded.metadata["model_set_id"],
        })
    return store.publish_prediction_panel(
        prediction_rows, parent_feature_set_id=feature_set_id, model_id=loaded.model_id,
        model_set_id=model_set_id, start_at=start, end_at=end, price_basis=feature_manifest.price_basis,
    )
