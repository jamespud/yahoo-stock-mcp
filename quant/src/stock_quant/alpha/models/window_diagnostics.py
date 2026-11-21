"""Independent Train-only LightGBM fits for exploratory window importance."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

import lightgbm as lgb
import numpy as np
import pandas as pd
from scipy.stats import spearmanr

from .hyperparameters import validate_hyperparameters
from .labels import LabelRow
from .registry import bytes_hash
from .temporal_split import ModelDevelopmentSplit


DIAGNOSTIC_WINDOW_VERSION = "diagnostic-window-importance-v1"


def _utc(value) -> datetime:
    parsed = pd.Timestamp(value).to_pydatetime()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _feature_frame(feature_rows: list[dict]) -> tuple[pd.DataFrame, list[str]]:
    frame = pd.DataFrame(feature_rows)
    if frame.empty:
        raise ValueError("Feature rows are required for diagnostic folds")
    decisions = pd.to_datetime(frame["decision_at"], utc=True)
    available = pd.to_datetime(frame["feature_available_at"], utc=True)
    if (available > decisions).any():
        raise ValueError("Feature contains values available after its decision time")
    pivot = frame.pivot_table(
        index=["decision_at", "instrument_id"],
        columns="factor_name",
        values="factor_value",
        aggfunc="first",
    )
    schema = sorted(str(value) for value in pivot.columns)
    pivot = pivot[schema]
    pivot.index = pivot.index.set_levels(pd.to_datetime(pivot.index.levels[0], utc=True), level=0)
    return pivot, schema


def _join_labels(features: pd.DataFrame, labels: list[LabelRow]) -> pd.DataFrame:
    label_frame = pd.DataFrame([row.model_dump(mode="python") for row in labels])
    if label_frame.empty:
        return features.reset_index().iloc[0:0].assign(label_value=pd.Series(dtype=float), label_available_at=pd.Series(dtype="datetime64[ns, UTC]"))
    label_frame["decision_at"] = pd.to_datetime(label_frame["feature_decision_at"], utc=True)
    label_frame["label_available_at"] = pd.to_datetime(label_frame["label_available_at"], utc=True)
    joined = features.reset_index().merge(
        label_frame[["decision_at", "instrument_id", "label_value", "label_available_at"]],
        on=["decision_at", "instrument_id"],
        how="inner",
        validate="one_to_one",
    )
    return joined[joined["label_value"].notna()].copy()


def _slice(joined: pd.DataFrame, start: datetime, end: datetime) -> pd.DataFrame:
    return joined[
        (joined["decision_at"] >= pd.Timestamp(start))
        & (joined["decision_at"] < pd.Timestamp(end))
        & (joined["label_available_at"] < pd.Timestamp(end))
    ].copy()


def _params(hyperparameters: dict, random_seed: int) -> tuple[dict, int]:
    return {
        "objective": "regression",
        "metric": "l2",
        "verbosity": -1,
        "seed": random_seed,
        "deterministic": True,
        "force_col_wise": True,
        "num_threads": 1,
        "feature_fraction": 1.0,
        "bagging_fraction": 1.0,
        "bagging_freq": 0,
        "learning_rate": float(hyperparameters.get("learning_rate", 0.05)),
        "num_leaves": int(hyperparameters.get("num_leaves", 31)),
        "max_depth": int(hyperparameters.get("max_depth", -1)),
        "min_child_samples": int(hyperparameters.get("min_child_samples", 20)),
    }, int(hyperparameters.get("n_estimators", 100))


def _normalize(values: dict[str, float]) -> dict[str, float]:
    total = sum(max(0.0, value) for value in values.values())
    if total <= 0:
        return {name: 0.0 for name in values}
    return {name: max(0.0, value) / total for name, value in values.items()}


def _label_evidence(frame: pd.DataFrame) -> dict:
    if frame.empty:
        return {
            "minimum_decision_at": None,
            "maximum_decision_at": None,
            "maximum_label_available_at": None,
            "instrument_count": 0,
            "decision_count": 0,
        }
    return {
        "minimum_decision_at": pd.Timestamp(frame["decision_at"].min()).isoformat(),
        "maximum_decision_at": pd.Timestamp(frame["decision_at"].max()).isoformat(),
        "maximum_label_available_at": pd.Timestamp(frame["label_available_at"].max()).isoformat(),
        "instrument_count": int(frame["instrument_id"].nunique()),
        "decision_count": int(frame["decision_at"].nunique()),
    }


def diagnostic_window_importance(
    feature_rows: list[dict],
    label_rows: list[LabelRow],
    *,
    split: ModelDevelopmentSplit,
    split_at: datetime,
    hyperparameters: dict | None = None,
    random_seed: int = 0,
    checkpoint: Callable[[float], None] | None = None,
) -> dict:
    """Fit two independent deterministic models over disjoint Train slices.

    These fits are exploratory evidence only. They are not persisted, do not
    select iterations, and cannot update the primary model or candidate ledger.
    """
    split_at = _utc(split_at)
    if not split.train_start < split_at < split.train_end:
        raise ValueError("split_at must be strictly inside Train")
    hyperparameters = validate_hyperparameters(hyperparameters or {})
    features, schema = _feature_frame(feature_rows)
    joined = _join_labels(features, label_rows)
    params, iteration_count = _params(hyperparameters, random_seed)
    windows = (
        ("train_first", split.train_start, split_at),
        ("train_second", split_at, split.train_end),
    )
    fits = []
    for index, (fold_id, start, end) in enumerate(windows):
        frame = _slice(joined, start, end)
        base = {
            "fold_id": fold_id,
            "window": {"start": _utc(start).isoformat(), "end": _utc(end).isoformat()},
            "feature_schema": schema,
            "iteration_count": iteration_count,
            "selection_method": "fixed_iteration_no_validation_selection",
            "label_evidence": _label_evidence(frame),
        }
        if frame.empty:
            fits.append({
                **base,
                "status": "NOT_TESTED",
                "reason": "NO_MATURED_LABELS",
                "sample_count": 0,
                "feature_importance_gain": None,
                "normalized_importance": None,
                "model_hash": None,
            })
            continue
        if len(frame) < 2:
            fits.append({
                **base,
                "status": "NOT_TESTED",
                "reason": "INSUFFICIENT_SAMPLES",
                "sample_count": len(frame),
                "feature_importance_gain": None,
                "normalized_importance": None,
                "model_hash": None,
            })
            continue
        dataset = lgb.Dataset(
            frame[schema],
            label=frame["label_value"],
            feature_name=schema,
            free_raw_data=False,
        )
        booster = lgb.train(
            params,
            dataset,
            num_boost_round=iteration_count,
            callbacks=[lgb.log_evaluation(0)],
        )
        model_text = booster.model_to_string(num_iteration=iteration_count)
        gains_array = booster.feature_importance(
            importance_type="gain", iteration=iteration_count,
        )
        gains = {
            name: float(value)
            for name, value in zip(schema, gains_array, strict=True)
        }
        fits.append({
            **base,
            "status": "TESTED",
            "reason": None,
            "sample_count": len(frame),
            "feature_importance_gain": gains,
            "normalized_importance": _normalize(gains),
            "model_hash": bytes_hash(model_text.encode()),
        })
        if checkpoint:
            checkpoint((index + 1) / 2)

    comparable = [
        fit for fit in fits
        if fit["status"] == "TESTED" and fit["feature_schema"] == schema
    ]
    if len(comparable) < 2:
        return {
            "schema_version": DIAGNOSTIC_WINDOW_VERSION,
            "status": "NOT_TESTED",
            "reason": "FEWER_THAN_TWO_COMPARABLE_FITS",
            "folds": fits,
            "importance_deltas": None,
            "importance_rank_correlation": None,
            "stability_claim": None,
            "definition": "independent_train_window_importance_not_model_selection",
        }
    first_scores = [comparable[0]["normalized_importance"][name] for name in schema]
    second_scores = [comparable[1]["normalized_importance"][name] for name in schema]
    correlation = spearmanr(first_scores, second_scores).statistic
    if correlation is None or not np.isfinite(correlation):
        correlation = None
    return {
        "schema_version": DIAGNOSTIC_WINDOW_VERSION,
        "status": "TESTED",
        "reason": None,
        "folds": fits,
        "importance_deltas": {
            name: comparable[1]["normalized_importance"][name] - comparable[0]["normalized_importance"][name]
            for name in schema
        },
        "importance_rank_correlation": float(correlation) if correlation is not None else None,
        "stability_claim": None,
        "definition": "independent_train_window_importance_not_model_selection",
    }
