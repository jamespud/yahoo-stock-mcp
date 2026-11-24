"""Evidence-report façade bound to a Campaign or sealed Ledger Snapshot."""
from __future__ import annotations

from .contracts import ResearchEvidenceReport
from .experiment_registry import ResearchExperimentRegistry


def build_evidence_report(
    registry: ResearchExperimentRegistry,
    *,
    campaign_id: str,
    protocol_hash: str | None = None,
    candidate_ledger_hash: str | None = None,
) -> ResearchEvidenceReport:
    return registry.build_evidence_report(
        campaign_id=campaign_id,
        protocol_hash=protocol_hash,
        candidate_ledger_hash=candidate_ledger_hash,
    )
