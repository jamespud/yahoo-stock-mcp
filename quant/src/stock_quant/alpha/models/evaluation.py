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


def rank_ic_by_decision(
    y_true: np.ndarray,
    y_pred: np.ndarray,
    decisions: list,
    *,
    expected_decisions: list | None = None,
) -> list[dict]:
    observation_groups = defaultdict(list)
    pair_groups = defaultdict(list)
    for target, prediction, decision in zip(y_true, y_pred, decisions, strict=True):
        observation_groups[decision].append((target, prediction))
        if np.isfinite(target) and np.isfinite(prediction):
            pair_groups[decision].append((float(target), float(prediction)))
    expected = sorted(set(expected_decisions if expected_decisions is not None else decisions))
    records = []
    for decision in expected:
        observations = observation_groups.get(decision, [])
        values = pair_groups.get(decision, [])
        reason = None
        reasons: list[str] = []
        correlation = None
        valid_pair_count = 0
        if not observations:
            reason = "NO_OBSERVATIONS"
            reasons = ["NO_OBSERVATIONS"]
        elif len(values) < 2:
            reason = "INSUFFICIENT_VALID_PAIRS"
            reasons = ["INSUFFICIENT_CROSS_SECTION"]
        else:
            targets, predictions = zip(*values)
            constant_label = len(set(targets)) <= 1
            constant_prediction = len(set(predictions)) <= 1
            if constant_label:
                reasons.append("CONSTANT_LABEL")
            if constant_prediction:
                reasons.append("CONSTANT_PREDICTION")
            if reasons:
                reason = "CONSTANT_ARRAY"
            else:
                result = spearmanr(targets, predictions).statistic
                if np.isfinite(result):
                    correlation = float(result)
                    valid_pair_count = len(values)
                else:
                    reason = "NONFINITE_CORRELATION"
                    reasons = ["NONFINITE_CORRELATION"]
        records.append({
            "decision_at": decision.isoformat() if hasattr(decision, "isoformat") else decision,
            "rank_ic": correlation,
            "observation_count": len(observations),
            "eligible_count": len(values),
            "valid_pair_count": valid_pair_count,
            "excluded_count": 0 if correlation is not None else 1,
            "reason": reason,
            "exclusion_reasons": reasons,
        })
    return records


def summarize_rank_ic_records(records: list[dict]) -> dict:
    correlations = [row["rank_ic"] for row in records if row["rank_ic"] is not None]
    mean = float(np.mean(correlations)) if correlations else None
    std = float(np.std(correlations, ddof=1)) if len(correlations) >= 2 else None
    valid_count = len(correlations)
    return {
        "rank_ic": correlations,
        "rank_ic_mean": mean,
        "rank_ic_std": std,
        "icir": (mean / std) if mean is not None and std not in (None, 0.0) else None,
        "valid_ic_date_count": valid_count,
        "invalid_ic_date_count": len(records) - valid_count,
    }


def metrics(
    y_true: np.ndarray,
    y_pred: np.ndarray,
    decisions: list,
    *,
    expected_decisions: list | None = None,
) -> dict:
    targets = np.asarray(y_true, dtype=float)
    predictions = np.asarray(y_pred, dtype=float)
    if targets.size != predictions.size:
        raise ValueError("y_true and y_pred must have equal length")
    expected = sorted(set(expected_decisions if expected_decisions is not None else decisions))
    records = rank_ic_by_decision(targets, predictions, decisions, expected_decisions=expected)
    summary = summarize_rank_ic_records(records)
    finite_targets = np.isfinite(targets)
    finite_predictions = np.isfinite(predictions)
    matched = finite_targets & finite_predictions
    valid_ic_count = summary["valid_ic_date_count"]
    invalid_ic_count = summary["invalid_ic_date_count"]
    return {
        **summary,
        "sample_count": int(matched.sum()),
        "decision_count": valid_ic_count,
        "prediction_coverage": float(finite_predictions.mean()) if len(predictions) else 0.0,
        "valid_label_coverage": float(finite_targets.mean()) if len(targets) else 0.0,
        "matched_prediction_label_coverage": float(matched.mean()) if len(matched) else 0.0,
        "valid_ic_date_count": valid_ic_count,
        "invalid_ic_date_count": invalid_ic_count,
        "expected_ic_date_count": len(records),
        "valid_ic_date_coverage": (valid_ic_count / len(records)) if records else 0.0,
        "rank_ic_by_decision": records,
        "mae": float(np.mean(np.abs(targets[matched] - predictions[matched]))) if matched.any() else None,
        "rmse": float(math.sqrt(np.mean((targets[matched] - predictions[matched]) ** 2))) if matched.any() else None,
        "definition": "Spearman correlation per decision time; ICIR=mean/std with sample standard deviation; null IC days remain in coverage denominators",
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
    top = means.get(str(quantiles - 1))
    bottom = means.get("0")
    spread = top - bottom if top is not None and bottom is not None else None
    return {"quantile_returns": means, "top_bottom_spread": spread}
