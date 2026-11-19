"""Independent out-of-sample evaluation for a validation-selected research freeze."""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timezone
from typing import Callable

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

from ...compiler import digest
from ..contracts import Hash
from ..data.catalog import DatasetStore
from ..errors import AlphaError, AlphaErrorCode
from .evaluation import metrics, quantile_returns
from .inference import predict_alpha
from .labels import LabelRow, stock_forward_labels
from .registry import AlphaModelRegistry
from .research_freeze import FrozenResearchRecord


def _rank_ic_by_decision(predictions: list[dict], labels: list[LabelRow], start, end) -> tuple[list[dict], dict]:
    label_by_key = {
        (row.feature_decision_at, row.instrument_id): row
        for row in labels
        if row.label_value is not None and start <= row.feature_decision_at < end
    }
    groups = defaultdict(list)
    for row in predictions:
        decision = row["decision_at"]
        if not start <= decision < end:
            continue
        label = label_by_key.get((decision, row["instrument_id"]))
        if label is None:
            continue
        prediction = float(row["prediction_value"])
        target = float(label.label_value)
        if np.isfinite(prediction) and np.isfinite(target):
            groups[decision].append((prediction, target))
    records = []
    exclusions = defaultdict(int)
    for decision, values in sorted(groups.items(), key=lambda item: item[0]):
        eligible_count = len(values)
        reason = None
        rank_ic = None
        valid_pair_count = 0
        if eligible_count < 2:
            reason = "INSUFFICIENT_VALID_PAIRS"
        else:
            prediction_values, target_values = zip(*values)
            if len(set(target_values)) <= 1 or len(set(prediction_values)) <= 1:
                reason = "CONSTANT_ARRAY"
            else:
                correlation = spearmanr(target_values, prediction_values).statistic
                if np.isfinite(correlation):
                    rank_ic = float(correlation)
                    valid_pair_count = eligible_count
                else:
                    reason = "NONFINITE_CORRELATION"
        if reason:
            exclusions[reason] += 1
        records.append({
            "decision_at": decision.astimezone(timezone.utc).isoformat(),
            "rank_ic": rank_ic,
            "eligible_count": eligible_count,
            "valid_pair_count": valid_pair_count,
            "excluded_count": 0 if rank_ic is not None else 1,
            "reason": reason,
        })
    return records, dict(sorted(exclusions.items()))


def evaluate_alpha_oos(
    record: FrozenResearchRecord,
    *,
    store: DatasetStore,
    model_registry: AlphaModelRegistry,
    checkpoint: Callable[[float], None] | None = None,
) -> dict:
    """Evaluate the frozen validation-selected model against a frozen Test window."""
    if record.frozen_at.tzinfo is None:
        raise AlphaError(AlphaErrorCode.MODEL_NOT_FOUND, "Freeze record has no timezone")
    if not record.frozen_model_ids:
        raise AlphaError(AlphaErrorCode.NO_CANDIDATE_SELECTED, "No candidate was selected at freeze time")
    if record.label_spec.get("label_name") != "stock_forward_return":
        raise AlphaError(AlphaErrorCode.DATASET_MISMATCH, "OOS evaluation currently supports stock_forward_return")
    for model_id in record.frozen_model_ids:
        try:
            loaded = model_registry.load(model_id)
        except AlphaError as error:
            raise AlphaError(AlphaErrorCode.MODEL_NOT_FOUND, "Frozen model artifact is unreadable", {"model_id": model_id, "reason": str(error)}) from error
        if loaded.metadata.get("model_hash") != record.model_hashes.get(model_id):
            raise AlphaError(AlphaErrorCode.MODEL_NOT_FOUND, "Frozen model hash changed after freeze", {"model_id": model_id})
    for dataset_id in (record.test_dataset_ids.get("test_feature_set_id"), record.test_dataset_ids.get("test_label_source_dataset_id")):
        if dataset_id is None:
            continue
        store.load_manifest(dataset_id)
    start = datetime.fromisoformat(record.test_window["start"])
    end = datetime.fromisoformat(record.test_window["end"])
    if record.test_dataset_hashes.get("test_feature_set_id") != store.load_manifest(record.test_dataset_ids["test_feature_set_id"]).content_hash:
        raise AlphaError(AlphaErrorCode.DATASET_MISMATCH, "Test feature hash changed after freeze")
    if record.test_dataset_hashes.get("test_label_source_dataset_id") != store.load_manifest(record.test_dataset_ids["test_label_source_dataset_id"]).content_hash:
        raise AlphaError(AlphaErrorCode.DATASET_MISMATCH, "Test label dataset hash changed after freeze")
    model_id = record.frozen_model_ids[0]
    loaded = model_registry.load(model_id)
    prediction_manifest = predict_alpha(
        loaded.metadata["model_set_id"], record.test_dataset_ids["test_feature_set_id"], start, end,
        store=store, registry=model_registry,
    )
    prediction_rows = store.read_rows(prediction_manifest.dataset_id)
    label_rows = stock_forward_labels(
        store.read_rows(record.test_dataset_ids["test_label_source_dataset_id"], start_at=start, end_at=end),
        horizon=int(record.label_spec.get("horizon_days", 5)),
    )
    rank_records, exclusion_reasons = _rank_ic_by_decision(prediction_rows, label_rows, start, end)
    values = [(row["rank_ic"], row) for row in rank_records if row["rank_ic"] is not None]
    correlations = [row["rank_ic"] for _, row in values]
    mean = float(np.mean(correlations)) if correlations else None
    std = float(np.std(correlations, ddof=1)) if len(correlations) >= 2 else None
    icir = mean / std if mean is not None and std not in (None, 0.0) else None
    target_values = np.array([row.label_value for row in label_rows if row.label_value is not None], dtype=float)
    prediction_values = np.array([row["prediction_value"] for row in prediction_rows], dtype=float)
    if len(prediction_values) != len(target_values):
        # Predictions are aligned to feature decisions; labels may include decisions outside the Test window.
        label_lookup = {(row.feature_decision_at, row.instrument_id): row.label_value for row in label_rows}
        aligned = [label_lookup.get((row["decision_at"], row["instrument_id"])) for row in prediction_rows]
        target_values = np.array([value if value is not None else np.nan for value in aligned], dtype=float)
    quantiles = quantile_returns(target_values, prediction_values, [row["decision_at"] for row in prediction_rows])
    summary = {
        "rank_ic_mean": mean,
        "rank_ic_std": std,
        "icir": icir,
        "quantile_returns": quantiles["quantile_returns"],
        "top_bottom_spread": quantiles["top_bottom_spread"],
        "prediction_coverage": float(np.isfinite(prediction_values).mean()) if len(prediction_values) else 0.0,
        "valid_pair_count": sum(row["valid_pair_count"] for row in rank_records),
        "eligible_count": sum(row["eligible_count"] for row in rank_records),
        "excluded_count": sum(row["excluded_count"] for row in rank_records),
        "exclusion_reasons": exclusion_reasons,
        "sample_count": int(np.isfinite(target_values).sum()),
        "evaluation_window": record.test_window,
        "definition": "Spearman correlation per decision time; ICIR=mean/std with sample standard deviation",
    }
    if checkpoint:
        checkpoint(1.0)
    return {
        "freeze_id": record.freeze_id,
        "summary": summary,
        "rank_ic": rank_records,
        "evaluation_window": record.test_window,
        "prediction_dataset_id": prediction_manifest.dataset_id,
        "model_ids": record.frozen_model_ids,
    }
