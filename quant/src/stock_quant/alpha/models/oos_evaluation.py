"""Independent out-of-sample evaluation for a validation-selected research freeze."""
from __future__ import annotations

from datetime import datetime
from typing import Callable

import numpy as np
from ...compiler import digest
from ..contracts import Hash
from ..data.catalog import DatasetStore
from ..data.partitions import read_partition
from ..errors import AlphaError, AlphaErrorCode
from .evaluation import quantile_returns, summarize_rank_ic_records
from .evaluation_coverage import attribute_evaluation_coverage
from .inference import predict_alpha
from .labels import stock_forward_labels
from .registry import AlphaModelRegistry
from .research_freeze import FrozenResearchRecord


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
    prediction_root = store.datasets / prediction_manifest.dataset_id.removeprefix("sha256:")
    prediction_rows = []
    for partition in prediction_manifest.partition_manifest:
        prediction_rows.extend(read_partition(prediction_root / partition.logical_path))
    label_manifest = store.load_manifest(record.test_dataset_ids["test_label_source_dataset_id"])
    label_rows = stock_forward_labels(
        store.read_rows(record.test_dataset_ids["test_label_source_dataset_id"], start_at=start, end_at=end),
        horizon=int(record.label_spec.get("horizon_days", 5)),
    )
    expected_decisions = sorted({
        row.feature_decision_at for row in label_rows if start <= row.feature_decision_at < end
    } | {
        row["decision_at"] for row in prediction_rows if start <= row["decision_at"] < end
    })
    expected_instruments = sorted({
        *label_manifest.instrument_ids,
        *(str(row["instrument_id"]).upper() for row in prediction_rows if start <= row["decision_at"] < end),
    })
    attribution = attribute_evaluation_coverage(
        prediction_rows,
        label_rows,
        expected_instruments=expected_instruments,
        expected_decisions=expected_decisions,
    )
    coverage = attribution.summary
    valid_rows = attribution.valid_pairs
    target_values = np.array([row["label_value"] for row in valid_rows], dtype=float)
    prediction_values = np.array([row["prediction_value"] for row in valid_rows], dtype=float)
    decisions = [row["decision_at"] for row in valid_rows]
    rank_records = coverage["rank_ic_by_decision"]
    rank_summary = summarize_rank_ic_records(rank_records)
    quantiles = quantile_returns(target_values, prediction_values, decisions)
    summary = {
        "rank_ic_mean": rank_summary["rank_ic_mean"],
        "rank_ic_std": rank_summary["rank_ic_std"],
        "icir": rank_summary["icir"],
        "quantile_returns": quantiles["quantile_returns"],
        "top_bottom_spread": quantiles["top_bottom_spread"],
        "prediction_coverage": coverage["prediction_coverage"],
        "valid_label_coverage": coverage["label_coverage"],
        "matched_prediction_label_coverage": coverage["matched_prediction_label_coverage"],
        "valid_ic_date_coverage": coverage["valid_ic_date_coverage"],
        "valid_ic_date_count": coverage["valid_ic_date_count"],
        "invalid_ic_date_count": coverage["invalid_ic_date_count"],
        "expected_ic_date_count": coverage["expected_date_count"],
        "expected_opportunities": coverage["expected_opportunities"],
        "valid_matched_opportunities": coverage["valid_matched_opportunities"],
        "excluded_opportunities": coverage["excluded_opportunities"],
        "valid_pair_count": coverage["valid_matched_opportunities"],
        "eligible_count": sum(row["eligible_count"] for row in rank_records),
        "excluded_count": coverage["excluded_opportunities"],
        "exclusion_reasons": coverage["excluded_samples_by_reason"],
        "exclusion_date_reasons": coverage["excluded_dates_by_reason"],
        "secondary_exclusion_flags": coverage["secondary_flags_by_reason"],
        "conservation_holds": coverage["conservation_holds"],
        "sample_reason_conservation_holds": coverage["sample_reason_conservation_holds"],
        "date_reason_conservation_holds": coverage["date_reason_conservation_holds"],
        "sample_count": coverage["valid_matched_opportunities"],
        "evaluation_window": record.test_window,
        "definition": coverage["definition"],
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
