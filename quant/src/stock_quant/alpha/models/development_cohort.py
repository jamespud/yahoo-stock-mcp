"""Single shared, matured development cohort for LightGBM and comparison baselines."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

import pandas as pd

from ...compiler import digest
from ..contracts import DatasetManifest, ModelResourceBudget
from ..data.catalog import DatasetStore
from ..errors import AlphaError, AlphaErrorCode
from .labels import LabelRow
from .temporal_split import ModelDevelopmentSplit, split_matured_development


@dataclass(frozen=True)
class DevelopmentCohort:
    """Trusted Train/Validation samples and identities prepared exactly once."""

    feature_set_id: str
    label_source_dataset_id: str
    feature_manifest: DatasetManifest
    label_manifest: DatasetManifest
    split: ModelDevelopmentSplit
    resource_budget: ModelResourceBudget
    feature_schema: tuple[str, ...]
    train: pd.DataFrame
    validation: pd.DataFrame
    label_spec: dict
    label_definition_hash: str
    counts: dict[str, int]
    train_identity_hash: str
    validation_identity_hash: str
    universe_identity_hash: str
    decision_identity_hash: str
    feature_validation_keys: frozenset
    matured_validation_label_keys: frozenset


def _feature_frame(rows: list[dict]) -> tuple[pd.DataFrame, list[str]]:
    frame = pd.DataFrame(rows)
    if frame.empty:
        raise AlphaError(AlphaErrorCode.DATA_UNAVAILABLE, "Feature set has no rows")
    frame = frame.copy()
    frame["decision_at"] = pd.to_datetime(frame["decision_at"], utc=True)
    frame["feature_available_at"] = pd.to_datetime(frame["feature_available_at"], utc=True)
    if (frame["feature_available_at"] > frame["decision_at"]).any():
        raise AlphaError(AlphaErrorCode.DATASET_MISMATCH, "Feature contains values available after its decision time")
    duplicate = frame.duplicated(["decision_at", "instrument_id", "factor_name"], keep=False)
    if duplicate.any():
        raise AlphaError(
            AlphaErrorCode.DATASET_MISMATCH,
            "Feature set contains duplicate sample-factor identities",
            {"duplicate_count": int(duplicate.sum())},
        )
    pivot = frame.pivot_table(
        index=["decision_at", "instrument_id"], columns="factor_name", values="factor_value", aggfunc="first",
    )
    schema = sorted(pivot.columns)
    pivot = pivot[schema]
    pivot.index = pivot.index.set_levels(pd.to_datetime(pivot.index.levels[0], utc=True), level=0)
    return pivot, list(schema)


def _join_labels(features: pd.DataFrame, schema: list[str], labels: list[LabelRow]) -> pd.DataFrame:
    label_frame = pd.DataFrame([row.model_dump(mode="python") for row in labels])
    label_frame["decision_at"] = pd.to_datetime(label_frame["feature_decision_at"], utc=True)
    label_frame["label_available_at"] = pd.to_datetime(label_frame["label_available_at"], utc=True)
    duplicate = label_frame.duplicated(["decision_at", "instrument_id"], keep=False)
    if duplicate.any():
        raise AlphaError(
            AlphaErrorCode.DATASET_MISMATCH,
            "Label source contains duplicate sample identities",
            {"duplicate_count": int(duplicate.sum())},
        )
    joined = features.reset_index().merge(
        label_frame[["decision_at", "instrument_id", "label_value", "label_available_at"]],
        on=["decision_at", "instrument_id"], how="inner", validate="one_to_one",
    )
    return joined[joined["label_value"].notna()].copy()


def _subset(joined: pd.DataFrame, start, end) -> pd.DataFrame:
    return joined[
        (joined["decision_at"] >= pd.Timestamp(start))
        & (joined["decision_at"] < pd.Timestamp(end))
        & (joined["label_available_at"] <= pd.Timestamp(end))
    ].copy()


def _keys(frame: pd.DataFrame) -> frozenset:
    return frozenset(
        (pd.Timestamp(decision).to_pydatetime(), str(instrument))
        for decision, instrument in frame[["decision_at", "instrument_id"]].itertuples(index=False, name=None)
    )


def _label_keys(rows: list[LabelRow]) -> frozenset:
    return frozenset((row.feature_decision_at, row.instrument_id) for row in rows)


def _identity(value) -> str:
    return digest(value)


def _sample_keys_hash(keys: frozenset) -> str:
    return digest([[decision.isoformat(), instrument] for decision, instrument in sorted(keys)])


def prepare_development_cohort(
    request,
    *,
    store: DatasetStore,
    label_rows: list[LabelRow],
    attempt_deadline: AttemptDeadline | None = None,
    checkpoint: Callable[[float], None] | None = None,
) -> DevelopmentCohort:
    """Prepare the sole LightGBM/Ridge/Baseline Train and Validation sample identity."""
    label_rows = [row for row in label_rows if row.label_name == request.label_name]
    if not label_rows:
        raise AlphaError(AlphaErrorCode.DATA_UNAVAILABLE, "No labels match the requested label_name")
    cutoff = request.split.validation_end
    if attempt_deadline:
        attempt_deadline.enter("training_lineage")
    feature_manifest = store.load_manifest(request.feature_set_id)
    label_manifest = store.load_manifest(request.label_source_dataset_id)
    if attempt_deadline:
        attempt_deadline.enter("training_feature_read")
    features, schema = _feature_frame(
        store.read_rows(request.feature_set_id, start_at=request.split.train_start, end_at=cutoff)
    )
    if attempt_deadline:
        attempt_deadline.enter("training_join")
    joined = _join_labels(features, schema, label_rows)
    if len(joined) > request.resource_budget.max_rows:
        raise AlphaError(AlphaErrorCode.RESOURCE_LIMIT_EXCEEDED, "Training rows exceed budget")
    matured = split_matured_development(label_rows, request.split)
    if attempt_deadline:
        attempt_deadline.enter("training_split")

    def allowed(frame: pd.DataFrame, rows: list[LabelRow]) -> pd.DataFrame:
        keys = {(row.feature_decision_at, row.instrument_id) for row in rows}
        return frame[
            frame.apply(lambda item: (item["decision_at"].to_pydatetime(), item["instrument_id"]) in keys, axis=1)
        ]

    train = allowed(_subset(joined, request.split.train_start, request.split.train_end), matured["train"])
    validation = allowed(_subset(joined, request.split.validation_start, request.split.validation_end), matured["validation"])
    if train.empty or validation.empty:
        raise AlphaError(AlphaErrorCode.INSUFFICIENT_HISTORY, "Train and validation require matured labels")
    if attempt_deadline:
        attempt_deadline.enter("training_cohort")

    # Retain the exact feature-side key set without expanding the pivot into a second frame.
    feature_validation_keys = frozenset(
        (pd.Timestamp(decision).to_pydatetime(), str(instrument))
        for decision, instrument in features.index
        if pd.Timestamp(request.split.validation_start) <= pd.Timestamp(decision) < pd.Timestamp(request.split.validation_end)
    )
    matured_validation_label_keys = _label_keys(matured["validation"])
    train_keys = _keys(train)
    validation_keys = _keys(validation)
    validation_window_labels = [
        row for row in label_rows
        if request.split.validation_start <= row.feature_decision_at < request.split.validation_end
    ]
    null_validation_labels = sum(row.label_value is None for row in validation_window_labels)
    immature_validation_labels = sum(
        row.label_value is not None and row.label_available_at > request.split.validation_end - timedelta(days=request.split.embargo_days)
        for row in validation_window_labels
    )
    universe = sorted(set(feature_manifest.instrument_ids) | set(label_manifest.instrument_ids))
    decisions = sorted({decision for decision, _ in train_keys | validation_keys})
    counts = {
        "raw_validation_candidate_count": len(feature_validation_keys),
        "validation_label_window_count": len(validation_window_labels),
        "matured_validation_label_count": len(matured_validation_label_keys),
        "feature_matched_validation_count": len(validation_keys),
        "features_without_matured_label_count": len(feature_validation_keys - matured_validation_label_keys),
        "matured_labels_without_feature_count": len(matured_validation_label_keys - feature_validation_keys),
        "null_validation_label_count": int(null_validation_labels),
        "immature_validation_label_count": int(immature_validation_labels),
        "train_sample_count": len(train_keys),
        "validation_sample_count": len(validation_keys),
        "train_decision_count": len({decision for decision, _ in train_keys}),
        "validation_decision_count": len({decision for decision, _ in validation_keys}),
    }
    label_versions = sorted({row.label_version for row in label_rows})
    label_spec = {
        "label_name": request.label_name,
        "target_type": request.target_type,
        "horizon_days": request.horizon_days,
        "label_source_dataset_id": request.label_source_dataset_id,
        "label_version": label_versions[0] if len(label_versions) == 1 else "mixed",
    }
    label_hash = digest({
        "label_spec": label_spec,
        "split": request.split.model_dump(mode="json"),
        "research_protocol_hash": request.research_protocol_hash,
    })
    if checkpoint:
        checkpoint(0.2)
    return DevelopmentCohort(
        feature_set_id=request.feature_set_id,
        label_source_dataset_id=request.label_source_dataset_id,
        feature_manifest=feature_manifest,
        label_manifest=label_manifest,
        split=request.split,
        resource_budget=request.resource_budget,
        feature_schema=tuple(schema),
        train=train,
        validation=validation,
        label_spec=label_spec,
        label_definition_hash=label_hash,
        counts=counts,
        train_identity_hash=_sample_keys_hash(train_keys),
        validation_identity_hash=_sample_keys_hash(validation_keys),
        universe_identity_hash=_identity(universe),
        decision_identity_hash=_identity([decision.isoformat() for decision in decisions]),
        feature_validation_keys=feature_validation_keys,
        matured_validation_label_keys=matured_validation_label_keys,
    )
