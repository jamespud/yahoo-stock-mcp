"""Complete sample- and date-level OOS exclusion attribution."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
from scipy.stats import spearmanr

DATE_REASON_PRIORITY = (
    "OUTSIDE_ELIGIBLE_UNIVERSE",
    "NO_PREDICTION",
    "NO_LABEL",
    "LABEL_NOT_MATURE",
    "INVALID_LABEL",
    "NONFINITE_PREDICTION",
    "NONFINITE_LABEL",
    "INSUFFICIENT_CROSS_SECTION",
    "CONSTANT_PREDICTION",
    "CONSTANT_LABEL",
)
MATURITY_REASONS = {"INSUFFICIENT_FUTURE_HISTORY", "LABEL_NOT_MATURE", "NO_ENTRY_SESSION"}


def _utc(value) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class CoverageAttribution:
    summary: dict
    valid_pairs: list[dict]


def attribute_evaluation_coverage(
    predictions: list[dict],
    labels: list,
    *,
    expected_instruments: list[str],
    expected_decisions: list,
    eligible_universe: list[str] | None = None,
) -> CoverageAttribution:
    instruments = sorted(set(str(value).upper() for value in expected_instruments))
    decisions = sorted({_utc(value) for value in expected_decisions})
    eligible = set(instruments) if eligible_universe is None else {str(value).upper() for value in eligible_universe}
    prediction_map = {}
    for row in predictions:
        key = (_utc(row["decision_at"]), str(row["instrument_id"]).upper())
        if key in prediction_map:
            raise ValueError(f"Duplicate prediction opportunity: {key}")
        prediction_map[key] = row
    label_map = {}
    for row in labels:
        key = (_utc(row.feature_decision_at), str(row.instrument_id).upper())
        if key in label_map:
            raise ValueError(f"Duplicate label opportunity: {key}")
        label_map[key] = row

    excluded: dict[tuple[datetime, str], tuple[str, list[str]]] = {}
    candidates: dict[datetime, list[dict]] = defaultdict(list)
    finite_matched_count = 0
    date_observations: dict[datetime, dict[str, int]] = defaultdict(lambda: {"prediction": 0, "label": 0})

    def exclude(decision, instrument, reason, secondary=None):
        excluded[(decision, instrument)] = (reason, list(secondary or []))

    for decision in decisions:
        for instrument in instruments:
            key = (decision, instrument)
            prediction = prediction_map.get(key)
            label = label_map.get(key)
            if prediction is not None:
                date_observations[decision]["prediction"] += 1
            if label is not None:
                date_observations[decision]["label"] += 1
            if instrument not in eligible:
                exclude(decision, instrument, "OUTSIDE_ELIGIBLE_UNIVERSE")
                continue
            if prediction is None:
                exclude(decision, instrument, "NO_PREDICTION", ["NO_LABEL"] if label is None else [])
                continue
            if label is None:
                exclude(decision, instrument, "NO_LABEL")
                continue
            prediction_value = float(prediction.get("prediction_value"))
            if not np.isfinite(prediction_value):
                exclude(decision, instrument, "NONFINITE_PREDICTION")
                continue
            if label.label_value is None:
                reasons = set(label.label_quality or [])
                reason = "LABEL_NOT_MATURE" if reasons & MATURITY_REASONS else "INVALID_LABEL"
                exclude(decision, instrument, reason)
                continue
            label_value = float(label.label_value)
            if not np.isfinite(label_value):
                exclude(decision, instrument, "NONFINITE_LABEL")
                continue
            finite_matched_count += 1
            candidates[decision].append({
                "decision_at": decision,
                "instrument_id": instrument,
                "prediction_value": prediction_value,
                "label_value": label_value,
            })

    valid_pairs = []
    ic_records = []
    for decision in decisions:
        values = candidates.get(decision, [])
        rank_ic = None
        exclusion_reasons = []
        if len(values) < 2:
            reason = "INSUFFICIENT_CROSS_SECTION"
            for row in values:
                exclude(decision, row["instrument_id"], reason)
            exclusion_reasons = [reason] if values else []
        else:
            targets = [row["label_value"] for row in values]
            predictions_ = [row["prediction_value"] for row in values]
            constant_label = len(set(targets)) <= 1
            constant_prediction = len(set(predictions_)) <= 1
            if constant_prediction:
                for row in values:
                    exclude(decision, row["instrument_id"], "CONSTANT_PREDICTION", ["CONSTANT_LABEL"] if constant_label else [])
                exclusion_reasons = ["CONSTANT_PREDICTION"] + (["CONSTANT_LABEL"] if constant_label else [])
            elif constant_label:
                for row in values:
                    exclude(decision, row["instrument_id"], "CONSTANT_LABEL")
                exclusion_reasons = ["CONSTANT_LABEL"]
            else:
                result = spearmanr(targets, predictions_).statistic
                if np.isfinite(result):
                    rank_ic = float(result)
                    valid_pairs.extend(values)
                else:
                    for row in values:
                        exclude(decision, row["instrument_id"], "INVALID_LABEL", ["NONFINITE_CORRELATION"])
                    exclusion_reasons = ["INVALID_LABEL"]
        ic_records.append({
            "decision_at": decision.isoformat(),
            "rank_ic": rank_ic,
            "valid_pair_count": len(values) if rank_ic is not None else 0,
            "eligible_count": len(values),
            "excluded_count": 0 if rank_ic is not None else len(values),
            "exclusion_reasons": exclusion_reasons,
        })

    excluded_samples = Counter(reason for reason, _ in excluded.values())
    secondary_flags: dict[str, Counter] = defaultdict(Counter)
    for reason, secondary in excluded.values():
        for flag in secondary:
            secondary_flags[reason][flag] += 1
    excluded_dates = Counter()
    date_records = []
    for decision, rank_record in zip(decisions, ic_records, strict=True):
        sample_reasons = Counter(
            reason for (at, _), (reason, _) in excluded.items() if at == decision
        )
        if rank_record["rank_ic"] is None:
            primary = next((reason for reason in DATE_REASON_PRIORITY if sample_reasons.get(reason)), "INSUFFICIENT_CROSS_SECTION")
            excluded_dates[primary] += 1
        date_records.append({
            **rank_record,
            "expected_count": len(instruments),
            "valid_count": rank_record["valid_pair_count"],
            "excluded_count": sum(sample_reasons.values()),
            "exclusion_reasons": dict(sorted(sample_reasons.items())),
        })

    expected_opportunities = len(instruments) * len(decisions)
    valid_count = len(valid_pairs)
    excluded_count = expected_opportunities - valid_count
    valid_ic_dates = sum(record["rank_ic"] is not None for record in ic_records)
    summary = {
        "expected_opportunities": expected_opportunities,
        "valid_matched_opportunities": valid_count,
        "excluded_opportunities": excluded_count,
        "expected_date_count": len(decisions),
        "valid_ic_date_count": valid_ic_dates,
        "invalid_ic_date_count": len(decisions) - valid_ic_dates,
        "excluded_samples_by_reason": dict(sorted(excluded_samples.items())),
        "excluded_dates_by_reason": dict(sorted(excluded_dates.items())),
        "secondary_flags_by_reason": {
            reason: dict(sorted(flags.items())) for reason, flags in sorted(secondary_flags.items())
        },
        "prediction_coverage": (
            sum(date_observations[decision]["prediction"] for decision in decisions) / expected_opportunities
            if expected_opportunities else 0.0
        ),
        "label_coverage": (
            sum(date_observations[decision]["label"] for decision in decisions) / expected_opportunities
            if expected_opportunities else 0.0
        ),
        "matched_prediction_label_coverage": (
            finite_matched_count / expected_opportunities if expected_opportunities else 0.0
        ),
        "valid_ic_date_coverage": (valid_ic_dates / len(decisions)) if decisions else 0.0,
        "conservation_holds": expected_opportunities == valid_count + excluded_count,
        "sample_reason_conservation_holds": sum(excluded_samples.values()) == excluded_count,
        "date_reason_conservation_holds": sum(excluded_dates.values()) == len(decisions) - valid_ic_dates,
        "rank_ic_by_decision": ic_records,
        "date_records": date_records,
        "definition": "Expected universe x decision-date opportunities; each excluded opportunity has one primary reason and optional secondary flags.",
    }
    return CoverageAttribution(summary=summary, valid_pairs=valid_pairs)
