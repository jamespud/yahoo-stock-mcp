"""PIT stock factor computation over an aligned decision frame."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable

import pandas as pd

from ...compiler import digest

from ..contracts import DatasetManifest
from ..data.catalog import DatasetStore
from ..errors import AlphaError, AlphaErrorCode
from .compiler import CompiledFactorSpec
from .operators import OPERATORS, apply_cross_section, apply_operator, operator_version


def _utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def compute_stock_factors(
    dataset: DatasetManifest,
    compiled: CompiledFactorSpec,
    *,
    store: DatasetStore,
    rows: Iterable[dict] | None = None,
    checkpoint: Callable[[float], None] | None = None,
) -> DatasetManifest:
    if dataset.dataset_kind != "stock_bars":
        raise AlphaError(AlphaErrorCode.DATASET_MISMATCH, "Stock factors require a stock_bars dataset")
    source_rows = list(rows) if rows is not None else store.read_rows(dataset.dataset_id)
    if not source_rows:
        raise AlphaError(AlphaErrorCode.DATA_UNAVAILABLE, "Factor dataset has no rows")
    frame = pd.DataFrame(source_rows)
    required = {"instrument_id", "end_at", "available_at", "open", "high", "low", "close", "volume"}
    if not required.issubset(frame.columns):
        raise AlphaError(AlphaErrorCode.DATASET_MISMATCH, "Stock dataset schema is incomplete")
    frame["decision_at"] = pd.to_datetime(frame["available_at"], utc=True)
    frame["available_at"] = pd.to_datetime(frame["available_at"], utc=True)
    frame = frame.sort_values(["decision_at", "instrument_id"], kind="stable").reset_index(drop=True)
    node_values: dict[str, pd.Series] = {}
    for index, node_id in enumerate(compiled.sorted_nodes):
        if checkpoint:
            checkpoint(0.75 * index / max(1, len(compiled.sorted_nodes)))
        node = compiled.node_map[node_id]
        inputs = [frame[ref].astype(float) if ref in frame.columns else node_values[ref] for ref in node.inputs]
        definition = OPERATORS[node.operator]
        if definition.scope == "cross_section":
            series = apply_cross_section(node.operator, inputs[0], frame["decision_at"], node.params)
        elif definition.scope == "elementwise":
            series = apply_operator(node.operator, inputs, node.params)
        else:
            series = pd.Series(index=frame.index, dtype=float)
            for _, indexes in frame.groupby("instrument_id", sort=True).groups.items():
                transformed = apply_operator(node.operator, [source.iloc[indexes] for source in inputs], node.params)
                series.loc[indexes] = transformed
        node_values[node_id] = series
    feature_rows = []
    for output in compiled.spec.outputs:
        version = operator_version(compiled.node_map[output].operator)
        for position, value in node_values[output].items():
            numeric = None if pd.isna(value) else float(value)
            feature_rows.append({
                "decision_at": frame.at[position, "decision_at"].to_pydatetime(),
                "instrument_id": frame.at[position, "instrument_id"],
                "factor_name": output,
                "factor_value": numeric,
                "feature_available_at": frame.at[position, "available_at"].to_pydatetime(),
                "eligible": numeric is not None,
                "quality_flags": [] if numeric is not None else ["MISSING_VALUE"],
                "factor_version": version,
            })
    feature_rows.sort(key=lambda row: (row["decision_at"], row["instrument_id"], row["factor_name"]))
    if checkpoint:
        checkpoint(0.8)
    manifest = store.publish_feature_panel(
        feature_rows,
        parent_dataset_id=dataset.dataset_id,
        factor_spec_hash=compiled.spec_hash,
        instrument_ids=sorted({row["instrument_id"] for row in feature_rows}),
        start_at=min(row["decision_at"] for row in feature_rows),
        end_at=max(row["decision_at"] for row in feature_rows) + timedelta(microseconds=1),
        calendar_version=dataset.calendar_version,
        price_basis=dataset.price_basis,
        checkpoint=checkpoint,
    )
    return manifest


def combine_feature_panels(
    primary: DatasetManifest,
    auxiliary: list[DatasetManifest],
    *,
    store: DatasetStore,
) -> DatasetManifest:
    """PIT as-of join for stock + option features consumed by one model feature set."""
    if primary.dataset_kind != "feature_panel" or any(item.dataset_kind != "feature_panel" for item in auxiliary):
        raise AlphaError(AlphaErrorCode.DATASET_MISMATCH, "Feature combination requires feature_panel inputs")
    def grouped(manifest):
        result = {}
        for row in store.read_rows(manifest.dataset_id):
            key = (_utc(row["decision_at"]), row["instrument_id"])
            item = result.setdefault(key, {"values": {}, "available_at": _utc(row["feature_available_at"]), "quality": []})
            item["values"][row["factor_name"]] = row["factor_value"]
            item["available_at"] = max(item["available_at"], _utc(row["feature_available_at"]))
            item["quality"].extend(row["quality_flags"])
        return result
    left = grouped(primary)
    rights = [(manifest.dataset_id, grouped(manifest)) for manifest in auxiliary]
    rows = []
    prefixes = {manifest.dataset_id: f"aux{i}" for i, manifest in enumerate(auxiliary, start=1)}
    for (decision, instrument), value in sorted(left.items()):
        combined = dict(value["values"])
        available_at = value["available_at"]
        quality = list(value["quality"])
        for dataset_id, right in rights:
            candidates = [
                (right_decision, right_value)
                for (right_decision, right_instrument), right_value in right.items()
                if right_instrument == instrument and right_decision <= decision and right_value["available_at"] <= decision
            ]
            if not candidates:
                continue
            _, latest = max(candidates, key=lambda item: item[0])
            for name, factor_value in latest["values"].items():
                output_name = name if name not in combined else f"{prefixes[dataset_id]}_{name}"
                combined[output_name] = factor_value
            available_at = max(available_at, latest["available_at"])
            quality.extend(latest["quality"])
        for name, factor_value in sorted(combined.items()):
            rows.append({
                "decision_at": decision, "instrument_id": instrument, "factor_name": name,
                "factor_value": factor_value, "feature_available_at": available_at,
                "eligible": factor_value is not None, "quality_flags": quality if factor_value is None else [],
                "factor_version": "joined-feature-v1",
            })
    spec_hash = digest({"combined": [primary.dataset_id, *[item.dataset_id for item in auxiliary]]})
    return store.publish_feature_panel(
        rows, parent_dataset_id=primary.dataset_id, parent_dataset_ids=[primary.dataset_id, *[item.dataset_id for item in auxiliary]],
        factor_spec_hash=spec_hash, instrument_ids=sorted({row["instrument_id"] for row in rows}),
        start_at=primary.start_at, end_at=primary.end_at, calendar_version=primary.calendar_version,
        price_basis=primary.price_basis,
    )
