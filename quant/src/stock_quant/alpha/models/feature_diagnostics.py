"""Read-only frozen-model feature importance and development-window stability."""
from __future__ import annotations

from datetime import datetime, timezone

import lightgbm as lgb
from scipy.stats import spearmanr

from ...compiler import digest
from ..errors import AlphaError, AlphaErrorCode
from .registry import AlphaModelRegistry

STABILITY_THRESHOLD = 0.70


def _utc(value) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _window_bounds(window: dict, *, name: str) -> tuple[datetime, datetime]:
    try:
        return _utc(window["start"]), _utc(window["end"])
    except (KeyError, TypeError, ValueError) as error:
        raise AlphaError(AlphaErrorCode.DATASET_MISMATCH, f"Invalid {name} window", {"window": window}) from error


def _importance(booster: lgb.Booster, names: list[str], importance_type: str, iteration: int) -> dict[str, float]:
    values = booster.feature_importance(importance_type=importance_type, iteration=iteration)
    return {name: float(value) for name, value in zip(names, values, strict=True)}


def _normalize(values: dict[str, float]) -> dict[str, float]:
    total = sum(max(0.0, value) for value in values.values())
    if total <= 0:
        return {name: 0.0 for name in values}
    return {name: max(0.0, value) / total for name, value in values.items()}


def _stability(loaded, primary: dict, registry: AlphaModelRegistry, names: list[str]) -> dict:
    folds = loaded.metadata.get("stability_by_fold") or []
    if not folds:
        return {"stability_status": "NOT_TESTED", "stability_by_fold": [], "stability_spearman": None,
                "stability_reason": "NO_MULTI_WINDOW_EVIDENCE"}
    train_start, _ = _window_bounds(primary["training_window"], name="primary training")
    _, validation_end = _window_bounds(primary["validation_window"], name="primary validation")
    prepared = []
    seen_models = set()
    for fold in folds:
        try:
            fold_id = str(fold["fold_id"])
            fold_model_id = str(fold["model_id"])
            window_start, window_end = _window_bounds(fold["window"], name=f"fold {fold_id}")
        except (KeyError, TypeError) as error:
            return {"stability_status": "INVALID_EVIDENCE", "stability_by_fold": [], "stability_spearman": None,
                    "stability_reason": "MALFORMED_FOLD_EVIDENCE"}
        if not train_start <= window_start < window_end <= validation_end:
            return {"stability_status": "INVALID_EVIDENCE", "stability_by_fold": [], "stability_spearman": None,
                    "stability_reason": "FOLD_OUTSIDE_DEVELOPMENT_WINDOWS"}
        if fold_model_id == loaded.model_id or fold_model_id in seen_models:
            return {"stability_status": "INVALID_EVIDENCE", "stability_by_fold": [], "stability_spearman": None,
                    "stability_reason": "FOLD_MODELS_NOT_DISTINCT"}
        seen_models.add(fold_model_id)
        try:
            fold_loaded = registry.load(fold_model_id)
            fold_booster = lgb.Booster(model_str=fold_loaded.model_text)
            if list(fold_booster.feature_name()) != names:
                return {"stability_status": "INVALID_EVIDENCE", "stability_by_fold": [], "stability_spearman": None,
                        "stability_reason": "FOLD_FEATURE_SCHEMA_MISMATCH"}
            iteration = int(fold_loaded.metadata.get("selected_iteration") or fold_booster.current_iteration())
            gain = _importance(fold_booster, names, "gain", iteration)
        except AlphaError:
            return {"stability_status": "INVALID_EVIDENCE", "stability_by_fold": [], "stability_spearman": None,
                    "stability_reason": "FOLD_MODEL_NOT_FOUND"}
        prepared.append({
            "fold_id": fold_id,
            "model_id": fold_model_id,
            "window": {"start": window_start.isoformat(), "end": window_end.isoformat()},
            "normalized_importance": _normalize(gain),
        })
    if len(prepared) < 2:
        return {"stability_status": "NOT_TESTED", "stability_by_fold": prepared, "stability_spearman": None,
                "stability_reason": "FEWER_THAN_TWO_FOLDS"}
    vectors = [[fold["normalized_importance"][name] for name in names] for fold in prepared]
    correlations = []
    for left in range(len(vectors)):
        for right in range(left + 1, len(vectors)):
            value = spearmanr(vectors[left], vectors[right]).statistic
            if value is not None and value == value:
                correlations.append(float(value))
    status = "PASS" if correlations and min(correlations) >= STABILITY_THRESHOLD else "UNSTABLE"
    return {
        "stability_status": status,
        "stability_by_fold": prepared,
        "stability_spearman": {"minimum_pairwise": min(correlations) if correlations else None, "pairwise": correlations},
        "stability_reason": "MINIMUM_PAIRWISE_SPEARMAN_AT_LEAST_0_70" if status == "PASS" else "IMPORTANCE_RANKING_UNSTABLE",
    }


def diagnose_alpha_model(model_id: str, *, registry: AlphaModelRegistry) -> dict:
    """Read an integrity-checked model and recompute deterministic diagnostics."""
    loaded = registry.load(model_id)
    metadata = loaded.metadata
    booster = lgb.Booster(model_str=loaded.model_text)
    names = list(booster.feature_name())
    schema = list(metadata.get("feature_schema") or [])
    if names != schema:
        raise AlphaError(AlphaErrorCode.DATASET_MISMATCH, "Frozen model feature names differ from its schema",
                         {"model_id": model_id, "features": names, "schema": schema})
    iteration = int(metadata.get("selected_iteration") or booster.current_iteration())
    if iteration < 1 or iteration > booster.current_iteration():
        raise AlphaError(AlphaErrorCode.DATASET_MISMATCH, "Selected model iteration is outside the frozen booster")
    gain = _importance(booster, names, "gain", iteration)
    split = _importance(booster, names, "split", iteration)
    result = {
        "schema_version": "alpha-model-diagnostics-v1",
        "model_id": loaded.model_id,
        "model_hash": metadata["model_hash"],
        "feature_schema_hash": metadata.get("feature_schema_hash") or digest(schema),
        "selected_iteration": iteration,
        "feature_importance_gain": gain,
        "feature_importance_split": split,
        "normalized_importance": _normalize(gain),
        "training_window": metadata.get("training_window"),
        "validation_window": metadata.get("validation_window"),
    }
    result.update(_stability(loaded, metadata, registry, names))
    return result
