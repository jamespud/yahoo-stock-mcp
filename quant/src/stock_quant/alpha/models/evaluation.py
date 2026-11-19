"""Research metrics with explicit sample counts and definitions."""
from __future__ import annotations

import math
from collections import defaultdict

import numpy as np
from scipy.stats import spearmanr


def rank_ic(y_true: np.ndarray, y_pred: np.ndarray, decisions: list) -> dict:
    groups = defaultdict(list)
    for target, prediction, decision in zip(y_true, y_pred, decisions, strict=True):
        if np.isfinite(target) and np.isfinite(prediction):
            groups[decision].append((float(target), float(prediction)))
    correlations = []
    for values in groups.values():
        if len(values) >= 2:
            target, prediction = zip(*values)
            if len(set(target)) > 1 and len(set(prediction)) > 1:
                correlation = spearmanr(target, prediction).statistic
                if np.isfinite(correlation):
                    correlations.append(float(correlation))
    mean = float(np.mean(correlations)) if correlations else None
    std = float(np.std(correlations, ddof=1)) if len(correlations) >= 2 else None
    return {
        "rank_ic": correlations,
        "rank_ic_mean": mean,
        "rank_ic_std": std,
        "icir": (mean / std) if mean is not None and std not in (None, 0.0) else None,
        "sample_count": int((np.isfinite(y_true) & np.isfinite(y_pred)).sum()),
        "decision_count": len(correlations),
        "definition": "Spearman correlation per decision time; ICIR=mean/std with sample standard deviation",
    }


def rank_ic_by_decision(y_true: np.ndarray, y_pred: np.ndarray, decisions: list) -> list[dict]:
    groups = defaultdict(list)
    for target, prediction, decision in zip(y_true, y_pred, decisions, strict=True):
        if np.isfinite(target) and np.isfinite(prediction):
            groups[decision].append((float(target), float(prediction)))
    records = []
    for decision, values in sorted(groups.items(), key=lambda item: item[0]):
        reason = None
        correlation = None
        valid_pair_count = 0
        if len(values) < 2:
            reason = "INSUFFICIENT_VALID_PAIRS"
        else:
            targets, predictions = zip(*values)
            if len(set(targets)) <= 1 or len(set(predictions)) <= 1:
                reason = "CONSTANT_ARRAY"
            else:
                result = spearmanr(targets, predictions).statistic
                if np.isfinite(result):
                    correlation = float(result)
                    valid_pair_count = len(values)
                else:
                    reason = "NONFINITE_CORRELATION"
        records.append({
            "decision_at": decision.isoformat() if hasattr(decision, "isoformat") else decision,
            "rank_ic": correlation,
            "eligible_count": len(values),
            "valid_pair_count": valid_pair_count,
            "excluded_count": 0 if correlation is not None else 1,
            "reason": reason,
        })
    return records


def metrics(y_true: np.ndarray, y_pred: np.ndarray, decisions: list) -> dict:
    rank = rank_ic(y_true, y_pred, decisions)
    valid = np.isfinite(y_true) & np.isfinite(y_pred)
    rank["rank_ic_by_decision"] = rank_ic_by_decision(y_true, y_pred, decisions)
    return {
        **rank,
        "prediction_coverage": float(valid.mean()) if len(valid) else 0.0,
        "mae": float(np.mean(np.abs(y_true[valid] - y_pred[valid]))) if valid.any() else None,
        "rmse": float(math.sqrt(np.mean((y_true[valid] - y_pred[valid]) ** 2))) if valid.any() else None,
    }


def quantile_returns(y_true: np.ndarray, y_pred: np.ndarray, decisions: list, quantiles: int = 5) -> dict:
    groups = defaultdict(list)
    for target, prediction, decision in zip(y_true, y_pred, decisions, strict=True):
        if np.isfinite(target) and np.isfinite(prediction):
            groups[decision].append((float(prediction), float(target)))
    buckets = defaultdict(list)
    for values in groups.values():
        ordered = sorted(values)
        for index, (_, target) in enumerate(ordered):
            bucket = min(quantiles - 1, index * quantiles // max(1, len(ordered)))
            buckets[bucket].append(target)
    means = {str(bucket): float(np.mean(values)) for bucket, values in sorted(buckets.items()) if values}
    spread = (means.get(str(quantiles - 1)) - means.get("0")) if len(means) >= 2 else None
    return {"quantile_returns": means, "top_bottom_spread": spread}
