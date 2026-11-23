"""Deterministic research-quality assessment separate from artifact integrity and trading validity."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Literal

import pyarrow.parquet as pq
from pydantic import Field, model_validator

from ...compiler import digest
from ...contracts import StrictModel
from ..contracts import DatasetKind, DatasetManifest, Hash, PriceBasis
from ..errors import AlphaError, AlphaErrorCode
from .catalog import DatasetStore


REPORT_SCHEMA_VERSION = "alpha-research-quality-v2"
POLICY_VERSION = "research-quality-rules-v2.1"

DimensionName = Literal[
    "artifact_integrity",
    "price_semantics",
    "price_attestation",
    "corporate_action_coverage",
    "temporal_availability",
    "universe_validity",
    "lineage_integrity",
    "feature_integrity",
]
DimensionStatus = Literal["VERIFIED", "UNVERIFIED", "BLOCKED", "NOT_APPLICABLE"]
ResearchReadiness = Literal["RESEARCH_EXPLORATORY", "RESEARCH_DATA_VALIDATED", "RESEARCH_BLOCKED"]
FindingSeverity = Literal["INFO", "WARNING", "BLOCKING"]

MAX_FINDING_SCOPE_ITEMS = 64
DIMENSIONS: tuple[DimensionName, ...] = (
    "artifact_integrity",
    "price_semantics",
    "price_attestation",
    "corporate_action_coverage",
    "temporal_availability",
    "universe_validity",
    "lineage_integrity",
    "feature_integrity",
)
_DIMENSION_RANK = {name: index for index, name in enumerate(DIMENSIONS)}


class ResearchQualityLimits(StrictModel):
    max_scan_rows: int = Field(default=1_000_000, ge=1, strict=True)
    max_parent_depth: int = Field(default=16, ge=1, le=128, strict=True)
    max_partitions: int = Field(default=4096, ge=1, strict=True)
    max_nodes: int = Field(default=512, ge=1, le=4096, strict=True)
    scan_batch_size: int = Field(default=65_536, ge=1, le=1_000_000, strict=True)
    max_findings: int = Field(default=256, ge=1, le=4096, strict=True)


class ResearchQualityFinding(StrictModel):
    code: str = Field(min_length=1, max_length=128)
    severity: FindingSeverity
    dimension: DimensionName
    scope: list[str] = Field(default_factory=list, max_length=1000)
    evidence: dict[str, Any] = Field(default_factory=dict)
    verified_facts: list[str] = Field(default_factory=list, max_length=32)
    unverified_facts: list[str] = Field(default_factory=list, max_length=32)


class ResearchDimension(StrictModel):
    status: DimensionStatus
    summary: str = Field(min_length=1, max_length=1024)
    finding_codes: list[str] = Field(default_factory=list, max_length=256)


class ResearchQualityReport(StrictModel):
    schema_version: Literal["alpha-research-quality-v2"]
    dataset_id: Hash
    dataset_kind: DatasetKind
    policy_version: str = Field(min_length=1, max_length=128)
    research_readiness: ResearchReadiness
    dimensions: dict[DimensionName, ResearchDimension]
    findings: list[ResearchQualityFinding]
    parent_dataset_ids: list[Hash]
    report_hash: Hash

    @model_validator(mode="after")
    def coherent(self):
        if set(self.dimensions) != set(DIMENSIONS):
            raise ValueError("Research quality report must contain every dimension exactly once")
        payload = self.model_dump(mode="json", exclude={"report_hash"})
        if digest(payload) != self.report_hash:
            raise ValueError("Research quality report hash mismatch")
        return self


@dataclass(frozen=True)
class TrustedResearchEvidence:
    """Evidence supplied by an internal trusted resolver, never by MCP request data."""

    dataset_id: str
    price_basis: PriceBasis
    price_symbols: tuple[str, ...]
    price_start_at: datetime
    price_end_at: datetime
    price_evidence_ids: tuple[str, ...]
    price_independent_source: str
    corporate_action_evidence_ids: tuple[str, ...]
    temporal_policy: str
    temporal_symbols: tuple[str, ...]
    temporal_start_at: datetime
    temporal_end_at: datetime
    temporal_evidence_ids: tuple[str, ...]
    universe_symbols: tuple[str, ...]
    universe_start_at: datetime
    universe_end_at: datetime
    universe_evidence_ids: tuple[str, ...]
    membership_verified: bool = True
    listing_status_verified: bool = True
    delisting_coverage_verified: bool = True
    symbol_identity_verified: bool = True


ResearchEvidenceResolver = Callable[[DatasetManifest], TrustedResearchEvidence | None]


@dataclass
class _Scan:
    rows_scanned: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    examples: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    adjustments: set[str] = field(default_factory=set)

    def record(self, code: str, example: dict[str, Any] | None = None) -> None:
        self.counts[code] = self.counts.get(code, 0) + 1
        if example is not None:
            examples = self.examples.setdefault(code, [])
            if len(examples) < 5:
                examples.append(example)


@dataclass
class _AssessmentBudget:
    limits: ResearchQualityLimits
    visited_nodes: set[str] = field(default_factory=set)
    scanned_rows: int = 0

    def visit(self, manifest: DatasetManifest) -> None:
        if manifest.dataset_id not in self.visited_nodes and len(self.visited_nodes) >= self.limits.max_nodes:
            raise AlphaError(
                AlphaErrorCode.RESOURCE_LIMIT_EXCEEDED,
                "Research quality assessment exceeded max_nodes",
                {
                    "dataset_id": manifest.dataset_id,
                    "max_nodes": self.limits.max_nodes,
                    "visited_nodes": sorted(self.visited_nodes),
                },
            )
        declared_rows = sum(part.row_count for part in manifest.partition_manifest)
        if self.scanned_rows + declared_rows > self.limits.max_scan_rows:
            raise AlphaError(
                AlphaErrorCode.RESOURCE_LIMIT_EXCEEDED,
                "Research quality assessment exceeded the shared max_scan_rows budget",
                {
                    "dataset_id": manifest.dataset_id,
                    "max_scan_rows": self.limits.max_scan_rows,
                    "already_scanned_rows": self.scanned_rows,
                    "dataset_row_count": declared_rows,
                },
            )
        self.visited_nodes.add(manifest.dataset_id)
        self.scanned_rows += declared_rows


@dataclass(frozen=True)
class _LocalAssessment:
    manifest: DatasetManifest
    statuses: dict[DimensionName, DimensionStatus]
    findings: list[ResearchQualityFinding]


_STOCK_ARTIFACT_CODES = {
    "DUPLICATE_STOCK_BAR",
    "INVALID_STOCK_OHLC",
    "INVALID_STOCK_VOLUME",
    "STOCK_ROW_ORDER_INVALID",
    "STOCK_ROW_OVERLAP",
    "ABNORMAL_PRICE_JUMP",
}
_FEATURE_SCAN_CODES = {
    "DUPLICATE_FEATURE_ROW",
    "INVALID_FEATURE_TIME",
    "FEATURE_AVAILABLE_AFTER_DECISION",
    "INVALID_FEATURE_VALUE",
}
_PREDICTION_ARTIFACT_CODES = {
    "DUPLICATE_PREDICTION_ROW",
    "INVALID_PREDICTION_TIME",
    "INVALID_PREDICTION_VALUE",
}
_NON_BLOCKING_SCAN_CODES = {"ABNORMAL_PRICE_JUMP"}


class _DependencyCycle(RuntimeError):
    def __init__(self, path: list[str]):
        self.path = path
        super().__init__("/".join(path))


class _DependencyDepth(RuntimeError):
    pass


def _status_summary(status: DimensionStatus) -> str:
    return {
        "VERIFIED": "Verified by deterministic evidence for the applicable scope.",
        "UNVERIFIED": "No contradiction is known, but required evidence is absent or incomplete.",
        "BLOCKED": "A concrete integrity, semantic or temporal contradiction blocks this dimension.",
        "NOT_APPLICABLE": "This dimension does not apply to the dataset kind without inherited constraints.",
    }[status]


def _json_safe(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return [_json_safe(item) for item in sorted(value, key=str)]
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _finding(
    code: str,
    severity: FindingSeverity,
    dimension: DimensionName,
    *,
    scope: list[str] | tuple[str, ...] = (),
    evidence: dict[str, Any] | None = None,
    verified_facts: list[str] | tuple[str, ...] = (),
    unverified_facts: list[str] | tuple[str, ...] = (),
) -> ResearchQualityFinding:
    return ResearchQualityFinding(
        code=code,
        severity=severity,
        dimension=dimension,
        scope=list(scope),
        evidence=_json_safe(evidence or {}),
        verified_facts=list(verified_facts),
        unverified_facts=list(unverified_facts),
    )


def _utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return None
        return value.astimezone(timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _nonnegative_count(value: Any) -> tuple[int, bool]:
    if value is None:
        return 0, True
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0, False
    return value, True


def _sort_findings(findings: list[ResearchQualityFinding]) -> list[ResearchQualityFinding]:
    return sorted(
        findings,
        key=lambda item: (
            _DIMENSION_RANK[item.dimension],
            item.code,
            tuple(item.scope),
            digest(item.evidence),
            item.severity,
        ),
    )


def _bounded_findings(findings: list[ResearchQualityFinding], maximum: int) -> list[ResearchQualityFinding]:
    ordered = []
    for finding in _sort_findings(findings):
        if len(finding.scope) > MAX_FINDING_SCOPE_ITEMS:
            finding = finding.model_copy(update={
                "scope": finding.scope[:MAX_FINDING_SCOPE_ITEMS],
                "evidence": {
                    **finding.evidence,
                    "affected_scope_count": len(finding.scope),
                    "scope_truncated": True,
                },
            })
        ordered.append(finding)
    if len(ordered) <= maximum:
        return ordered
    retained = ordered[: maximum - 1]
    retained.append(_finding(
        "REPORT_FINDINGS_TRUNCATED",
        "INFO",
        "lineage_integrity",
        evidence={"omitted_finding_count": len(ordered) - len(retained)},
        verified_facts=["Report truncation is deterministic and does not change the computed dimension statuses."],
    ))
    return _sort_findings(retained)


def _evidence_scope_errors(
    manifest: DatasetManifest,
    *,
    symbols: tuple[str, ...],
    start_at: datetime,
    end_at: datetime,
    evidence_ids: tuple[str, ...],
) -> list[str]:
    errors = []
    if not evidence_ids or any(not isinstance(item, str) or not item.strip() for item in evidence_ids):
        errors.append("evidence_ids_empty")
    if set(manifest.instrument_ids) - {str(item).upper() for item in symbols}:
        errors.append("symbol_scope_incomplete")
    if _utc(start_at) is None or _utc(end_at) is None:
        errors.append("window_invalid")
    else:
        if _utc(start_at) > manifest.start_at.astimezone(timezone.utc):
            errors.append("window_start_after_dataset_start")
        if _utc(end_at) < manifest.end_at.astimezone(timezone.utc):
            errors.append("window_end_before_dataset_end")
    return errors


def _trusted_evidence(manifest: DatasetManifest, resolver: ResearchEvidenceResolver | None) -> TrustedResearchEvidence | None:
    if resolver is None:
        return None
    evidence = resolver(manifest)
    if evidence is not None and evidence.dataset_id != manifest.dataset_id:
        return TrustedResearchEvidence(
            **{**evidence.__dict__, "dataset_id": "__evidence_dataset_mismatch__"}
        )
    return evidence


def _price_findings(
    manifest: DatasetManifest,
    scan: _Scan | None,
    evidence: TrustedResearchEvidence | None,
) -> tuple[dict[DimensionName, DimensionStatus], list[ResearchQualityFinding]]:
    if manifest.dataset_kind != "stock_bars":
        return {
            "price_semantics": "NOT_APPLICABLE",
            "price_attestation": "NOT_APPLICABLE",
            "corporate_action_coverage": "NOT_APPLICABLE",
        }, []

    findings: list[ResearchQualityFinding] = []
    semantics_status: DimensionStatus = "UNVERIFIED"
    attestation_status: DimensionStatus = "UNVERIFIED"
    corporate_status: DimensionStatus = "UNVERIFIED"
    adjustments = sorted(scan.adjustments) if scan is not None else []
    declared = manifest.price_basis
    scope = sorted(manifest.instrument_ids)

    if len(adjustments) > 1:
        semantics_status = "BLOCKED"
        attestation_status = "BLOCKED"
        findings.append(_finding(
            "PRICE_ROW_BASIS_INCONSISTENT",
            "BLOCKING",
            "price_semantics",
            scope=scope,
            evidence={"declared_price_basis": declared, "row_adjustments": adjustments},
            unverified_facts=["Mixed row adjustment semantics cannot be treated as one price basis."],
        ))
    elif adjustments:
        expected = {
            "raw_attested": {"raw"},
            "split_adjusted": {"split", "split_adjusted"},
            "total_return": {"total_return"},
        }.get(declared)
        if expected is not None and not set(adjustments) <= expected:
            semantics_status = "BLOCKED"
            attestation_status = "BLOCKED"
            findings.append(_finding(
                "PRICE_ROW_BASIS_CONFLICT",
                "BLOCKING",
                "price_semantics",
                scope=scope,
                evidence={"declared_price_basis": declared, "row_adjustments": adjustments},
                unverified_facts=["The stored row adjustment contradicts the dataset price declaration."],
            ))

    evidence_errors: list[str] = []
    if evidence is not None:
        if evidence.dataset_id != manifest.dataset_id:
            evidence_errors.append("evidence_dataset_mismatch")
        if evidence.price_basis != manifest.price_basis:
            evidence_errors.append("price_basis_mismatch")
        if not evidence.price_independent_source.strip() or evidence.price_independent_source.strip().lower() == manifest.source.strip().lower():
            evidence_errors.append("price_source_not_independent")
        evidence_errors.extend(_evidence_scope_errors(
            manifest,
            symbols=evidence.price_symbols,
            start_at=evidence.price_start_at,
            end_at=evidence.price_end_at,
            evidence_ids=evidence.price_evidence_ids,
        ))
        if evidence_errors:
            semantics_status = "BLOCKED"
            attestation_status = "BLOCKED"
            findings.append(_finding(
                "PRICE_EVIDENCE_SCOPE_MISMATCH",
                "BLOCKING",
                "price_attestation",
                scope=scope,
                evidence={"errors": sorted(set(evidence_errors)), "evidence_ids": sorted(evidence.price_evidence_ids)},
                unverified_facts=["Trusted evidence was supplied but does not match the dataset scope or semantics."],
            ))

    if semantics_status != "BLOCKED":
        if evidence is not None and not evidence_errors:
            semantics_status = "VERIFIED"
            findings.append(_finding(
                "PRICE_SEMANTICS_VERIFIED",
                "INFO",
                "price_semantics",
                scope=scope,
                evidence={"declared_price_basis": declared, "row_adjustments": adjustments, "evidence_ids": sorted(evidence.price_evidence_ids)},
                verified_facts=["An internal trusted resolver verified the declared price semantics for the full dataset window."],
            ))
        else:
            findings.append(_finding(
                "PRICE_BASIS_DECLARED_NOT_ATTESTED",
                "WARNING",
                "price_semantics",
                scope=scope,
                evidence={"declared_price_basis": declared, "row_adjustments": adjustments},
                unverified_facts=["A declaration or source name is not independent evidence of the stored price basis."],
            ))

    if attestation_status != "BLOCKED":
        if evidence is not None and not evidence_errors:
            attestation_status = "VERIFIED"
            findings.append(_finding(
                "PRICE_ATTESTATION_VERIFIED",
                "INFO",
                "price_attestation",
                scope=scope,
                evidence={"independent_source": evidence.price_independent_source, "evidence_ids": sorted(evidence.price_evidence_ids)},
                verified_facts=["Independent trusted price evidence covers every symbol in the dataset window."],
            ))
        else:
            findings.append(_finding(
                "PRICE_ATTESTATION_UNVERIFIED",
                "WARNING",
                "price_attestation",
                scope=scope,
                evidence={"declared_price_basis": declared, "manifest_status": manifest.data_quality.get("price_attestation_status")},
                unverified_facts=["Manifest status text and caller declarations are not independent attestations."],
            ))

    if evidence is not None and not evidence_errors and evidence.corporate_action_evidence_ids:
        corporate_status = "VERIFIED"
        findings.append(_finding(
            "CORPORATE_ACTION_COVERAGE_VERIFIED",
            "INFO",
            "corporate_action_coverage",
            scope=scope,
            evidence={"evidence_ids": sorted(evidence.corporate_action_evidence_ids)},
            verified_facts=["Trusted corporate-action evidence covers the dataset window."],
        ))
    else:
        findings.append(_finding(
            "CORPORATE_ACTION_COVERAGE_UNVERIFIED",
            "WARNING",
            "corporate_action_coverage",
            scope=scope,
            evidence={"manifest_status": manifest.data_quality.get("corporate_action_coverage_status")},
            unverified_facts=["Corporate actions and dividend reinvestment are not proven complete."],
        ))
        findings.append(_finding(
            "RETURN_LABEL_PRICE_ADJUSTMENT_RISK",
            "WARNING",
            "price_semantics",
            scope=scope,
            evidence={"price_semantics": semantics_status, "corporate_action_coverage": corporate_status},
            unverified_facts=[
                "Adjacent Close returns may be distorted around splits, mergers or other corporate actions.",
                "This report does not recompute or silently repair historical labels.",
            ],
        ))

    return {
        "price_semantics": semantics_status,
        "price_attestation": attestation_status,
        "corporate_action_coverage": corporate_status,
    }, findings


def _temporal_findings(
    manifest: DatasetManifest,
    scan: _Scan | None,
    evidence: TrustedResearchEvidence | None,
) -> tuple[DimensionStatus, list[ResearchQualityFinding]]:
    scope = sorted(manifest.instrument_ids)
    findings: list[ResearchQualityFinding] = []
    if scan is not None and scan.counts.get("INVALID_STOCK_TIME", 0):
        findings.append(_finding(
            "PIT_AVAILABLE_BEFORE_OBSERVATION_END",
            "BLOCKING",
            "temporal_availability",
            scope=scope,
            evidence={"count": scan.counts["INVALID_STOCK_TIME"], "examples": scan.examples.get("INVALID_STOCK_TIME", [])},
            unverified_facts=["Availability earlier than the completed market observation is an impossible PIT state."],
        ))
        return "BLOCKED", findings
    if scan is not None and scan.counts.get("INVALID_FEATURE_TIME", 0):
        findings.append(_finding(
            "PIT_FEATURE_TIME_INVALID",
            "BLOCKING",
            "temporal_availability",
            scope=scope,
            evidence={"count": scan.counts["INVALID_FEATURE_TIME"], "examples": scan.examples.get("INVALID_FEATURE_TIME", [])},
        ))
        return "BLOCKED", findings
    if scan is not None and scan.counts.get("FEATURE_AVAILABLE_AFTER_DECISION", 0):
        findings.append(_finding(
            "FEATURE_AVAILABLE_AFTER_DECISION",
            "BLOCKING",
            "feature_integrity",
            scope=scope,
            evidence={
                "count": scan.counts["FEATURE_AVAILABLE_AFTER_DECISION"],
                "examples": scan.examples.get("FEATURE_AVAILABLE_AFTER_DECISION", []),
            },
            unverified_facts=["A feature unavailable at its decision time creates synthetic future leakage."],
        ))
        return "BLOCKED", findings

    errors = []
    if evidence is not None:
        if evidence.dataset_id != manifest.dataset_id:
            errors.append("evidence_dataset_mismatch")
        if evidence.temporal_policy != manifest.availability_policy:
            errors.append("availability_policy_mismatch")
        errors.extend(_evidence_scope_errors(
            manifest,
            symbols=evidence.temporal_symbols,
            start_at=evidence.temporal_start_at,
            end_at=evidence.temporal_end_at,
            evidence_ids=evidence.temporal_evidence_ids,
        ))
    if errors:
        findings.append(_finding(
            "PIT_EVIDENCE_SCOPE_MISMATCH",
            "BLOCKING",
            "temporal_availability",
            scope=scope,
            evidence={"errors": sorted(set(errors))},
        ))
        return "BLOCKED", findings
    if evidence is not None:
        findings.append(_finding(
            "PIT_PUBLICATION_TIME_VERIFIED",
            "INFO",
            "temporal_availability",
            scope=scope,
            evidence={"availability_policy": manifest.availability_policy, "evidence_ids": sorted(evidence.temporal_evidence_ids)},
            verified_facts=["Trusted historical publication evidence covers the full dataset window."],
        ))
        return "VERIFIED", findings

    code = (
        "PIT_AVAILABILITY_MODELED"
        if manifest.availability_policy == "modeled_bar_end_plus_delay"
        else "PIT_HISTORICAL_PUBLICATION_UNVERIFIED"
    )
    findings.append(_finding(
        code,
        "WARNING",
        "temporal_availability",
        scope=scope,
        evidence={"availability_policy": manifest.availability_policy},
        unverified_facts=[
            "The availability timestamp is a rule-based model, not an observed historical publication record.",
            "Dataset capture time is not evidence of historical availability.",
        ],
    ))
    return "UNVERIFIED", findings


def _universe_findings(
    manifest: DatasetManifest,
    evidence: TrustedResearchEvidence | None,
) -> tuple[DimensionStatus, list[ResearchQualityFinding]]:
    if manifest.dataset_kind != "stock_bars":
        return "NOT_APPLICABLE", []
    scope = sorted(manifest.instrument_ids)
    findings: list[ResearchQualityFinding] = []
    errors = []
    if evidence is not None:
        if evidence.dataset_id != manifest.dataset_id:
            errors.append("evidence_dataset_mismatch")
        errors.extend(_evidence_scope_errors(
            manifest,
            symbols=evidence.universe_symbols,
            start_at=evidence.universe_start_at,
            end_at=evidence.universe_end_at,
            evidence_ids=evidence.universe_evidence_ids,
        ))
        for field_name in (
            "membership_verified",
            "listing_status_verified",
            "delisting_coverage_verified",
            "symbol_identity_verified",
        ):
            if not getattr(evidence, field_name):
                errors.append(f"{field_name}_false")
    if errors:
        findings.append(_finding(
            "UNIVERSE_EVIDENCE_SCOPE_MISMATCH",
            "BLOCKING",
            "universe_validity",
            scope=scope,
            evidence={"errors": sorted(set(errors))},
        ))
        return "BLOCKED", findings
    if evidence is not None:
        findings.append(_finding(
            "UNIVERSE_MEMBERSHIP_VERIFIED",
            "INFO",
            "universe_validity",
            scope=scope,
            evidence={"evidence_ids": sorted(evidence.universe_evidence_ids)},
            verified_facts=["Point-in-time membership, listing, delisting and symbol identity evidence covers the full window."],
        ))
        return "VERIFIED", findings

    mode = str(manifest.universe_definition.get("mode", "unknown")).strip().lower()
    findings.extend([
        _finding(
            "UNIVERSE_MEMBERSHIP_UNVERIFIED",
            "WARNING",
            "universe_validity",
            scope=scope,
            evidence={"mode": mode, "symbol_count": len(scope)},
            unverified_facts=["An explicit symbol list does not prove historical index membership or selection eligibility."],
        ),
        _finding(
            "HISTORICAL_LISTING_STATUS_UNKNOWN",
            "WARNING",
            "universe_validity",
            scope=scope,
            evidence={"mode": mode},
            unverified_facts=["First-listing dates are not proven by the presence of bars."],
        ),
        _finding(
            "DELISTING_COVERAGE_UNKNOWN",
            "WARNING",
            "universe_validity",
            scope=scope,
            evidence={"mode": mode},
            unverified_facts=["Delisted instruments may be absent from the current provider universe."],
        ),
        _finding(
            "SYMBOL_IDENTITY_HISTORY_UNKNOWN",
            "WARNING",
            "universe_validity",
            scope=scope,
            evidence={"mode": mode},
            unverified_facts=["A ticker string does not prove stable legal security identity through time."],
        ),
    ])
    if mode in {"current_index_constituents", "current_constituents", "retrospective_current_constituents"}:
        findings.append(_finding(
            "RETROSPECTIVE_CONSTITUENT_BIAS_RISK",
            "WARNING",
            "universe_validity",
            scope=scope,
            evidence={"mode": mode},
            verified_facts=["The manifest explicitly declares a current-constituent snapshot."],
            unverified_facts=["Using today's members for historical dates introduces survivorship and look-ahead selection risk."],
        ))
    elif mode not in {"explicit"}:
        findings.append(_finding(
            "UNIVERSE_SOURCE_UNKNOWN",
            "WARNING",
            "universe_validity",
            scope=scope,
            evidence={"mode": mode},
        ))
    return "UNVERIFIED", findings


def _scan_row_example(code: str, row: dict[str, Any], **extra: Any) -> dict[str, Any]:
    example = {"instrument_id": row.get("instrument_id"), **extra}
    if code in {"INVALID_STOCK_TIME", "INVALID_FEATURE_TIME", "INVALID_PREDICTION_TIME", "FEATURE_AVAILABLE_AFTER_DECISION"}:
        for key in ("start_at", "end_at", "available_at", "decision_at", "feature_available_at"):
            value = row.get(key)
            if isinstance(value, datetime):
                example[key] = _iso(value)
    return example


def _iter_partition_rows(store: DatasetStore, manifest: DatasetManifest, partition, columns: list[str], batch_size: int):
    path = store.datasets / manifest.dataset_id.removeprefix("sha256:") / partition.logical_path
    parquet = pq.ParquetFile(path)
    for batch in parquet.iter_batches(batch_size=batch_size, columns=columns):
        yield from batch.to_pylist()


def _scan_stock(store: DatasetStore, manifest: DatasetManifest, limits: ResearchQualityLimits) -> _Scan:
    scan = _Scan()
    previous_identity: dict[str, tuple[Any, Any, Any]] = {}
    previous_start: dict[str, datetime] = {}
    previous_end: dict[str, datetime] = {}
    previous_close: dict[str, tuple[datetime, float]] = {}
    columns = ["instrument_id", "start_at", "end_at", "available_at", "open", "high", "low", "close", "volume", "adjustment"]
    for partition in sorted(manifest.partition_manifest, key=lambda item: item.ordinal):
        for row in _iter_partition_rows(store, manifest, partition, columns, limits.scan_batch_size):
            scan.rows_scanned += 1
            if scan.rows_scanned > limits.max_scan_rows:
                raise AlphaError(
                    AlphaErrorCode.RESOURCE_LIMIT_EXCEEDED,
                    "Research quality scan exceeded max_scan_rows; refusing a partial assessment",
                    {"dataset_id": manifest.dataset_id, "max_scan_rows": limits.max_scan_rows},
                )
            instrument = str(row.get("instrument_id") or "").upper()
            start, end, available = _utc(row.get("start_at")), _utc(row.get("end_at")), _utc(row.get("available_at"))
            identity = (instrument, start, end)
            if instrument and identity == previous_identity.get(instrument):
                scan.record("DUPLICATE_STOCK_BAR", _scan_row_example("DUPLICATE_STOCK_BAR", row))
            elif instrument and start is not None and instrument in previous_start:
                if start <= previous_start[instrument] or (end is not None and end <= previous_start[instrument]):
                    scan.record("STOCK_ROW_OVERLAP", _scan_row_example("STOCK_ROW_OVERLAP", row))
                elif instrument in previous_end and start < previous_end[instrument]:
                    scan.record("STOCK_ROW_OVERLAP", _scan_row_example("STOCK_ROW_OVERLAP", row))
                elif start < previous_start[instrument]:
                    scan.record("STOCK_ROW_ORDER_INVALID", _scan_row_example("STOCK_ROW_ORDER_INVALID", row))
            if instrument and start is not None:
                previous_identity[instrument] = identity
                previous_start[instrument] = start
                if end is not None:
                    previous_end[instrument] = end
            if start is None or end is None or available is None or start >= end or available < end:
                scan.record("INVALID_STOCK_TIME", _scan_row_example("INVALID_STOCK_TIME", row))
            values = [_number(row.get(key)) for key in ("open", "high", "low", "close")]
            ohlc = row.get("open"), row.get("high"), row.get("low"), row.get("close")
            if (
                any(value is None or value <= 0 for value in values)
                or values[1] < max(values[0], values[3])
                or values[2] > min(values[0], values[3])
                or values[2] > values[1]
            ):
                scan.record("INVALID_STOCK_OHLC", _scan_row_example("INVALID_STOCK_OHLC", row, values=list(ohlc)))
            else:
                if instrument and start is not None and instrument in previous_close:
                    previous_at, previous_value = previous_close[instrument]
                    if start > previous_at and previous_value > 0:
                        ratio = values[3] / previous_value
                        if ratio >= 5 or ratio <= 0.2:
                            scan.record(
                                "ABNORMAL_PRICE_JUMP",
                                _scan_row_example("ABNORMAL_PRICE_JUMP", row, previous_close=previous_value, ratio=ratio),
                            )
                if instrument and start is not None:
                    previous_close[instrument] = (start, values[3])
            volume = row.get("volume")
            if volume is not None and (_number(volume) is None or float(volume) < 0):
                scan.record("INVALID_STOCK_VOLUME", _scan_row_example("INVALID_STOCK_VOLUME", row, volume=volume))
            adjustment = row.get("adjustment")
            normalized_adjustment = adjustment.strip() if isinstance(adjustment, str) and adjustment.strip() else "unknown"
            if normalized_adjustment in scan.adjustments:
                pass
            elif len(scan.adjustments) < 32:
                scan.adjustments.add(normalized_adjustment)
            else:
                scan.adjustments.add("<additional-adjustments>")
    expected = sum(part.row_count for part in manifest.partition_manifest)
    if scan.rows_scanned != expected:
        scan.counts["ROW_COUNT_MISMATCH"] = abs(scan.rows_scanned - expected)
    return scan


def _scan_feature(store: DatasetStore, manifest: DatasetManifest, limits: ResearchQualityLimits) -> _Scan:
    scan = _Scan()
    seen: set[tuple[Any, str, str]] = set()
    columns = ["decision_at", "instrument_id", "factor_name", "factor_value", "feature_available_at"]
    for partition in sorted(manifest.partition_manifest, key=lambda item: item.ordinal):
        for row in _iter_partition_rows(store, manifest, partition, columns, limits.scan_batch_size):
            scan.rows_scanned += 1
            if scan.rows_scanned > limits.max_scan_rows:
                raise AlphaError(
                    AlphaErrorCode.RESOURCE_LIMIT_EXCEEDED,
                    "Research quality scan exceeded max_scan_rows; refusing a partial assessment",
                    {"dataset_id": manifest.dataset_id, "max_scan_rows": limits.max_scan_rows},
                )
            decision = _utc(row.get("decision_at"))
            available = _utc(row.get("feature_available_at"))
            instrument = str(row.get("instrument_id") or "").upper()
            factor_name = str(row.get("factor_name") or "")
            identity = (decision, instrument, factor_name)
            if identity in seen:
                scan.record("DUPLICATE_FEATURE_ROW", _scan_row_example("DUPLICATE_FEATURE_ROW", row))
            seen.add(identity)
            if decision is None or available is None:
                scan.record("INVALID_FEATURE_TIME", _scan_row_example("INVALID_FEATURE_TIME", row))
            elif available > decision:
                scan.record("FEATURE_AVAILABLE_AFTER_DECISION", _scan_row_example("FEATURE_AVAILABLE_AFTER_DECISION", row))
            value = row.get("factor_value")
            if value is not None and _number(value) is None:
                scan.record("INVALID_FEATURE_VALUE", _scan_row_example("INVALID_FEATURE_VALUE", row, factor_value=value))
    expected = sum(part.row_count for part in manifest.partition_manifest)
    if scan.rows_scanned != expected:
        scan.counts["ROW_COUNT_MISMATCH"] = abs(scan.rows_scanned - expected)
    return scan


def _scan_prediction(store: DatasetStore, manifest: DatasetManifest, limits: ResearchQualityLimits) -> _Scan:
    scan = _Scan()
    seen: set[tuple[Any, str, str, str]] = set()
    columns = ["decision_at", "instrument_id", "prediction_value", "model_id", "model_set_id", "fold"]
    for partition in sorted(manifest.partition_manifest, key=lambda item: item.ordinal):
        for row in _iter_partition_rows(store, manifest, partition, columns, limits.scan_batch_size):
            scan.rows_scanned += 1
            if scan.rows_scanned > limits.max_scan_rows:
                raise AlphaError(
                    AlphaErrorCode.RESOURCE_LIMIT_EXCEEDED,
                    "Research quality scan exceeded max_scan_rows; refusing a partial assessment",
                    {"dataset_id": manifest.dataset_id, "max_scan_rows": limits.max_scan_rows},
                )
            decision = _utc(row.get("decision_at"))
            identity = (
                decision,
                str(row.get("instrument_id") or "").upper(),
                str(row.get("model_id") or ""),
                str(row.get("fold") or ""),
            )
            if identity in seen:
                scan.record("DUPLICATE_PREDICTION_ROW", _scan_row_example("DUPLICATE_PREDICTION_ROW", row))
            seen.add(identity)
            if decision is None:
                scan.record("INVALID_PREDICTION_TIME", _scan_row_example("INVALID_PREDICTION_TIME", row))
            value = row.get("prediction_value")
            if value is None or _number(value) is None:
                scan.record("INVALID_PREDICTION_VALUE", _scan_row_example("INVALID_PREDICTION_VALUE", row, prediction_value=value))
    expected = sum(part.row_count for part in manifest.partition_manifest)
    if scan.rows_scanned != expected:
        scan.counts["ROW_COUNT_MISMATCH"] = abs(scan.rows_scanned - expected)
    return scan


def _artifact_findings(
    store: DatasetStore,
    manifest: DatasetManifest,
    limits: ResearchQualityLimits,
) -> tuple[DimensionStatus, list[ResearchQualityFinding], _Scan | None]:
    if len(manifest.partition_manifest) > limits.max_partitions:
        raise AlphaError(
            AlphaErrorCode.RESOURCE_LIMIT_EXCEEDED,
            "Research quality scan exceeded max_partitions",
            {"dataset_id": manifest.dataset_id, "max_partitions": limits.max_partitions},
        )
    declared_rows = sum(part.row_count for part in manifest.partition_manifest)
    if declared_rows > limits.max_scan_rows:
        raise AlphaError(
            AlphaErrorCode.RESOURCE_LIMIT_EXCEEDED,
            "Research quality scan exceeded max_scan_rows",
            {"dataset_id": manifest.dataset_id, "max_scan_rows": limits.max_scan_rows, "row_count": declared_rows},
        )
    validation = store.validate_dataset(manifest.dataset_id)
    findings: list[ResearchQualityFinding] = []
    if not validation.valid:
        issues = sorted(validation.issues, key=lambda item: (str(item.get("code")), str(item.get("path")), str(item.get("message"))))
        findings.append(_finding(
            "ARTIFACT_INTEGRITY_FAILED",
            "BLOCKING",
            "artifact_integrity",
            evidence={"issue_count": len(issues), "issues": issues[:20]},
            unverified_facts=["Dataset identity, partition hashes or file sizes do not prove a valid immutable artifact."],
        ))
        return "BLOCKED", findings, None
    try:
        if manifest.dataset_kind == "stock_bars":
            scan = _scan_stock(store, manifest, limits)
        elif manifest.dataset_kind == "feature_panel":
            scan = _scan_feature(store, manifest, limits)
        elif manifest.dataset_kind == "prediction_panel":
            scan = _scan_prediction(store, manifest, limits)
        else:
            scan = _Scan(rows_scanned=declared_rows)
    except AlphaError:
        raise
    except Exception as error:
        findings.append(_finding(
            "ARTIFACT_ROWS_UNREADABLE",
            "BLOCKING",
            "artifact_integrity",
            evidence={"error_type": type(error).__name__, "message": str(error)[:512]},
            unverified_facts=["Partition bytes cannot be decoded into the declared dataset schema."],
        ))
        return "BLOCKED", findings, None

    status: DimensionStatus = "VERIFIED"
    if scan.counts.get("ROW_COUNT_MISMATCH"):
        status = "BLOCKED"
        findings.append(_finding(
            "ROW_COUNT_MISMATCH",
            "BLOCKING",
            "artifact_integrity",
            evidence={"declared": declared_rows, "scanned": scan.rows_scanned},
        ))
    artifact_codes = set()
    if manifest.dataset_kind == "stock_bars":
        artifact_codes.update(_STOCK_ARTIFACT_CODES)
    elif manifest.dataset_kind == "prediction_panel":
        artifact_codes.update(_PREDICTION_ARTIFACT_CODES)
    for code in sorted(artifact_codes):
        count = scan.counts.get(code, 0)
        if not count:
            continue
        severity: FindingSeverity = "WARNING" if code in _NON_BLOCKING_SCAN_CODES else "BLOCKING"
        if severity == "BLOCKING":
            status = "BLOCKED"
        elif status == "VERIFIED":
            status = "UNVERIFIED"
        findings.append(_finding(
            code,
            severity,
            "artifact_integrity",
            evidence={"count": count, "examples": scan.examples.get(code, [])},
            unverified_facts=(
                ["A large adjacent price ratio is only an anomaly signal; it is not proof of a corporate action or an error, and the assessor will not auto-adjust prices."]
                if code == "ABNORMAL_PRICE_JUMP" else []
            ),
        ))

    quality = manifest.data_quality
    metadata_errors: list[str] = []
    non_session_raw = quality.get("non_session_bars")
    if non_session_raw is None:
        non_session = []
    elif isinstance(non_session_raw, list):
        non_session = non_session_raw
    else:
        non_session = []
        metadata_errors.append("non_session_bars")
    missing_ohlcv, missing_valid = _nonnegative_count(quality.get("missing_ohlcv_rows"))
    invalid_volume, volume_valid = _nonnegative_count(quality.get("volume_invalid_count"))
    if not missing_valid:
        metadata_errors.append("missing_ohlcv_rows")
    if not volume_valid:
        metadata_errors.append("volume_invalid_count")
    if non_session:
        status = "UNVERIFIED" if status == "VERIFIED" else status
        findings.append(_finding(
            "NON_SESSION_BARS_EXCLUDED",
            "WARNING",
            "artifact_integrity",
            evidence={"count": len(non_session), "examples": list(non_session)[:5]},
            unverified_facts=["Rows outside the declared trading calendar were excluded and are not independently reconciled."],
        ))
    if missing_ohlcv:
        status = "UNVERIFIED" if status == "VERIFIED" else status
        findings.append(_finding(
            "MISSING_OHLCV_ROWS_EXCLUDED",
            "WARNING",
            "artifact_integrity",
            evidence={"count": missing_ohlcv},
        ))
    if invalid_volume:
        status = "UNVERIFIED" if status == "VERIFIED" else status
        findings.append(_finding(
            "VOLUME_INVALID_OBSERVATIONS",
            "WARNING",
            "artifact_integrity",
            evidence={"count": invalid_volume},
        ))

    if manifest.dataset_kind == "stock_bars":
        reports = quality.get("session_coverage_by_symbol")
        if not isinstance(reports, list) or not reports:
            status = "UNVERIFIED" if status == "VERIFIED" else status
            findings.append(_finding(
                "SESSION_COVERAGE_UNVERIFIED",
                "WARNING",
                "artifact_integrity",
                unverified_facts=["Missing trading sessions cannot be distinguished from suspensions or absent observations."],
            ))
        else:
            missing = extra = duplicates = 0
            expected_symbols = set(manifest.instrument_ids)
            report_symbols: list[str] = []
            missing_fields: dict[str, list[str]] = {}
            required_fields = {
                "instrument_id",
                "calendar_id",
                "first_observed_session",
                "last_observed_session",
                "expected_sessions_within_observed_range",
                "observed_sessions_within_observed_range",
                "missing_sessions",
                "extra_sessions",
                "duplicate_sessions",
                "missing_session_count",
                "extra_session_count",
                "duplicate_session_count",
                "session_coverage",
                "session_status",
                "outside_observed_range_status",
            }
            for item in reports:
                if not isinstance(item, dict):
                    metadata_errors.append("session_coverage_by_symbol")
                    continue
                symbol = str(item.get("instrument_id") or "").strip().upper()
                if symbol:
                    report_symbols.append(symbol)
                absent = sorted(required_fields - set(item))
                if not symbol and "instrument_id" not in absent:
                    absent.append("instrument_id")
                if absent:
                    missing_fields[symbol or "<missing-instrument-id>"] = absent
                for field_name, label in (
                    ("missing_session_count", "missing"),
                    ("extra_session_count", "extra"),
                    ("duplicate_session_count", "duplicate"),
                ):
                    value, valid = _nonnegative_count(item.get(field_name))
                    if not valid:
                        metadata_errors.append(f"session_coverage_by_symbol.{field_name}")
                    if label == "missing":
                        missing += value
                    elif label == "extra":
                        extra += value
                    else:
                        duplicates += value
            missing_symbols = sorted(expected_symbols - set(report_symbols))
            extra_symbols = sorted(set(report_symbols) - expected_symbols)
            duplicate_symbols = sorted({symbol for symbol in report_symbols if report_symbols.count(symbol) > 1})
            if missing_symbols or extra_symbols or duplicate_symbols:
                status = "UNVERIFIED" if status == "VERIFIED" else status
                findings.append(_finding(
                    "SESSION_COVERAGE_IDENTITY_INCOMPLETE",
                    "WARNING",
                    "artifact_integrity",
                    evidence={
                        "missing_symbols": missing_symbols[:20],
                        "missing_symbol_count": len(missing_symbols),
                        "extra_symbols": extra_symbols[:20],
                        "extra_symbol_count": len(extra_symbols),
                        "duplicate_symbols": duplicate_symbols[:20],
                        "duplicate_symbol_count": len(duplicate_symbols),
                    },
                    unverified_facts=["Session coverage must contain exactly one complete report for every manifest instrument."],
                ))
            if missing_fields:
                status = "UNVERIFIED" if status == "VERIFIED" else status
                findings.append(_finding(
                    "SESSION_COVERAGE_FIELDS_INCOMPLETE",
                    "WARNING",
                    "artifact_integrity",
                    evidence={
                        "instrument_ids": sorted(missing_fields)[:20],
                        "missing_field_examples": {
                            symbol: missing_fields[symbol]
                            for symbol in sorted(missing_fields)[:5]
                        },
                    },
                    unverified_facts=["Missing session-audit fields are unresolved evidence, not complete coverage."],
                ))
            no_bars = sorted(str(item.get("instrument_id")) for item in reports if isinstance(item, dict) and item.get("session_status") == "NO_BARS")
            if extra or duplicates:
                status = "BLOCKED"
                findings.append(_finding(
                    "SESSION_COVERAGE_ANOMALIES",
                    "BLOCKING",
                    "artifact_integrity",
                    evidence={"extra_session_count": extra, "duplicate_session_count": duplicates},
                ))
            if missing:
                status = "UNVERIFIED" if status == "VERIFIED" else status
                findings.append(_finding(
                    "SESSION_COVERAGE_GAPS",
                    "WARNING",
                    "artifact_integrity",
                    evidence={"missing_session_count": missing},
                    unverified_facts=["A missing session may be a suspension, delisting or absent data and is not inferred here."],
                ))
            if no_bars:
                status = "UNVERIFIED" if status == "VERIFIED" else status
                findings.append(_finding(
                    "SESSION_COVERAGE_NO_BARS",
                    "WARNING",
                    "artifact_integrity",
                    scope=no_bars,
                ))
    if metadata_errors:
        status = "UNVERIFIED" if status == "VERIFIED" else status
        findings.append(_finding(
            "QUALITY_METADATA_INVALID",
            "WARNING",
            "artifact_integrity",
            evidence={"fields": sorted(set(metadata_errors))},
            unverified_facts=["Malformed quality metadata is not interpreted as a complete coverage result."],
        ))
    return status, findings, scan


def _feature_integrity(manifest: DatasetManifest, scan: _Scan | None) -> tuple[DimensionStatus, list[ResearchQualityFinding]]:
    if manifest.dataset_kind != "feature_panel":
        return "NOT_APPLICABLE", []
    if scan is None:
        return "UNVERIFIED", []
    codes = sorted(code for code in scan.counts if code in _FEATURE_SCAN_CODES)
    if any(code != "ABNORMAL_PRICE_JUMP" for code in codes):
        findings = [_finding(
            code,
            "BLOCKING",
            "feature_integrity",
            evidence={"count": scan.counts[code], "examples": scan.examples.get(code, [])},
        ) for code in codes]
        return "BLOCKED", findings
    return "VERIFIED", []


def _local_assessment(
    manifest: DatasetManifest,
    store: DatasetStore,
    limits: ResearchQualityLimits,
    resolver: ResearchEvidenceResolver | None,
) -> _LocalAssessment:
    statuses: dict[DimensionName, DimensionStatus] = {name: "NOT_APPLICABLE" for name in DIMENSIONS}
    artifact_status, findings, scan = _artifact_findings(store, manifest, limits)
    statuses["artifact_integrity"] = artifact_status
    statuses["lineage_integrity"] = "VERIFIED"

    evidence = _trusted_evidence(manifest, resolver)
    price_statuses, price_findings = _price_findings(manifest, scan, evidence)
    statuses.update(price_statuses)
    findings.extend(price_findings)

    temporal_status, temporal_findings = _temporal_findings(manifest, scan, evidence)
    statuses["temporal_availability"] = temporal_status
    findings.extend(temporal_findings)

    universe_status, universe_findings = _universe_findings(manifest, evidence)
    statuses["universe_validity"] = universe_status
    findings.extend(universe_findings)

    feature_status, feature_findings = _feature_integrity(manifest, scan)
    statuses["feature_integrity"] = feature_status
    findings.extend(feature_findings)

    return _LocalAssessment(manifest=manifest, statuses=statuses, findings=findings)


def _inherit_status(child: DimensionStatus, parent: DimensionStatus) -> DimensionStatus:
    if child == "BLOCKED" or parent == "BLOCKED":
        return "BLOCKED"
    if child == "UNVERIFIED" or parent == "UNVERIFIED":
        return "UNVERIFIED"
    if parent == "VERIFIED":
        return "VERIFIED"
    return child


def _dedupe_findings(findings: list[ResearchQualityFinding]) -> list[ResearchQualityFinding]:
    unique: dict[tuple[Any, ...], ResearchQualityFinding] = {}
    for finding in findings:
        key = (
            finding.dimension,
            finding.code,
            tuple(finding.scope),
            finding.severity,
            digest(finding.evidence),
            tuple(finding.verified_facts),
            tuple(finding.unverified_facts),
        )
        unique[key] = finding
    return list(unique.values())


def _research_readiness(statuses: dict[DimensionName, DimensionStatus]) -> ResearchReadiness:
    applicable = [status for status in statuses.values() if status != "NOT_APPLICABLE"]
    if any(status == "BLOCKED" for status in applicable):
        return "RESEARCH_BLOCKED"
    if applicable and all(status == "VERIFIED" for status in applicable):
        return "RESEARCH_DATA_VALIDATED"
    return "RESEARCH_EXPLORATORY"


def _build_report(
    manifest: DatasetManifest,
    statuses: dict[DimensionName, DimensionStatus],
    findings: list[ResearchQualityFinding],
    limits: ResearchQualityLimits,
) -> ResearchQualityReport:
    bounded = _bounded_findings(_dedupe_findings(findings), limits.max_findings)
    dimensions = {
        name: ResearchDimension(
            status=statuses[name],
            summary=_status_summary(statuses[name]),
            finding_codes=sorted({finding.code for finding in bounded if finding.dimension == name}),
        )
        for name in DIMENSIONS
    }
    payload = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "dataset_id": manifest.dataset_id,
        "dataset_kind": manifest.dataset_kind,
        "policy_version": POLICY_VERSION,
        "research_readiness": _research_readiness(statuses),
        "dimensions": {name: dimensions[name].model_dump(mode="json") for name in DIMENSIONS},
        "findings": [finding.model_dump(mode="json") for finding in bounded],
        "parent_dataset_ids": list(manifest.parent_dataset_ids),
    }
    return ResearchQualityReport.model_validate({**payload, "report_hash": digest(payload)})


def assess_research_quality(
    dataset_id: str,
    store: DatasetStore,
    *,
    evidence: ResearchEvidenceResolver | None = None,
    limits: ResearchQualityLimits | None = None,
) -> ResearchQualityReport:
    """Assess research readiness without mutating manifests or artifacts."""
    active_limits = limits or ResearchQualityLimits()
    cache: dict[str, ResearchQualityReport] = {}
    budget = _AssessmentBudget(active_limits)

    def walk(identifier: str, stack: tuple[str, ...]) -> ResearchQualityReport:
        if identifier in stack:
            raise _DependencyCycle([*stack, identifier])
        if len(stack) >= active_limits.max_parent_depth:
            raise _DependencyDepth(identifier)
        if identifier in cache:
            return cache[identifier]
        manifest = store.load_manifest(identifier)
        budget.visit(manifest)
        local = _local_assessment(manifest, store, active_limits, evidence)
        statuses = dict(local.statuses)
        findings = list(local.findings)
        if len(set(manifest.parent_dataset_ids)) != len(manifest.parent_dataset_ids):
            statuses["lineage_integrity"] = "BLOCKED"
            findings.append(_finding(
                "DUPLICATE_PARENT_DATASET",
                "BLOCKING",
                "lineage_integrity",
                evidence={"parent_dataset_ids": list(manifest.parent_dataset_ids)},
                unverified_facts=["Duplicate parent identities make lineage evidence ambiguous."],
            ))
        parent_reports: list[ResearchQualityReport] = []
        for parent_id in list(dict.fromkeys(manifest.parent_dataset_ids)):
            try:
                parent_reports.append(walk(parent_id, (*stack, identifier)))
            except _DependencyCycle as error:
                statuses["lineage_integrity"] = "BLOCKED"
                findings.append(_finding(
                    "PARENT_DATASET_CYCLE",
                    "BLOCKING",
                    "lineage_integrity",
                    scope=[parent_id],
                    evidence={"cycle": error.path},
                    unverified_facts=["Cyclic parent evidence cannot be resolved deterministically."],
                ))
            except _DependencyDepth:
                statuses["lineage_integrity"] = "BLOCKED"
                findings.append(_finding(
                    "PARENT_DEPTH_LIMIT_EXCEEDED",
                    "BLOCKING",
                    "lineage_integrity",
                    scope=[parent_id],
                    evidence={"max_parent_depth": active_limits.max_parent_depth},
                ))
            except AlphaError as error:
                if error.code == AlphaErrorCode.RESOURCE_LIMIT_EXCEEDED:
                    raise
                statuses["lineage_integrity"] = "BLOCKED"
                findings.append(_finding(
                    "PARENT_DATASET_UNREADABLE",
                    "BLOCKING",
                    "lineage_integrity",
                    scope=[parent_id],
                    evidence={"error_code": error.code.value, "message": error.message[:512]},
                    unverified_facts=["The parent dataset cannot be validated, so child quality cannot be upgraded."],
                ))
            except (KeyError, ValueError, OSError) as error:
                statuses["lineage_integrity"] = "BLOCKED"
                findings.append(_finding(
                    "PARENT_DATASET_UNREADABLE",
                    "BLOCKING",
                    "lineage_integrity",
                    scope=[parent_id],
                    evidence={"error_type": type(error).__name__, "message": str(error)[:512]},
                    unverified_facts=["The parent dataset cannot be validated, so child quality cannot be upgraded."],
                ))
        for parent in parent_reports:
            for name in DIMENSIONS:
                statuses[name] = _inherit_status(statuses[name], parent.dimensions[name].status)
            for finding in parent.findings:
                chain = finding.evidence.get("inheritance_chain")
                if not isinstance(chain, list) or any(not isinstance(value, str) for value in chain):
                    previous = finding.evidence.get("inherited_from")
                    chain = [previous] if isinstance(previous, str) else []
                lineage_chain = list(dict.fromkeys([*chain, parent.dataset_id]))
                findings.append(finding.model_copy(update={"scope": [parent.dataset_id, *finding.scope], "evidence": {
                    **finding.evidence,
                    "inherited_from": parent.dataset_id,
                    "inheritance_chain": lineage_chain,
                }}))
        report = _build_report(manifest, statuses, findings, active_limits)
        cache[identifier] = report
        return report

    return walk(dataset_id, ())
