"""Canonical identities for Campaigns, Protocols, Trials and ledger events."""
from __future__ import annotations

import json
import math
from collections.abc import Iterable
from copy import deepcopy
from datetime import UTC, date, datetime
from typing import Any

from ...compiler import digest


def _assert_finite(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Research identity cannot contain NaN or Infinity")
    if isinstance(value, dict):
        for item in value.values():
            _assert_finite(item)
    elif isinstance(value, list):
        for item in value:
            _assert_finite(item)


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"Unsupported research identity value: {type(value).__name__}")


def _normalize_identity(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        # Canonicalize timezone-aware ISO instants so +08:00 and Z spell the same moment.
        if "T" in value and (value.endswith("Z") or "+" in value[10:] or "-" in value[10:]):
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError:
                return value
            if parsed.tzinfo is not None:
                return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
        return value
    if isinstance(value, dict):
        return {key: _normalize_identity(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_identity(item) for item in value]
    if isinstance(value, tuple):
        return [_normalize_identity(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    normalized = _normalize_identity(value)
    _assert_finite(normalized)
    return json.dumps(
        normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    )


def canonical_digest(value: Any) -> str:
    return digest(json.loads(canonical_json(value)))


def _drop_paths(value: dict[str, Any], paths: Iterable[tuple[str, ...]]) -> dict[str, Any]:
    result = deepcopy(value)
    for path in paths:
        current: Any = result
        for key in path[:-1]:
            if not isinstance(current, dict) or key not in current:
                current = None
                break
            current = current[key]
        if isinstance(current, dict):
            current.pop(path[-1], None)
    return result


_REQUEST_RUNTIME_PATHS = (
    ("idempotency_key",),
    ("research_trial_id",),
    ("max_seconds",),
    ("resource_budget", "max_seconds"),
)


def canonical_request_payload(value: dict[str, Any]) -> dict[str, Any]:
    """Request bytes that must match exactly after caller-independent runtime fields are removed."""
    return _drop_paths(value, _REQUEST_RUNTIME_PATHS)


def trial_definition_payload(value: dict[str, Any]) -> dict[str, Any]:
    """Scientific candidate identity.

    Runtime budgets and caller display labels are excluded. Dataset, factor, label,
    model, seed, window, fold, inference, metric and threshold fields remain.
    """
    payload = canonical_request_payload(value)
    payload.pop("kind", None)
    payload.pop("feature_set_id", None)
    payload.pop("stock_dataset_id", None)
    payload.pop("horizon_days", None)
    payload.pop("random_seed", None)
    payload.pop("split", None)
    payload.pop("development_window", None)
    payload.pop("walk_forward_config", None)
    payload.pop("baseline_comparison", None)
    payload.pop("lightgbm_hyperparameters", None)
    payload.pop("hyperparameters", None)
    payload.pop("min_rank_ic", None)
    payload.pop("label_spec", None)
    payload.pop("label_name", None)
    payload.pop("target_type", None)
    identity = {
        "request_kind": value.get("kind"),
        "feature_set_id": value.get("feature_set_id"),
        "stock_dataset_id": value.get("stock_dataset_id") or (value.get("label_spec") or {}).get("stock_dataset_id"),
        "label": value.get("label_spec") or {
            "label_name": value.get("label_name"),
            "target_type": value.get("target_type"),
            "horizon_days": value.get("horizon_days"),
        },
        "development_windows": value.get("split") or value.get("development_window"),
        "factor_and_candidate_config": value.get("baseline_comparison"),
        "model_config": value.get("hyperparameters") or value.get("lightgbm_hyperparameters"),
        "seed": value.get("random_seed", 0),
        "walk_forward": value.get("walk_forward_config"),
        "primary_metric_and_threshold": {
            "min_rank_ic": value.get("min_rank_ic"),
            "selection_policy": value.get("selection_policy"),
        },
        "inference": (value.get("walk_forward_config") or {}).get("inference") if value.get("kind") == "walk_forward_alpha" else None,
    }
    return identity


def trial_identities(request: dict[str, Any]) -> tuple[dict[str, Any], str, str, str]:
    canonical_request = canonical_request_payload(request)
    request_hash = canonical_digest(canonical_request)
    definition_payload = trial_definition_payload(request)
    definition_hash = canonical_digest(definition_payload)
    label = request.get("label_spec") or {
        "label_name": request.get("label_name"),
        "target_type": request.get("target_type"),
        "horizon_days": request.get("horizon_days"),
    }
    evidence_body = {
        "feature_set_id": request.get("feature_set_id"),
        "stock_dataset_id": request.get("stock_dataset_id") or label.get("stock_dataset_id"),
        "label": label,
        "development_windows": request.get("split") or request.get("development_window"),
        "evaluation": {
            "kind": request.get("kind"),
            "primary_metric": request.get("min_rank_ic"),
            "walk_forward": request.get("walk_forward_config"),
            "baseline_comparison": request.get("baseline_comparison"),
        },
    }
    return canonical_request, request_hash, definition_hash, canonical_digest(evidence_body)


def campaign_definition_payload(definition: dict[str, Any]) -> dict[str, Any]:
    payload = deepcopy(definition)
    payload.pop("campaign_id", None)
    payload.pop("campaign_identity_hash", None)
    payload.pop("created_at", None)
    payload.pop("campaign_status", None)
    return payload


def campaign_identity(definition: dict[str, Any]) -> tuple[str, str]:
    payload = campaign_definition_payload(definition)
    identity_hash = canonical_digest(payload)
    return identity_hash, identity_hash


def protocol_definition_payload(definition: dict[str, Any]) -> dict[str, Any]:
    payload = deepcopy(definition)
    payload.pop("protocol_hash", None)
    hypotheses = payload.get("hypothesis_definitions")
    if isinstance(hypotheses, list):
        payload["hypothesis_definitions"] = sorted(
            hypotheses,
            key=lambda item: item.get("hypothesis_key", "") if isinstance(item, dict) else "",
        )
    for key in ("allowed_job_kinds", "candidate_definitions", "development_windows", "label_specifications", "factor_specifications"):
        value = payload.get(key)
        if isinstance(value, list):
            payload[key] = sorted(value, key=canonical_json)
    return payload


def protocol_hash_for(definition: dict[str, Any]) -> str:
    return canonical_digest(protocol_definition_payload(definition))


def hypothesis_hash_for(protocol_hash: str, definition: dict[str, Any]) -> str:
    return canonical_digest({"protocol_hash": protocol_hash, "hypothesis": definition})


def selection_policy_hash_for(policy: dict[str, Any]) -> str:
    return canonical_digest(policy)


def event_hash_for(payload: dict[str, Any]) -> str:
    body = deepcopy(payload)
    body.pop("event_hash", None)
    for key in ("created_at",):
        value = body.get(key)
        if isinstance(value, datetime):
            body[key] = value.astimezone(UTC).isoformat().replace("+00:00", "Z")
        elif isinstance(value, date):
            body[key] = value.isoformat()
    return canonical_digest(body)


def snapshot_hash_for(payload: dict[str, Any]) -> str:
    body = deepcopy(payload)
    body.pop("candidate_ledger_hash", None)
    return canonical_digest(body)


def selection_hash_for(payload: dict[str, Any]) -> str:
    body = deepcopy(payload)
    body.pop("selection_id", None)
    body.pop("selection_hash", None)
    return canonical_digest(body)


def report_hash_for(payload: dict[str, Any]) -> str:
    body = deepcopy(payload)
    body.pop("report_hash", None)
    return canonical_digest(body)
