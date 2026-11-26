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
    ("budget_contract_version",),
)


def canonical_request_payload(value: dict[str, Any]) -> dict[str, Any]:
    """Request bytes that must match exactly after caller-independent runtime fields are removed."""
    return _drop_paths(value, _REQUEST_RUNTIME_PATHS)


def _factor_spec_hash(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    try:
        from ..factors.ast import FactorSpec
        from ..factors.compiler import compile_factor_spec
        return compile_factor_spec(FactorSpec.model_validate(value)).spec_hash
    except Exception as error:
        raise ValueError("RESEARCH_PROTOCOL_MISMATCH: invalid FactorSpec in managed request") from error


def normalized_research_request(value: dict[str, Any]) -> dict[str, Any]:
    """One Training/Walk-forward shape for Protocol constraints and Selection."""
    request = canonical_request_payload(value)
    kind = request.get("kind")
    if kind == "train_alpha_model":
        label = {
            "label_name": request.get("label_name"),
            "target_type": request.get("target_type"),
            "stock_dataset_id": request.get("stock_dataset_id"),
            "horizon_days": request.get("horizon_days"),
        }
        window = request.get("split")
        factor_spec = (request.get("baseline_comparison") or {}).get("factor_spec")
        candidate = {
            "kind": kind,
            "feature_set_id": request.get("feature_set_id"),
            "stock_dataset_id": request.get("stock_dataset_id"),
            "label": label,
            "factor_spec": factor_spec,
            "factor_spec_hash": _factor_spec_hash(factor_spec),
            "baseline_comparison": request.get("baseline_comparison"),
            "hyperparameters": request.get("hyperparameters") or {},
            "random_seed": request.get("random_seed", 0),
            "seed": request.get("random_seed", 0),
            "model": "lightgbm_v1",
        }
    elif kind == "walk_forward_alpha":
        label = request.get("label_spec") or {}
        window = request.get("development_window")
        factor_spec = (request.get("baseline_comparison") or {}).get("factor_spec")
        candidate = {
            "kind": kind,
            "feature_set_id": request.get("feature_set_id"),
            "stock_dataset_id": request.get("stock_dataset_id") or label.get("stock_dataset_id"),
            "label": label,
            "factor_spec": factor_spec,
            "factor_spec_hash": _factor_spec_hash(factor_spec),
            "baseline_comparison": request.get("baseline_comparison"),
            "hyperparameters": request.get("lightgbm_hyperparameters") or {},
            "random_seed": request.get("random_seed", 0),
            "seed": request.get("random_seed", 0),
            "walk_forward_config": request.get("walk_forward_config"),
        }
    else:
        raise TypeError("Managed Research supports training and walk-forward requests only")
    return {
        "kind": kind,
        "canonical_request": request,
        "feature_set_id": candidate["feature_set_id"],
        "stock_dataset_id": candidate["stock_dataset_id"],
        "label_spec": label,
        "development_window": window,
        "factor_spec": factor_spec,
        "factor_spec_hash": candidate["factor_spec_hash"],
        "candidate": candidate,
        "evaluation": {
            "primary_metric": "rank_ic",
            "minimum_rank_ic": request.get("min_rank_ic"),
            "baseline_comparison_status_policy": (request.get("baseline_comparison") or {}).get("factor_spec") is not None,
            "multiple_testing_scope": "CURRENT_PREREGISTERED_FAMILY_ONLY",
            "minimum_valid_folds": (request.get("walk_forward_config") or {}).get("min_folds") if kind == "walk_forward_alpha" else None,
        },
        "stopping": {
            "max_seconds": request.get("max_seconds"),
            "resource_budget": request.get("resource_budget") or {},
        },
    }


def trial_definition_payload(value: dict[str, Any]) -> dict[str, Any]:
    """Scientific candidate identity shared by Training and Walk-forward registrations."""
    normalized = normalized_research_request(value)
    candidate = normalized["candidate"]
    return {
        "request_kind": normalized["kind"],
        "feature_set_id": normalized["feature_set_id"],
        "stock_dataset_id": normalized["stock_dataset_id"],
        "label": normalized["label_spec"],
        "development_windows": normalized["development_window"],
        "factor_spec_hash": normalized["factor_spec_hash"],
        "candidate": candidate,
        "evaluation": normalized["evaluation"],
    }


def trial_identities(request: dict[str, Any]) -> tuple[dict[str, Any], str, str, str]:
    canonical_request = canonical_request_payload(request)
    request_hash = canonical_digest(canonical_request)
    normalized = normalized_research_request(request)
    definition_hash = canonical_digest(trial_definition_payload(request))
    evidence_body = {
        "feature_set_id": normalized["feature_set_id"],
        "stock_dataset_id": normalized["stock_dataset_id"],
        "label": normalized["label_spec"],
        "development_windows": normalized["development_window"],
        "factor_spec_hash": normalized["factor_spec_hash"],
        "evaluation": normalized["evaluation"],
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
