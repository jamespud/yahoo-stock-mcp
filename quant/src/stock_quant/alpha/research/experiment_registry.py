"""Append-only research Campaign, Protocol, Trial and selection registry."""
from __future__ import annotations

import base64
import json
import math
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ...compiler import digest
from ...pagination import result_page
from ..errors import AlphaError, AlphaErrorCode
from ..models.baseline_evidence import BaselineEvidenceLoader, BaselineEvidenceError
from ..runtime_budget import TRAINING_DEFAULT_SECONDS
from .contracts import (
    ExcludedCandidate,
    HypothesisRecord,
    ResearchCampaignDefinition,
    ResearchCampaignRecord,
    ResearchDataReuseFinding,
    ResearchEvidenceReport,
    ResearchHypothesisDefinition,
    ResearchLedgerEvent,
    ResearchLedgerIntegrity,
    ResearchLedgerSnapshot,
    ResearchProtocolDefinition,
    ResearchProtocolRecord,
    ResearchSelection,
    ResearchTrialJobRecord,
    ResearchTrialRecord,
    ResearchTrialTerminalState,
    ScopeCompleteness,
    TrialAccounting,
    TrialPurpose,
)
from .protocol import (
    campaign_identity,
    canonical_digest,
    event_hash_for,
    hypothesis_hash_for,
    normalized_research_request,
    protocol_hash_for,
    report_hash_for,
    selection_hash_for,
    selection_policy_hash_for,
    snapshot_hash_for,
    trial_identities,
)

TERMINAL_TRIAL_STATES = {"COMPLETED", "FAILED", "CANCELLED", "REJECTED"}
TERMINAL_JOB_STATES = {"completed", "failed", "cancelled"}
CAMPAIGN_EVENT_TYPES = {
    "CAMPAIGN_REGISTERED", "PROTOCOL_REGISTERED", "PROTOCOL_REVISED", "HYPOTHESIS_REGISTERED",
    "HYPOTHESIS_UPDATED", "TRIAL_REGISTERED", "TRIAL_SUBMITTED", "TRIAL_EXECUTION_STARTED",
    "TRIAL_RETRY_STARTED", "TRIAL_PUBLISHING", "TRIAL_COMPLETED", "TRIAL_FAILED",
    "TRIAL_CANCELLED", "TRIAL_WORKER_LOST", "TRIAL_REJECTED", "TRIAL_RESULT_ACCESSED",
    "LEDGER_SEALED", "SELECTION_REGISTERED", "CAMPAIGN_SEALED", "CAMPAIGN_CLOSED",
}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _load(value: str | bytes | None) -> Any:
    return None if value is None else json.loads(value)


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


@contextmanager
def _transaction(connection: sqlite3.Connection, *, immediate: bool = True):
    """Reuse an outer transaction, otherwise open and commit a bounded transaction."""
    owns = not connection.in_transaction
    if owns:
        connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        if owns:
            connection.rollback()
        raise
    else:
        if owns:
            connection.commit()


class ResearchExperimentRegistry:
    """SQLite-backed governance layer sharing the JobStore connection."""

    def __init__(self, connection: sqlite3.Connection, *, job_store=None, artifact_store=None):
        self.connection = connection
        self.job_store = job_store
        self.artifact_store = artifact_store
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self.connection.executescript('''
            CREATE TABLE IF NOT EXISTS alpha_research_campaigns (
                campaign_id TEXT PRIMARY KEY,
                campaign_identity_hash TEXT NOT NULL UNIQUE,
                definition TEXT NOT NULL,
                created_at TEXT NOT NULL,
                campaign_status TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS alpha_research_protocols (
                protocol_hash TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL REFERENCES alpha_research_campaigns(campaign_id),
                definition TEXT NOT NULL,
                modification_ledger_watermark INTEGER NOT NULL,
                exposure_context TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(campaign_id, protocol_hash)
            );
            CREATE INDEX IF NOT EXISTS alpha_research_protocol_campaign
                ON alpha_research_protocols(campaign_id, created_at, protocol_hash);
            CREATE TABLE IF NOT EXISTS alpha_research_hypotheses (
                hypothesis_id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL REFERENCES alpha_research_campaigns(campaign_id),
                protocol_hash TEXT NOT NULL REFERENCES alpha_research_protocols(protocol_hash),
                definition TEXT NOT NULL,
                hypothesis_definition_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS alpha_research_hypothesis_protocol
                ON alpha_research_hypotheses(protocol_hash, created_at);
            CREATE TABLE IF NOT EXISTS alpha_research_trials (
                trial_id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL REFERENCES alpha_research_campaigns(campaign_id),
                protocol_hash TEXT NOT NULL REFERENCES alpha_research_protocols(protocol_hash),
                hypothesis_id TEXT NOT NULL REFERENCES alpha_research_hypotheses(hypothesis_id),
                trial_definition_hash TEXT NOT NULL,
                canonical_request_hash TEXT NOT NULL,
                canonical_request TEXT NOT NULL,
                evidence_scope_hash TEXT NOT NULL,
                parent_trial_id TEXT,
                trial_purpose TEXT NOT NULL,
                preregistration_status TEXT NOT NULL,
                registered_at TEXT NOT NULL,
                registered_ledger_watermark INTEGER NOT NULL,
                status TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS alpha_research_trial_campaign
                ON alpha_research_trials(campaign_id, registered_at, trial_id);
            CREATE INDEX IF NOT EXISTS alpha_research_trial_definition
                ON alpha_research_trials(campaign_id, trial_definition_hash);
            CREATE TABLE IF NOT EXISTS alpha_research_trial_jobs (
                job_id TEXT PRIMARY KEY REFERENCES jobs(id),
                trial_id TEXT NOT NULL UNIQUE REFERENCES alpha_research_trials(trial_id),
                campaign_id TEXT NOT NULL REFERENCES alpha_research_campaigns(campaign_id),
                job_config_hash TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS alpha_research_trial_job_trial
                ON alpha_research_trial_jobs(trial_id, created_at);
            CREATE TABLE IF NOT EXISTS alpha_research_events (
                event_id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL REFERENCES alpha_research_campaigns(campaign_id),
                event_sequence INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                protocol_hash TEXT,
                trial_id TEXT,
                job_id TEXT,
                attempt INTEGER,
                request_hash TEXT,
                result_hash TEXT,
                reason_code TEXT,
                detail TEXT NOT NULL,
                previous_event_hash TEXT,
                event_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(campaign_id, event_sequence)
            );
            CREATE INDEX IF NOT EXISTS alpha_research_event_campaign
                ON alpha_research_events(campaign_id, event_sequence);
            CREATE INDEX IF NOT EXISTS alpha_research_event_trial
                ON alpha_research_events(trial_id, event_sequence);
            CREATE TABLE IF NOT EXISTS alpha_research_ledger_snapshots (
                candidate_ledger_hash TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL REFERENCES alpha_research_campaigns(campaign_id),
                protocol_hash TEXT NOT NULL REFERENCES alpha_research_protocols(protocol_hash),
                ledger_watermark INTEGER NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS alpha_research_snapshot_campaign
                ON alpha_research_ledger_snapshots(campaign_id, protocol_hash, ledger_watermark);
            CREATE TABLE IF NOT EXISTS alpha_research_selections (
                selection_id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL REFERENCES alpha_research_campaigns(campaign_id),
                protocol_hash TEXT NOT NULL REFERENCES alpha_research_protocols(protocol_hash),
                candidate_ledger_hash TEXT NOT NULL REFERENCES alpha_research_ledger_snapshots(candidate_ledger_hash),
                selection_hash TEXT NOT NULL UNIQUE,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS alpha_research_selection_snapshot
                ON alpha_research_selections(candidate_ledger_hash);
            CREATE TABLE IF NOT EXISTS alpha_research_exposures (
                trial_id TEXT NOT NULL REFERENCES alpha_research_trials(trial_id),
                job_id TEXT NOT NULL,
                result_hash TEXT NOT NULL,
                section TEXT NOT NULL,
                first_accessed_at TEXT NOT NULL,
                last_accessed_at TEXT NOT NULL,
                access_count INTEGER NOT NULL,
                first_access_event_sequence INTEGER NOT NULL DEFAULT 0,
                last_access_event_sequence INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(trial_id, job_id, result_hash, section)
            );
            CREATE INDEX IF NOT EXISTS alpha_research_exposure_trial
                ON alpha_research_exposures(trial_id, last_accessed_at);
        ''')
        exposure_columns = {
            row[1] for row in self.connection.execute("PRAGMA table_info(alpha_research_exposures)").fetchall()
        }
        if "first_access_event_sequence" not in exposure_columns:
            self.connection.execute(
                "ALTER TABLE alpha_research_exposures ADD COLUMN first_access_event_sequence INTEGER NOT NULL DEFAULT 0"
            )
        if "last_access_event_sequence" not in exposure_columns:
            self.connection.execute(
                "ALTER TABLE alpha_research_exposures ADD COLUMN last_access_event_sequence INTEGER NOT NULL DEFAULT 0"
            )

    # ------------------------------------------------------------------
    # Lookup and integrity
    # ------------------------------------------------------------------
    def get_campaign(self, campaign_id: str) -> ResearchCampaignRecord:
        row = self.connection.execute(
            "SELECT definition,created_at,campaign_status FROM alpha_research_campaigns WHERE campaign_id=?",
            (campaign_id,),
        ).fetchone()
        if row is None:
            raise AlphaError(AlphaErrorCode.RESEARCH_CAMPAIGN_NOT_FOUND, "Research Campaign does not exist", {"campaign_id": campaign_id})
        definition = ResearchCampaignDefinition.model_validate(_load(row[0]))
        identity = digest(definition.model_dump(mode="json"))
        if identity != campaign_id:
            raise AlphaError(AlphaErrorCode.RESEARCH_LEDGER_INTEGRITY_FAILED, "Research Campaign identity mismatch", {"campaign_id": campaign_id})
        return ResearchCampaignRecord(
            campaign_id=campaign_id,
            campaign_identity_hash=identity,
            definition=definition,
            created_at=_parse_time(row[1]),
            campaign_status=row[2],
        )

    def get_protocol(self, protocol_hash: str) -> ResearchProtocolRecord:
        row = self.connection.execute(
            "SELECT campaign_id,definition,modification_ledger_watermark,exposure_context,created_at "
            "FROM alpha_research_protocols WHERE protocol_hash=?",
            (protocol_hash,),
        ).fetchone()
        if row is None:
            raise AlphaError(AlphaErrorCode.RESEARCH_PROTOCOL_NOT_REGISTERED, "Research Protocol does not exist", {"protocol_hash": protocol_hash})
        definition = ResearchProtocolDefinition.model_validate(_load(row[1]))
        if protocol_hash_for(definition.model_dump(mode="json")) != protocol_hash:
            raise AlphaError(AlphaErrorCode.RESEARCH_LEDGER_INTEGRITY_FAILED, "Research Protocol hash mismatch", {"protocol_hash": protocol_hash})
        hypotheses = [
            self._hypothesis_from_row(item)
            for item in self.connection.execute(
                "SELECT hypothesis_id,campaign_id,protocol_hash,definition,hypothesis_definition_hash,status,created_at "
                "FROM alpha_research_hypotheses WHERE protocol_hash=? ORDER BY created_at,hypothesis_id",
                (protocol_hash,),
            ).fetchall()
        ]
        return ResearchProtocolRecord(
            definition=definition,
            protocol_hash=protocol_hash,
            modification_ledger_watermark=row[2],
            exposure_context=_load(row[3]),
            created_at=_parse_time(row[4]),
            registered_hypotheses=hypotheses,
        )

    def get_trial(self, trial_id: str) -> ResearchTrialRecord:
        row = self.connection.execute(
            "SELECT campaign_id,trial_id,trial_definition_hash,canonical_request_hash,canonical_request,"
            "evidence_scope_hash,protocol_hash,hypothesis_id,parent_trial_id,trial_purpose,"
            "preregistration_status,registered_at,registered_ledger_watermark,status "
            "FROM alpha_research_trials WHERE trial_id=?",
            (trial_id,),
        ).fetchone()
        if row is None:
            raise AlphaError(AlphaErrorCode.RESEARCH_TRIAL_NOT_REGISTERED, "Research Trial does not exist", {"trial_id": trial_id})
        return ResearchTrialRecord(
            campaign_id=row[0], trial_id=row[1], trial_definition_hash=row[2],
            canonical_request_hash=row[3], canonical_request=_load(row[4]),
            evidence_scope_hash=row[5], protocol_hash=row[6], hypothesis_id=row[7],
            parent_trial_id=row[8], trial_purpose=row[9], preregistration_status=row[10],
            registered_at=_parse_time(row[11]), registered_ledger_watermark=row[12], status=row[13],
        )

    def get_trial_job(self, trial_id: str) -> ResearchTrialJobRecord | None:
        row = self.connection.execute(
            "SELECT campaign_id,trial_id,job_id,job_config_hash,request_hash,created_at "
            "FROM alpha_research_trial_jobs WHERE trial_id=?", (trial_id,),
        ).fetchone()
        return None if row is None else ResearchTrialJobRecord(
            campaign_id=row[0], trial_id=row[1], job_id=row[2], job_config_hash=row[3],
            request_hash=row[4], created_at=_parse_time(row[5]),
        )

    def trial_for_job(self, job_id: str) -> ResearchTrialRecord | None:
        row = self.connection.execute(
            "SELECT trial_id FROM alpha_research_trial_jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        return None if row is None else self.get_trial(row[0])

    def _hypothesis_from_row(self, row) -> HypothesisRecord:
        return HypothesisRecord(
            hypothesis_id=row[0], campaign_id=row[1], protocol_hash=row[2],
            definition=_load(row[3]), hypothesis_definition_hash=row[4], status=row[5],
            created_at=_parse_time(row[6]),
        )

    def _event_from_row(self, row) -> ResearchLedgerEvent:
        return ResearchLedgerEvent(
            event_id=row[0], event_sequence=row[1], event_type=row[2], campaign_id=row[3],
            protocol_hash=row[4], trial_id=row[5], job_id=row[6], attempt=row[7],
            request_hash=row[8], result_hash=row[9], reason_code=row[10], detail=_load(row[11]),
            previous_event_hash=row[12], event_hash=row[13], created_at=_parse_time(row[14]),
        )

    def list_events(self, campaign_id: str, *, limit: int | None = None) -> list[ResearchLedgerEvent]:
        sql = (
            "SELECT event_id,event_sequence,event_type,campaign_id,protocol_hash,trial_id,job_id,attempt,"
            "request_hash,result_hash,reason_code,detail,previous_event_hash,event_hash,created_at "
            "FROM alpha_research_events WHERE campaign_id=? ORDER BY event_sequence"
        )
        args: tuple[Any, ...] = (campaign_id,)
        if limit is not None:
            sql += " LIMIT ?"
            args += (limit,)
        return [self._event_from_row(row) for row in self.connection.execute(sql, args).fetchall()]

    def ledger_watermark(self, campaign_id: str) -> int:
        row = self.connection.execute(
            "SELECT coalesce(max(event_sequence),0) FROM alpha_research_events WHERE campaign_id=?",
            (campaign_id,),
        ).fetchone()
        return int(row[0])

    def verify_ledger(self, campaign_id: str, *, watermark: int | None = None) -> ResearchLedgerIntegrity:
        events = [
            event for event in self.list_events(campaign_id)
            if watermark is None or event.event_sequence <= watermark
        ]
        previous: str | None = None
        expected_sequence = 1
        for event in events:
            payload = event.model_dump(mode="json", exclude={"event_id", "event_hash"})
            if event.event_sequence != expected_sequence:
                return ResearchLedgerIntegrity(
                    status="FAILED", campaign_id=campaign_id, ledger_watermark=events[-1].event_sequence,
                    event_root_hash=events[-1].event_hash,
                    failure_reason=f"event sequence discontinuity at {event.event_sequence}; expected {expected_sequence}",
                )
            if event.previous_event_hash != previous:
                return ResearchLedgerIntegrity(
                    status="FAILED", campaign_id=campaign_id, ledger_watermark=events[-1].event_sequence,
                    event_root_hash=events[-1].event_hash, failure_reason="previous event hash mismatch",
                )
            if event_hash_for(payload) != event.event_hash:
                return ResearchLedgerIntegrity(
                    status="FAILED", campaign_id=campaign_id, ledger_watermark=events[-1].event_sequence,
                    event_root_hash=events[-1].event_hash, failure_reason=f"event hash mismatch at {event.event_sequence}",
                )
            if event.event_type not in CAMPAIGN_EVENT_TYPES:
                return ResearchLedgerIntegrity(
                    status="FAILED", campaign_id=campaign_id, ledger_watermark=events[-1].event_sequence,
                    event_root_hash=events[-1].event_hash, failure_reason=f"unknown event type {event.event_type}",
                )
            previous = event.event_hash
            expected_sequence += 1
        watermark = events[-1].event_sequence if events else 0
        return ResearchLedgerIntegrity(
            status="VERIFIED", campaign_id=campaign_id, ledger_watermark=watermark,
            event_root_hash=previous,
        )

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------
    def register_campaign(self, raw: dict[str, Any]) -> ResearchCampaignRecord:
        if "campaign_id" in raw or "campaign_identity_hash" in raw:
            raise ValueError("campaign_id and campaign_identity_hash are server-generated")
        definition = ResearchCampaignDefinition.model_validate(raw)
        identity_hash, campaign_id = campaign_identity(definition.model_dump(mode="json"))
        created_at = _utcnow()
        with _transaction(self.connection):
            existing = self.connection.execute(
                "SELECT campaign_id FROM alpha_research_campaigns WHERE campaign_id=? OR campaign_identity_hash=?",
                (campaign_id, identity_hash),
            ).fetchone()
            if existing:
                return self.get_campaign(existing[0])
            self.connection.execute(
                "INSERT INTO alpha_research_campaigns(campaign_id,campaign_identity_hash,definition,created_at,campaign_status) "
                "VALUES (?,?,?,?,?)",
                (campaign_id, identity_hash, _json(definition.model_dump(mode="json")),
                 created_at.isoformat().replace("+00:00", "Z"), "CAMPAIGN_OPEN"),
            )
            self._append_event(
                campaign_id=campaign_id, event_type="CAMPAIGN_REGISTERED",
                detail={"campaign_identity_hash": identity_hash, "campaign_name": definition.campaign_name},
            )
        return self.get_campaign(campaign_id)

    def register_protocol(self, raw: dict[str, Any]) -> ResearchProtocolRecord:
        if "protocol_hash" in raw:
            raise ValueError("protocol_hash is server-generated")
        definition = ResearchProtocolDefinition.model_validate(raw)
        if definition.selection_policy.primary_metric != "rank_ic":
            raise ValueError("PR5 candidate selection currently supports primary_metric=rank_ic only")
        self._validate_protocol_policy_surface(definition)
        campaign = self.get_campaign(definition.campaign_id)
        campaign_budget = campaign.definition.resource_policy
        budget_updates = {}
        if "resource_policy" not in raw:
            budget_updates["resource_policy"] = campaign_budget
        if "trial_budget" not in raw:
            budget_updates["trial_budget"] = budget_updates.get("resource_policy", campaign_budget)
        if budget_updates:
            definition = definition.model_copy(update=budget_updates)
        self._validate_protocol_budget(definition, campaign_budget)
        protocol_hash = protocol_hash_for(definition.model_dump(mode="json"))
        with _transaction(self.connection):
            existing = self.connection.execute(
                "SELECT campaign_id FROM alpha_research_protocols WHERE protocol_hash=?", (protocol_hash,)
            ).fetchone()
            if existing is not None:
                return self.get_protocol(protocol_hash)
            protocol_count = self.connection.execute(
                "SELECT count(*) FROM alpha_research_protocols WHERE campaign_id=?", (definition.campaign_id,)
            ).fetchone()[0]
            if protocol_count >= campaign_budget.max_protocol_revisions:
                raise AlphaError(
                    AlphaErrorCode.RESEARCH_PROTOCOL_BUDGET_EXCEEDED,
                    "Research Protocol revision budget exceeded",
                    {"campaign_id": definition.campaign_id, "limit": campaign_budget.max_protocol_revisions},
                )
            if protocol_count and definition.parent_protocol_hash is None:
                raise AlphaError(
                    AlphaErrorCode.RESEARCH_PROTOCOL_MISMATCH,
                    "A new Protocol revision must identify its parent",
                    {"campaign_id": definition.campaign_id},
                )
            if definition.parent_protocol_hash is not None:
                parent = self.get_protocol(definition.parent_protocol_hash)
                if parent.definition.campaign_id != definition.campaign_id:
                    raise AlphaError(AlphaErrorCode.RESEARCH_PROTOCOL_MISMATCH, "Parent Protocol belongs to another Campaign", {})
                if definition.protocol_revision != parent.definition.protocol_revision + 1:
                    raise AlphaError(AlphaErrorCode.RESEARCH_PROTOCOL_MISMATCH, "Protocol revision must increment its parent by one", {})
                if definition.campaign_id != parent.definition.campaign_id:
                    raise AlphaError(AlphaErrorCode.RESEARCH_PROTOCOL_MISMATCH, "Protocol Campaign mismatch", {})
            exposure_count = self._result_access_count(definition.campaign_id)
            watermark = self.ledger_watermark(definition.campaign_id)
            exposure_context = {
                "result_access_count": exposure_count,
                "latest_ledger_watermark": watermark,
                "prior_result_exposure_observed": exposure_count > 0,
            }
            created_at = _utcnow()
            self.connection.execute(
                "INSERT INTO alpha_research_protocols(protocol_hash,campaign_id,definition,modification_ledger_watermark,"
                "exposure_context,created_at) VALUES (?,?,?,?,?,?)",
                (protocol_hash, definition.campaign_id, _json(definition.model_dump(mode="json")),
                 watermark, _json(exposure_context), created_at.isoformat().replace("+00:00", "Z")),
            )
            for hypothesis in sorted(definition.hypothesis_definitions, key=lambda value: value.hypothesis_key):
                definition_json = hypothesis.model_dump(mode="json")
                hypothesis_hash = hypothesis_hash_for(protocol_hash, definition_json)
                hypothesis_id = canonical_digest({
                    "campaign_id": definition.campaign_id,
                    "protocol_hash": protocol_hash,
                    "hypothesis_definition_hash": hypothesis_hash,
                })
                self.connection.execute(
                    "INSERT INTO alpha_research_hypotheses(hypothesis_id,campaign_id,protocol_hash,definition,"
                    "hypothesis_definition_hash,status,created_at) VALUES (?,?,?,?,?,?,?)",
                    (hypothesis_id, definition.campaign_id, protocol_hash, _json(definition_json),
                     hypothesis_hash, "REGISTERED", created_at.isoformat().replace("+00:00", "Z")),
                )
                self._append_event(
                    campaign_id=definition.campaign_id, protocol_hash=protocol_hash,
                    event_type="HYPOTHESIS_REGISTERED",
                    detail={"hypothesis_id": hypothesis_id, "hypothesis_key": hypothesis.hypothesis_key},
                )
            self._append_event(
                campaign_id=definition.campaign_id, protocol_hash=protocol_hash,
                event_type="PROTOCOL_REGISTERED" if definition.protocol_revision == 1 else "PROTOCOL_REVISED",
                detail={
                    "protocol_revision": definition.protocol_revision,
                    "parent_protocol_hash": definition.parent_protocol_hash,
                    "modification_reason": definition.modification_reason,
                    "exposure_context": exposure_context,
                },
            )
        return self.get_protocol(protocol_hash)

    @staticmethod
    def _validate_protocol_policy_surface(definition: ResearchProtocolDefinition) -> None:
        evaluation_keys = {
            "primary_metric", "minimum_rank_ic", "minimum_valid_folds", "minimum_valid_ic_sessions",
            "minimum_label_coverage", "minimum_model_execution_coverage", "minimum_end_to_end_coverage",
            "allowed_research_status", "stability_constraints", "requires_walk_forward", "walk_forward_mode",
        }
        stopping_keys = {
            "max_seconds", "max_total_training_samples", "max_folds", "max_bootstrap_replicates",
        }
        unsupported_evaluation = sorted(set(definition.evaluation_policy) - evaluation_keys)
        unsupported_stopping = sorted(set(definition.stopping_policy) - stopping_keys)
        if unsupported_evaluation or unsupported_stopping:
            raise ValueError(
                "Unsupported Protocol evaluation/stopping constraints: "
                + ", ".join(unsupported_evaluation + unsupported_stopping)
            )
        policy = definition.selection_policy.model_dump(mode="json")
        for key in evaluation_keys - {"requires_walk_forward", "walk_forward_mode"}:
            if key in definition.evaluation_policy and definition.evaluation_policy[key] != policy.get(key):
                raise ValueError(f"evaluation_policy.{key} must match selection_policy")
        if "requires_walk_forward" in definition.evaluation_policy and type(definition.evaluation_policy["requires_walk_forward"]) is not bool:
            raise ValueError("evaluation_policy.requires_walk_forward must be boolean")
        if "walk_forward_mode" in definition.evaluation_policy and definition.evaluation_policy["walk_forward_mode"] not in {"expanding", "rolling"}:
            raise ValueError("evaluation_policy.walk_forward_mode must be expanding or rolling")
        for key in stopping_keys:
            if key in definition.stopping_policy and (
                type(definition.stopping_policy[key]) is not int or definition.stopping_policy[key] < 1
            ):
                raise ValueError(f"stopping_policy.{key} must be a positive integer")

    def _validate_protocol_budget(self, definition: ResearchProtocolDefinition, campaign_budget) -> None:
        for field, campaign_limit in campaign_budget.hard_limits().items():
            protocol_limit = getattr(definition.trial_budget, field)
            if protocol_limit > campaign_limit:
                raise AlphaError(
                    AlphaErrorCode.RESEARCH_PROTOCOL_BUDGET_EXCEEDED,
                    "Protocol budget cannot exceed Campaign budget",
                    {"field": field, "campaign_limit": campaign_limit, "protocol_limit": protocol_limit},
                )
        if definition.resource_policy != definition.trial_budget:
            raise ValueError("resource_policy and trial_budget must match")

    def register_trial(
        self,
        *,
        campaign_id: str,
        protocol_hash: str,
        hypothesis_id: str,
        request: dict[str, Any],
        trial_purpose: TrialPurpose,
        parent_trial_id: str | None = None,
    ) -> ResearchTrialRecord:
        from ..jobs.requests import ALPHA_REQUEST_ADAPTER, TrainAlphaModelJob
        from ..models.walk_forward import WalkForwardAlphaRequest
        from ..research_admission import (
            require_training_execution,
            require_walk_forward_execution,
        )

        campaign = self.get_campaign(campaign_id)
        parsed = ALPHA_REQUEST_ADAPTER.validate_python(request)
        if not isinstance(parsed, (TrainAlphaModelJob, WalkForwardAlphaRequest)):
            raise TypeError("Managed Research trials currently support training and walk-forward Jobs only")
        if parsed.research_protocol_hash != protocol_hash:
            raise AlphaError(
                AlphaErrorCode.RESEARCH_PROTOCOL_MISMATCH,
                "Trial and request Protocol differ",
                {
                    "trial_protocol_hash": protocol_hash,
                    "request_protocol_hash": parsed.research_protocol_hash,
                },
            )
        protocol = self.get_protocol(protocol_hash)
        if protocol.definition.campaign_id != campaign_id:
            raise AlphaError(AlphaErrorCode.RESEARCH_PROTOCOL_MISMATCH, "Protocol belongs to another Campaign", {})
        if parsed.kind not in protocol.definition.allowed_job_kinds:
            raise AlphaError(AlphaErrorCode.RESEARCH_PROTOCOL_MISMATCH, "Job kind is not allowed by the Protocol", {"kind": parsed.kind})
        if isinstance(parsed, TrainAlphaModelJob):
            require_training_execution(parsed)
        else:
            require_walk_forward_execution(parsed)
        hypotheses = {row.hypothesis_id: row for row in protocol.registered_hypotheses}
        if hypothesis_id not in hypotheses:
            raise AlphaError(AlphaErrorCode.RESEARCH_PROTOCOL_MISMATCH, "Hypothesis does not belong to the Protocol", {"hypothesis_id": hypothesis_id})
        request_payload = parsed.model_dump(mode="json")
        if request_payload.get("research_trial_id"):
            raise ValueError("A Trial registration request must not bind an existing research_trial_id")
        self._validate_protocol_constraints(protocol.definition, request_payload, hypotheses[hypothesis_id].definition)
        canonical_request, request_hash, definition_hash, evidence_scope_hash = trial_identities(request_payload)
        parent = None if parent_trial_id is None else self.get_trial(parent_trial_id)
        if parent is not None and parent.campaign_id != campaign_id:
            raise AlphaError(AlphaErrorCode.RESEARCH_PROTOCOL_MISMATCH, "Parent Trial belongs to another Campaign", {})
        prior_exposure = self._result_access_count(campaign_id)
        if trial_purpose == "POST_HOC_EXPLORATION":
            preregistration = "POST_HOC_EXPLORATORY"
        elif prior_exposure:
            preregistration = "REGISTERED_AFTER_PRIOR_EXPOSURE"
        else:
            preregistration = "PREREGISTERED_WITHIN_MANAGED_CAMPAIGN"
        trial_id = "sha256:" + __import__("hashlib").sha256(
            f"{campaign_id}:{protocol_hash}:{request_hash}:{trial_purpose}".encode()
            + _utcnow().isoformat().encode()
        ).hexdigest()
        with _transaction(self.connection):
            self._check_registration_budget(campaign, protocol, definition_hash)
            watermark = self.ledger_watermark(campaign_id)
            created_at = _utcnow()
            self.connection.execute(
                "INSERT INTO alpha_research_trials(trial_id,campaign_id,protocol_hash,hypothesis_id,trial_definition_hash,"
                "canonical_request_hash,canonical_request,evidence_scope_hash,parent_trial_id,trial_purpose,"
                "preregistration_status,registered_at,registered_ledger_watermark,status) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (trial_id, campaign_id, protocol_hash, hypothesis_id, definition_hash, request_hash,
                 _json(canonical_request), evidence_scope_hash, parent_trial_id, trial_purpose,
                 preregistration, created_at.isoformat().replace("+00:00", "Z"), watermark, "REGISTERED"),
            )
            self._append_event(
                campaign_id=campaign_id, protocol_hash=protocol_hash, trial_id=trial_id,
                request_hash=request_hash, event_type="TRIAL_REGISTERED",
                detail={
                    "trial_definition_hash": definition_hash,
                    "evidence_scope_hash": evidence_scope_hash,
                    "trial_purpose": trial_purpose,
                    "preregistration_status": preregistration,
                    "parent_trial_id": parent_trial_id,
                },
            )
        return self.get_trial(trial_id)

    @staticmethod
    def _contains(pattern: Any, actual: Any) -> bool:
        if isinstance(pattern, dict):
            return isinstance(actual, dict) and all(
                key in actual and ResearchExperimentRegistry._contains(value, actual[key])
                for key, value in pattern.items()
            )
        if isinstance(pattern, list):
            return isinstance(actual, list) and len(pattern) == len(actual) and all(
                ResearchExperimentRegistry._contains(left, right) for left, right in zip(pattern, actual, strict=True)
            )
        return pattern == actual

    def _validate_protocol_constraints(
        self, protocol: ResearchProtocolDefinition, request: dict[str, Any],
        hypothesis: ResearchHypothesisDefinition,
    ) -> None:
        normalized = normalized_research_request(request)
        label = normalized["label_spec"]
        constraints = protocol.data_constraints
        for key, value in (("stock_dataset_id", normalized["stock_dataset_id"]), ("feature_set_id", normalized["feature_set_id"])):
            allowed = constraints.get(f"{key}s") or constraints.get(key)
            if isinstance(allowed, str):
                allowed = [allowed]
            if isinstance(allowed, list) and value not in allowed:
                raise AlphaError(AlphaErrorCode.RESEARCH_PROTOCOL_MISMATCH, "Trial data identity is outside the Protocol", {"field": key})
        labels = protocol.label_specifications
        if labels and not any(self._contains(item, label) for item in labels):
            raise AlphaError(AlphaErrorCode.RESEARCH_PROTOCOL_MISMATCH, "Trial label definition is outside the Protocol", {"field": "label"})
        window = normalized["development_window"]
        if protocol.development_windows and not any(self._contains(item, window) for item in protocol.development_windows):
            raise AlphaError(AlphaErrorCode.RESEARCH_PROTOCOL_MISMATCH, "Trial development window is outside the Protocol", {"field": "development_window"})
        if protocol.factor_specifications and not any(
            self._contains(item, normalized["factor_spec"]) for item in protocol.factor_specifications
        ):
            raise AlphaError(AlphaErrorCode.RESEARCH_PROTOCOL_MISMATCH, "Trial FactorSpec is outside the Protocol", {"field": "factor_spec"})
        if hypothesis.factor_spec_hash is not None and normalized["factor_spec_hash"] != hypothesis.factor_spec_hash:
            raise AlphaError(AlphaErrorCode.RESEARCH_PROTOCOL_MISMATCH, "Trial FactorSpec hash differs from the Hypothesis", {
                "expected": hypothesis.factor_spec_hash, "actual": normalized["factor_spec_hash"],
            })
        if protocol.candidate_definitions and not any(
            self._contains(item, normalized["candidate"]) for item in protocol.candidate_definitions
        ):
            raise AlphaError(AlphaErrorCode.RESEARCH_PROTOCOL_MISMATCH, "Trial candidate is outside Protocol candidate_definitions", {"field": "candidate_definitions"})
        if hypothesis.candidate_constraints and not self._contains(hypothesis.candidate_constraints, normalized["candidate"]):
            raise AlphaError(AlphaErrorCode.RESEARCH_PROTOCOL_MISMATCH, "Trial candidate is outside Hypothesis candidate_constraints", {"field": "candidate_constraints"})
        model_config = normalized["candidate"]["hyperparameters"]
        hyper = protocol.hyperparameter_constraints
        if hyper:
            fixed = hyper.get("fixed")
            if isinstance(fixed, dict) and not self._contains(fixed, model_config):
                raise AlphaError(AlphaErrorCode.RESEARCH_PROTOCOL_MISMATCH, "Trial hyperparameters differ from fixed Protocol values", {"field": "hyperparameters"})
            allowed_configs = hyper.get("allowed")
            if isinstance(allowed_configs, list) and not any(self._contains(item, model_config) for item in allowed_configs):
                raise AlphaError(AlphaErrorCode.RESEARCH_PROTOCOL_MISMATCH, "Trial hyperparameters are outside the Protocol", {"field": "hyperparameters"})
            for field, bounds in (hyper.get("ranges") or {}).items():
                if not isinstance(bounds, dict) or field not in model_config:
                    raise AlphaError(AlphaErrorCode.RESEARCH_PROTOCOL_MISMATCH, "Required constrained hyperparameter is missing", {"field": field})
                value = model_config[field]
                if not isinstance(value, (int, float)) or "min" in bounds and value < bounds["min"] or "max" in bounds and value > bounds["max"]:
                    raise AlphaError(AlphaErrorCode.RESEARCH_PROTOCOL_MISMATCH, "Trial hyperparameter is outside the Protocol range", {"field": field})
        seed_policy = protocol.seed_policy
        if seed_policy:
            allowed_seeds = seed_policy.get("allowed_seeds")
            fixed_seed = seed_policy.get("fixed_seed")
            request_seed = normalized["candidate"]["random_seed"]
            if fixed_seed is not None and request_seed != fixed_seed:
                raise AlphaError(AlphaErrorCode.RESEARCH_PROTOCOL_MISMATCH, "Trial seed differs from the Protocol", {"field": "random_seed"})
            if isinstance(allowed_seeds, list) and request_seed not in allowed_seeds:
                raise AlphaError(AlphaErrorCode.RESEARCH_PROTOCOL_MISMATCH, "Trial seed is outside the Protocol", {"field": "random_seed"})
        if protocol.inference_policy:
            inference = (normalized["candidate"].get("walk_forward_config") or {}).get("inference") or {}
            if not self._contains(protocol.inference_policy, inference):
                raise AlphaError(AlphaErrorCode.RESEARCH_PROTOCOL_MISMATCH, "Walk-forward inference differs from the Protocol", {"field": "walk_forward_config.inference"})
        evaluation = protocol.evaluation_policy
        declared_minimum_rank_ic = evaluation.get("minimum_rank_ic")
        if declared_minimum_rank_ic is not None and request.get("min_rank_ic") is not None and request.get("min_rank_ic") < declared_minimum_rank_ic:
            raise AlphaError(AlphaErrorCode.RESEARCH_PROTOCOL_MISMATCH, "Trial minimum RankIC differs from the Protocol", {"field": "min_rank_ic"})
        stopping = protocol.stopping_policy
        if "max_seconds" in stopping:
            if request.get("kind") == "train_alpha_model":
                effective_seconds = (
                    (request.get("resource_budget") or {}).get("max_seconds")
                    if (request.get("resource_budget") or {}).get("max_seconds") is not None
                    else request.get("max_seconds", TRAINING_DEFAULT_SECONDS)
                )
            else:
                effective_seconds = request.get("max_seconds")
            if effective_seconds is None or effective_seconds > stopping["max_seconds"]:
                raise AlphaError(AlphaErrorCode.RESEARCH_PROTOCOL_MISMATCH, "Trial max_seconds exceeds the Protocol", {"field": "resource_budget.max_seconds"})
        budget = request.get("resource_budget") or {}
        if "max_total_training_samples" in stopping and budget.get("max_total_training_samples", request.get("resource_budget", {}).get("max_rows")) is not None and budget.get("max_total_training_samples", request.get("resource_budget", {}).get("max_rows")) > stopping["max_total_training_samples"]:
            raise AlphaError(AlphaErrorCode.RESEARCH_PROTOCOL_MISMATCH, "Trial training-sample budget exceeds the Protocol", {"field": "resource_budget.max_total_training_samples"})
        if request.get("kind") == "walk_forward_alpha":
            walk = request.get("walk_forward_config") or {}
            if "walk_forward_mode" in evaluation and walk.get("mode") != evaluation["walk_forward_mode"]:
                raise AlphaError(AlphaErrorCode.RESEARCH_PROTOCOL_MISMATCH, "Trial walk-forward mode differs from the Protocol", {"field": "walk_forward_config.mode"})
            if "max_folds" in stopping and walk.get("max_folds", 12) > stopping["max_folds"]:
                raise AlphaError(AlphaErrorCode.RESEARCH_PROTOCOL_MISMATCH, "Trial max_folds exceeds the Protocol", {"field": "walk_forward_config.max_folds"})
            inference = walk.get("inference") or {}
            if "max_bootstrap_replicates" in stopping and inference.get("bootstrap_replicates", 1000) > stopping["max_bootstrap_replicates"]:
                raise AlphaError(AlphaErrorCode.RESEARCH_PROTOCOL_MISMATCH, "Trial bootstrap budget exceeds the Protocol", {"field": "walk_forward_config.inference.bootstrap_replicates"})

    def _check_registration_budget(
        self, campaign: ResearchCampaignRecord, protocol: ResearchProtocolRecord, definition_hash: str,
    ) -> None:
        checks = (
            ("campaign", campaign.definition.resource_policy),
            ("protocol", protocol.definition.trial_budget),
        )
        for scope, budget in checks:
            where = "campaign_id=?" if scope == "campaign" else "campaign_id=? AND protocol_hash=?"
            args = (campaign.campaign_id,) if scope == "campaign" else (campaign.campaign_id, protocol.protocol_hash)
            registered = self.connection.execute(
                f"SELECT count(*) FROM alpha_research_trials WHERE {where}", args,
            ).fetchone()[0]
            if registered >= budget.max_registered_trials:
                raise AlphaError(AlphaErrorCode.RESEARCH_TRIAL_BUDGET_EXCEEDED, f"{scope} Trial registration budget exceeded", {
                    "scope": scope, "limit": budget.max_registered_trials,
                })
            distinct = self.connection.execute(
                f"SELECT count(DISTINCT trial_definition_hash) FROM alpha_research_trials WHERE {where}", args,
            ).fetchone()[0]
            exists = self.connection.execute(
                f"SELECT 1 FROM alpha_research_trials WHERE {where} AND trial_definition_hash=? LIMIT 1",
                (*args, definition_hash),
            ).fetchone()
            if exists is None and distinct >= budget.max_distinct_candidate_definitions:
                raise AlphaError(AlphaErrorCode.RESEARCH_TRIAL_BUDGET_EXCEEDED, f"{scope} distinct candidate budget exceeded", {
                    "scope": scope, "limit": budget.max_distinct_candidate_definitions,
                })

    # ------------------------------------------------------------------
    # Managed Job binding and lifecycle
    # ------------------------------------------------------------------
    def validate_managed_submission(self, request: dict[str, Any], *, job_id: str, job_config_hash: str) -> ResearchTrialRecord | None:
        protocol_hash = request.get("research_protocol_hash")
        trial_id = request.get("research_trial_id")
        registered = bool(protocol_hash) and self.connection.execute(
            "SELECT 1 FROM alpha_research_protocols WHERE protocol_hash=?", (protocol_hash,)
        ).fetchone() is not None
        if not registered:
            if trial_id:
                raise AlphaError(AlphaErrorCode.RESEARCH_PROTOCOL_NOT_REGISTERED, "Trial references an unregistered Protocol", {"trial_id": trial_id})
            return None
        if not trial_id:
            raise AlphaError(AlphaErrorCode.RESEARCH_TRIAL_NOT_REGISTERED, "Registered Protocol requires research_trial_id", {"protocol_hash": protocol_hash})
        trial = self.get_trial(trial_id)
        if trial.protocol_hash != protocol_hash or trial.campaign_id == "":
            raise AlphaError(AlphaErrorCode.RESEARCH_PROTOCOL_MISMATCH, "Trial and request Protocol differ", {"trial_id": trial_id})
        _, request_hash, _, _ = trial_identities(request)
        if request_hash != trial.canonical_request_hash:
            raise AlphaError(AlphaErrorCode.RESEARCH_TRIAL_REQUEST_MISMATCH, "Submitted request differs from registered Trial", {"trial_id": trial_id})
        existing = self.get_trial_job(trial_id)
        if existing is not None:
            if existing.job_id != job_id:
                raise AlphaError(AlphaErrorCode.RESEARCH_TRIAL_REQUEST_MISMATCH, "Trial already belongs to another Job", {"trial_id": trial_id, "job_id": existing.job_id})
            return trial
        if trial.status not in {"REGISTERED"}:
            raise AlphaError(AlphaErrorCode.RESEARCH_TRIAL_REQUEST_MISMATCH, "Trial is not available for submission", {"trial_id": trial_id, "status": trial.status})
        campaign = self.get_campaign(trial.campaign_id)
        protocol = self.get_protocol(trial.protocol_hash)
        hypothesis = next(
            (row.definition for row in protocol.registered_hypotheses if row.hypothesis_id == trial.hypothesis_id),
            None,
        )
        if hypothesis is None:
            raise AlphaError(AlphaErrorCode.RESEARCH_PROTOCOL_MISMATCH, "Trial Hypothesis is not registered", {"trial_id": trial_id})
        self._validate_protocol_constraints(protocol.definition, request, hypothesis)
        for scope, budget in (
            ("campaign", campaign.definition.resource_policy),
            ("protocol", protocol.definition.trial_budget),
        ):
            if scope == "campaign":
                submitted = self.connection.execute(
                    "SELECT count(*) FROM alpha_research_trial_jobs WHERE campaign_id=?", (trial.campaign_id,)
                ).fetchone()[0]
            else:
                submitted = self.connection.execute(
                    "SELECT count(*) FROM alpha_research_trial_jobs WHERE campaign_id=? AND trial_id IN "
                    "(SELECT trial_id FROM alpha_research_trials WHERE protocol_hash=?)",
                    (trial.campaign_id, trial.protocol_hash),
                ).fetchone()[0]
            limit = min(budget.max_submitted_trials, budget.max_evaluation_jobs)
            if submitted >= limit:
                raise AlphaError(AlphaErrorCode.RESEARCH_TRIAL_BUDGET_EXCEEDED, f"{scope} managed submission budget exceeded", {
                    "scope": scope, "limit": limit,
                })
        now = _utcnow().isoformat().replace("+00:00", "Z")
        self.connection.execute(
            "INSERT INTO alpha_research_trial_jobs(job_id,trial_id,campaign_id,job_config_hash,request_hash,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (job_id, trial_id, trial.campaign_id, job_config_hash, request_hash, now),
        )
        self.connection.execute(
            "UPDATE alpha_research_trials SET status='SUBMITTED' WHERE trial_id=?", (trial_id,)
        )
        self._append_event(
            campaign_id=trial.campaign_id, protocol_hash=trial.protocol_hash, trial_id=trial_id,
            job_id=job_id, request_hash=request_hash, event_type="TRIAL_SUBMITTED",
            detail={"job_config_hash": job_config_hash},
        )
        return self.get_trial(trial_id)

    def verify_managed_execution(self, request: dict[str, Any], *, job_id: str) -> ResearchTrialRecord | None:
        protocol_hash = request.get("research_protocol_hash")
        if not protocol_hash or self.connection.execute(
            "SELECT 1 FROM alpha_research_protocols WHERE protocol_hash=?", (protocol_hash,),
        ).fetchone() is None:
            return None
        trial_id = request.get("research_trial_id")
        if not trial_id:
            raise AlphaError(AlphaErrorCode.RESEARCH_TRIAL_NOT_REGISTERED, "Managed execution requires research_trial_id", {
                "protocol_hash": protocol_hash, "job_id": job_id,
            })
        trial = self.get_trial(trial_id)
        binding = self.get_trial_job(trial_id)
        if binding is None or binding.job_id != job_id:
            raise AlphaError(AlphaErrorCode.RESEARCH_TRIAL_REQUEST_MISMATCH, "Trial-Job binding is missing or changed", {
                "trial_id": trial_id, "job_id": job_id,
            })
        _, request_hash, _, _ = trial_identities(request)
        if request_hash != trial.canonical_request_hash or request_hash != binding.request_hash:
            raise AlphaError(AlphaErrorCode.RESEARCH_TRIAL_REQUEST_MISMATCH, "Worker request differs from the registered Trial", {
                "trial_id": trial_id, "job_id": job_id,
            })
        if trial.protocol_hash != protocol_hash:
            raise AlphaError(AlphaErrorCode.RESEARCH_PROTOCOL_MISMATCH, "Worker Protocol differs from the Trial", {"trial_id": trial_id})
        protocol = self.get_protocol(protocol_hash)
        if request.get("kind") not in protocol.definition.allowed_job_kinds:
            raise AlphaError(AlphaErrorCode.RESEARCH_PROTOCOL_MISMATCH, "Worker Job kind is not allowed by the Protocol", {})
        hypothesis = next(
            (row.definition for row in protocol.registered_hypotheses if row.hypothesis_id == trial.hypothesis_id),
            None,
        )
        if hypothesis is None:
            raise AlphaError(AlphaErrorCode.RESEARCH_PROTOCOL_MISMATCH, "Worker Trial Hypothesis is not registered", {"trial_id": trial_id})
        self._validate_protocol_constraints(protocol.definition, request, hypothesis)
        if trial.status not in {"RUNNING", "PUBLISHING", "COMPLETED"}:
            raise AlphaError(AlphaErrorCode.RESEARCH_TRIAL_REQUEST_MISMATCH, "Managed Trial is not executable", {
                "trial_id": trial_id, "status": trial.status,
            })
        return trial

    def record_rejection(self, request: dict[str, Any], *, reason_code: str, detail: dict[str, Any] | None = None) -> None:
        trial_id = request.get("research_trial_id") if isinstance(request, dict) else None
        if not trial_id:
            return
        try:
            trial = self.get_trial(trial_id)
        except AlphaError:
            return
        with _transaction(self.connection):
            current = self.get_trial(trial.trial_id)
            preserved = current.status != "REGISTERED"
            self._append_event(
                campaign_id=trial.campaign_id, protocol_hash=trial.protocol_hash, trial_id=trial.trial_id,
                reason_code=reason_code, event_type="TRIAL_REJECTED",
                detail={
                    **(detail or {}),
                    "previous_status": current.status,
                    "status_preserved": preserved,
                },
            )
            if not preserved:
                self.connection.execute(
                    "UPDATE alpha_research_trials SET status='REJECTED' WHERE trial_id=? AND status='REGISTERED'",
                    (trial.trial_id,),
                )

    def attempt_budget_available(self, job_id: str) -> tuple[bool, dict[str, Any]]:
        trial = self.trial_for_job(job_id)
        if trial is None:
            return True, {}
        campaign = self.get_campaign(trial.campaign_id)
        protocol = self.get_protocol(trial.protocol_hash)
        campaign_used = self.connection.execute(
            "SELECT count(*) FROM alpha_research_events WHERE campaign_id=? AND event_type IN "
            "('TRIAL_EXECUTION_STARTED','TRIAL_RETRY_STARTED')",
            (trial.campaign_id,),
        ).fetchone()[0]
        protocol_used = self.connection.execute(
            "SELECT count(*) FROM alpha_research_events e JOIN alpha_research_trials tr ON tr.trial_id=e.trial_id "
            "WHERE tr.protocol_hash=? AND e.event_type IN ('TRIAL_EXECUTION_STARTED','TRIAL_RETRY_STARTED')",
            (trial.protocol_hash,),
        ).fetchone()[0]
        for scope, used, budget in (
            ("campaign", campaign_used, campaign.definition.resource_policy),
            ("protocol", protocol_used, protocol.definition.trial_budget),
        ):
            if used >= budget.max_execution_attempts:
                return False, {
                    "scope": scope, "used": used, "limit": budget.max_execution_attempts,
                    "trial_id": trial.trial_id, "job_id": job_id,
                }
        return True, {}

    def record_lifecycle(self, *, job_id: str, event_type: str, attempt: int | None = None,
                         result_hash: str | None = None, reason_code: str | None = None,
                         detail: dict[str, Any] | None = None) -> None:
        trial = self.trial_for_job(job_id)
        if trial is None:
            return
        status_by_event = {
            "TRIAL_EXECUTION_STARTED": "RUNNING",
            "TRIAL_RETRY_STARTED": "RUNNING",
            "TRIAL_PUBLISHING": "PUBLISHING",
            "TRIAL_COMPLETED": "COMPLETED",
            "TRIAL_FAILED": "FAILED",
            "TRIAL_CANCELLED": "CANCELLED",
            "TRIAL_WORKER_LOST": "FAILED",
        }
        if event_type in {"TRIAL_EXECUTION_STARTED", "TRIAL_RETRY_STARTED"}:
            available, context = self.attempt_budget_available(job_id)
            if not available:
                raise AlphaError(AlphaErrorCode.RESEARCH_TRIAL_BUDGET_EXCEEDED, "Execution attempt budget exceeded", context)
        self._append_event(
            campaign_id=trial.campaign_id, protocol_hash=trial.protocol_hash, trial_id=trial.trial_id,
            job_id=job_id, attempt=attempt, result_hash=result_hash, reason_code=reason_code,
            event_type=event_type, detail=detail or {},
        )
        if event_type in status_by_event:
            self.connection.execute(
                "UPDATE alpha_research_trials SET status=? WHERE trial_id=?",
                (status_by_event[event_type], trial.trial_id),
            )

    def record_result_access(self, *, job_id: str, section: str) -> None:
        trial = self.trial_for_job(job_id)
        if trial is None:
            return
        row = self.connection.execute("SELECT result_hash FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None or not row[0] or trial.status not in TERMINAL_TRIAL_STATES:
            return
        result_hash = row[0]
        now = _utcnow().isoformat().replace("+00:00", "Z")
        with _transaction(self.connection):
            existing = self.connection.execute(
                "SELECT access_count FROM alpha_research_exposures WHERE trial_id=? AND job_id=? AND result_hash=? AND section=?",
                (trial.trial_id, job_id, result_hash, section),
            ).fetchone()
            event = self._append_event(
                campaign_id=trial.campaign_id, protocol_hash=trial.protocol_hash, trial_id=trial.trial_id,
                job_id=job_id, result_hash=result_hash, event_type="TRIAL_RESULT_ACCESSED",
                detail={"section": section, "first_access": existing is None},
            )
            if existing is None:
                self.connection.execute(
                    "INSERT INTO alpha_research_exposures(trial_id,job_id,result_hash,section,first_accessed_at,"
                    "last_accessed_at,access_count,first_access_event_sequence,last_access_event_sequence) "
                    "VALUES (?,?,?,?,?,?,1,?,?)",
                    (trial.trial_id, job_id, result_hash, section, now, now, event.event_sequence, event.event_sequence),
                )
            else:
                self.connection.execute(
                    "UPDATE alpha_research_exposures SET last_accessed_at=?,access_count=access_count+1,"
                    "last_access_event_sequence=? WHERE trial_id=? AND job_id=? AND result_hash=? AND section=?",
                    (now, event.event_sequence, trial.trial_id, job_id, result_hash, section),
                )

    # ------------------------------------------------------------------
    # Event chain
    # ------------------------------------------------------------------
    def _append_event(self, *, campaign_id: str, event_type: str,
                      protocol_hash: str | None = None, trial_id: str | None = None,
                      job_id: str | None = None, attempt: int | None = None,
                      request_hash: str | None = None, result_hash: str | None = None,
                      reason_code: str | None = None, detail: dict[str, Any] | None = None,
                      created_at: datetime | None = None) -> ResearchLedgerEvent:
        created = created_at or _utcnow()
        row = self.connection.execute(
            "SELECT event_sequence,event_hash FROM alpha_research_events WHERE campaign_id=? ORDER BY event_sequence DESC LIMIT 1",
            (campaign_id,),
        ).fetchone()
        sequence = 1 if row is None else row[0] + 1
        previous = None if row is None else row[1]
        payload = {
            "event_sequence": sequence, "event_type": event_type, "campaign_id": campaign_id,
            "protocol_hash": protocol_hash, "trial_id": trial_id, "job_id": job_id,
            "attempt": attempt, "request_hash": request_hash, "result_hash": result_hash,
            "reason_code": reason_code, "detail": detail or {},
            "previous_event_hash": previous, "created_at": created,
        }
        event_hash = event_hash_for(payload)
        event_id = canonical_digest(payload)
        encoded_time = created.astimezone(UTC).isoformat().replace("+00:00", "Z")
        self.connection.execute(
            "INSERT INTO alpha_research_events(event_id,campaign_id,event_sequence,event_type,protocol_hash,"
            "trial_id,job_id,attempt,request_hash,result_hash,reason_code,detail,previous_event_hash,event_hash,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (event_id, campaign_id, sequence, event_type, protocol_hash, trial_id, job_id, attempt,
             request_hash, result_hash, reason_code, _json(detail or {}), previous, event_hash, encoded_time),
        )
        return ResearchLedgerEvent(
            event_id=event_id, event_sequence=sequence, event_type=event_type, campaign_id=campaign_id,
            protocol_hash=protocol_hash, trial_id=trial_id, job_id=job_id, attempt=attempt,
            request_hash=request_hash, result_hash=result_hash, reason_code=reason_code,
            detail=detail or {}, previous_event_hash=previous, event_hash=event_hash, created_at=created,
        )

    def _result_access_count(self, campaign_id: str) -> int:
        row = self.connection.execute(
            "SELECT coalesce(sum(access_count),0) FROM alpha_research_exposures WHERE trial_id IN "
            "(SELECT trial_id FROM alpha_research_trials WHERE campaign_id=?)",
            (campaign_id,),
        ).fetchone()
        return int(row[0])

    # ------------------------------------------------------------------
    # Accounting, snapshots and selection
    # ------------------------------------------------------------------
    def trial_accounting(self, campaign_id: str, *, watermark: int | None = None) -> TrialAccounting:
        self.get_campaign(campaign_id)
        if watermark is None:
            registered_rows = self.connection.execute(
                "SELECT trial_id,trial_definition_hash,hypothesis_id FROM alpha_research_trials WHERE campaign_id=?",
                (campaign_id,),
            ).fetchall()
            event_sql = (
                "SELECT event_id,event_sequence,event_type,campaign_id,protocol_hash,trial_id,job_id,attempt,"
                "request_hash,result_hash,reason_code,detail,previous_event_hash,event_hash,created_at "
                "FROM alpha_research_events WHERE campaign_id=?"
            )
            event_args = (campaign_id,)
        else:
            registered_rows = [
                row for row in self.connection.execute(
                    "SELECT trial_id,trial_definition_hash,hypothesis_id FROM alpha_research_trials WHERE campaign_id=?",
                    (campaign_id,),
                ).fetchall()
                if (self._trial_registered_sequence(row[0]) or 0) <= watermark
            ]
            event_sql = (
                "SELECT event_id,event_sequence,event_type,campaign_id,protocol_hash,trial_id,job_id,attempt,"
                "request_hash,result_hash,reason_code,detail,previous_event_hash,event_hash,created_at "
                "FROM alpha_research_events WHERE campaign_id=? AND event_sequence<=?"
            )
            event_args = (campaign_id, watermark)
        registered_ids = {row[0] for row in registered_rows}
        events = self.connection.execute(event_sql, event_args).fetchall()
        event_rows = [self._event_from_row(row) for row in events]
        submitted_ids = {event.trial_id for event in event_rows if event.event_type == "TRIAL_SUBMITTED" and event.trial_id}
        completed_ids = {event.trial_id for event in event_rows if event.event_type == "TRIAL_COMPLETED" and event.trial_id}
        failed_ids = {event.trial_id for event in event_rows if event.event_type == "TRIAL_FAILED" and event.trial_id}
        cancelled_ids = {event.trial_id for event in event_rows if event.event_type == "TRIAL_CANCELLED" and event.trial_id}
        attempts = sum(event.event_type in {"TRIAL_EXECUTION_STARTED", "TRIAL_RETRY_STARTED"} for event in event_rows)
        rejected = sum(event.event_type == "TRIAL_REJECTED" for event in event_rows)
        exposure_events = [event for event in event_rows if event.event_type == "TRIAL_RESULT_ACCESSED"]
        exposure = len({
            (event.trial_id, event.job_id, event.result_hash) for event in exposure_events
            if event.trial_id and event.job_id and event.result_hash
        })
        model_fits = sum(self._model_fit_estimate(trial_id) for trial_id in completed_ids & registered_ids)
        return TrialAccounting(
            registered_trial_count=len(registered_rows),
            submitted_trial_count=len(submitted_ids & registered_ids),
            distinct_trial_definition_count=len({row[1] for row in registered_rows}),
            execution_attempt_count=attempts,
            completed_evaluation_count=len(completed_ids & registered_ids),
            result_exposure_count=exposure,
            failed_trial_count=len(failed_ids & registered_ids),
            cancelled_trial_count=len(cancelled_ids & registered_ids),
            rejected_submission_count=rejected,
            model_fit_count=model_fits,
            statistical_hypothesis_count=len({row[2] for row in registered_rows}),
            result_access_request_count=len(exposure_events),
        )

    def _model_fit_estimate(self, trial_id: str) -> int:
        binding = self.get_trial_job(trial_id)
        if binding is None:
            return 0
        result = self._job_result(binding.job_id)
        if result is None:
            return 0
        trial = self.get_trial(trial_id)
        if trial.canonical_request.get("kind") == "train_alpha_model":
            try:
                _metrics, baseline = BaselineEvidenceLoader(self.job_store).load(
                    binding.job_id, result,
                )
            except (BaselineEvidenceError, ValueError, AlphaError):
                return 0
            return 2 if baseline is not None else 1
        if trial.canonical_request.get("kind") == "walk_forward_alpha":
            fold_count = len(result.get("walk_forward") or [])
            return 3 * fold_count
        return 0

    def _job_row(self, job_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT status,request,config_hash,result,result_hash,error_code,created_at,started_at,finished_at,attempts "
            "FROM jobs WHERE id=?", (job_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "status": row[0], "request": _load(row[1]), "config_hash": row[2], "result": _load(row[3]),
            "result_hash": row[4], "error_code": row[5], "created_at": row[6], "started_at": row[7],
            "finished_at": row[8], "attempts": row[9],
        }

    def _job_result(self, job_id: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT status,result,result_hash FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None or row[0] != "completed" or row[1] is None:
            return None
        result = _load(row[1])
        if digest(result) != row[2]:
            raise AlphaError(AlphaErrorCode.RESEARCH_EVIDENCE_HASH_MISMATCH, "Job result hash mismatch", {"job_id": job_id})
        return result

    def _result_ref(self, trial: ResearchTrialRecord, job: dict[str, Any]) -> dict[str, Any]:
        return {
            "trial_id": trial.trial_id,
            "job_id": self.get_trial_job(trial.trial_id).job_id,
            "job_status": job["status"],
            "job_config_hash": job["config_hash"],
            "result_hash": job["result_hash"],
            "evidence_scope_hash": trial.evidence_scope_hash,
        }

    def _pre_submission_rejection_event(self, trial_id: str, watermark: int) -> ResearchLedgerEvent | None:
        rows = self.connection.execute(
            "SELECT event_id,event_sequence,event_type,campaign_id,protocol_hash,trial_id,job_id,attempt,"
            "request_hash,result_hash,reason_code,detail,previous_event_hash,event_hash,created_at "
            "FROM alpha_research_events WHERE trial_id=? AND event_sequence<=? ORDER BY event_sequence",
            (trial_id, watermark),
        ).fetchall()
        rejection = None
        registered = False
        for row in rows:
            event = self._event_from_row(row)
            if event.event_type == "TRIAL_REGISTERED":
                registered = True
                continue
            if event.event_type == "TRIAL_REJECTED" and registered and event.reason_code:
                if rejection is None:
                    if event.detail.get("previous_status") != "REGISTERED" or event.detail.get("status_preserved") is not False:
                        return None
                    rejection = event
                continue
            return None
        if not registered or rejection is None or self.get_trial_job(trial_id) is not None:
            return None
        return rejection

    def _trial_reconciled_at(self, trial_id: str, watermark: int) -> bool:
        state = self._trial_status_at(trial_id, watermark)
        if state not in TERMINAL_TRIAL_STATES:
            return False
        if state == "REJECTED":
            return self._pre_submission_rejection_event(trial_id, watermark) is not None
        binding = self.get_trial_job(trial_id)
        submitted = self._trial_submitted_sequence(trial_id)
        if binding is None or submitted is None or submitted > watermark:
            return False
        job = self._job_row(binding.job_id)
        return job is not None and job["status"] in TERMINAL_JOB_STATES

    def _trial_terminal_state(
        self,
        trial: ResearchTrialRecord,
        *,
        rejection: ResearchLedgerEvent | None = None,
    ) -> ResearchTrialTerminalState:
        binding = self.get_trial_job(trial.trial_id)
        job = None if binding is None else self._job_row(binding.job_id)
        evidence = [] if job is None else [{
            "job_id": binding.job_id,
            "job_status": job["status"],
            "job_config_hash": job["config_hash"],
            "result_hash": job["result_hash"],
        }]
        exclusion = None
        if binding is None:
            exclusion = (
                "TRIAL_REJECTED_BEFORE_SUBMISSION"
                if rejection is not None
                else "TRIAL_NOT_SUBMITTED"
            )
        elif job["status"] == "completed":
            if self._job_result(binding.job_id) is None:
                exclusion = "RESULT_EVIDENCE_INVALID"
        elif job["status"] in {"failed", "cancelled"}:
            exclusion = f"JOB_{job['status'].upper()}"
        return ResearchTrialTerminalState(
            trial_id=trial.trial_id, hypothesis_id=trial.hypothesis_id, protocol_hash=trial.protocol_hash,
            trial_definition_hash=trial.trial_definition_hash, trial_purpose=trial.trial_purpose,
            preregistration_status=trial.preregistration_status,
            canonical_request_hash=trial.canonical_request_hash,
            terminal_state=trial.status, job_id=None if binding is None else binding.job_id,
            job_status=None if job is None else job["status"],
            job_config_hash=None if job is None else job["config_hash"],
            result_hash=None if job is None else job["result_hash"],
            rejection_reason=None if rejection is None else rejection.reason_code,
            evidence_references=evidence, exclusion_reason=exclusion,
        )

    def _scope_completeness(
        self, campaign_id: str, integrity: ResearchLedgerIntegrity, *, watermark: int | None = None,
    ) -> ScopeCompleteness:
        target = self.ledger_watermark(campaign_id) if watermark is None else watermark
        rows = self.connection.execute(
            "SELECT trial_id FROM alpha_research_trials WHERE campaign_id=?", (campaign_id,),
        ).fetchall()
        complete = True
        for (trial_id,) in rows:
            if (self._trial_registered_sequence(trial_id) or 0) > target:
                continue
            if not self._trial_reconciled_at(trial_id, target):
                complete = False
                break
        return ScopeCompleteness(
            managed_campaign_history_complete=bool(complete and integrity.status == "VERIFIED"),
            job_lifecycle_reconciled=bool(complete and integrity.status == "VERIFIED"),
            ledger_integrity_status=integrity.status,
        )

    def _all_trials(self, campaign_id: str) -> list[ResearchTrialRecord]:
        rows = self.connection.execute(
            "SELECT trial_id FROM alpha_research_trials WHERE campaign_id=? ORDER BY registered_at,trial_id",
            (campaign_id,),
        ).fetchall()
        return [self.get_trial(row[0]) for row in rows]

    def _research_activity_since_snapshot(self, snapshot: ResearchLedgerSnapshot) -> int:
        return int(self.connection.execute(
            "SELECT count(*) FROM alpha_research_events WHERE campaign_id=? AND event_sequence>? AND event_type IN "
            "('PROTOCOL_REGISTERED','PROTOCOL_REVISED','HYPOTHESIS_REGISTERED','HYPOTHESIS_UPDATED','TRIAL_REGISTERED',"
            "'TRIAL_SUBMITTED','TRIAL_EXECUTION_STARTED','TRIAL_RETRY_STARTED','TRIAL_PUBLISHING','TRIAL_COMPLETED',"
            "'TRIAL_FAILED','TRIAL_CANCELLED','TRIAL_WORKER_LOST','TRIAL_REJECTED')",
            (snapshot.campaign_id, snapshot.ledger_watermark),
        ).fetchone()[0])

    def seal_candidate_ledger(self, *, campaign_id: str, protocol_hash: str) -> tuple[ResearchLedgerSnapshot, ResearchSelection]:
        with _transaction(self.connection, immediate=True):
            return self._seal_candidate_ledger_committed(campaign_id=campaign_id, protocol_hash=protocol_hash)

    def _seal_candidate_ledger_committed(self, *, campaign_id: str, protocol_hash: str) -> tuple[ResearchLedgerSnapshot, ResearchSelection]:
        integrity = self.verify_ledger(campaign_id)
        if integrity.status != "VERIFIED":
            raise AlphaError(AlphaErrorCode.RESEARCH_LEDGER_INTEGRITY_FAILED, "Ledger integrity verification failed", {
                "campaign_id": campaign_id, "reason": integrity.failure_reason,
            })
        latest = self._latest_snapshot(campaign_id, protocol_hash)
        if latest is not None and self._research_activity_since_snapshot(latest) == 0:
            return latest, self.get_selection(latest.candidate_ledger_hash)
        protocol = self.get_protocol(protocol_hash)
        if protocol.definition.campaign_id != campaign_id:
            raise AlphaError(AlphaErrorCode.RESEARCH_PROTOCOL_MISMATCH, "Snapshot Protocol belongs to another Campaign", {})
        latest_protocol = self.connection.execute(
            "SELECT protocol_hash FROM alpha_research_protocols WHERE campaign_id=? "
            "ORDER BY created_at DESC,protocol_hash DESC LIMIT 1", (campaign_id,),
        ).fetchone()
        if latest_protocol is not None and latest_protocol[0] != protocol_hash:
            raise AlphaError(AlphaErrorCode.RESEARCH_PROTOCOL_MISMATCH, "A newer Protocol revision must govern the Snapshot", {
                "requested_protocol_hash": protocol_hash,
                "latest_protocol_hash": latest_protocol[0],
            })
        trials = self._all_trials(campaign_id)
        terminal_states: list[ResearchTrialTerminalState] = []
        for trial in trials:
            if not self._trial_reconciled_at(trial.trial_id, integrity.ledger_watermark):
                raise AlphaError(
                    AlphaErrorCode.RESEARCH_LEDGER_INCOMPLETE,
                    "A registered trial is not reconciled to a terminal state",
                    {"trial_id": trial.trial_id, "status": self._trial_status_at(trial.trial_id, integrity.ledger_watermark)},
                )
            rejection = (
                self._pre_submission_rejection_event(trial.trial_id, integrity.ledger_watermark)
                if trial.status == "REJECTED"
                else None
            )
            terminal_states.append(self._trial_terminal_state(trial, rejection=rejection))
        references = [
            {
                "trial_id": state.trial_id, "job_id": state.job_id, "job_status": state.job_status,
                "job_config_hash": state.job_config_hash, "result_hash": state.result_hash,
            }
            for state in terminal_states if state.job_id is not None
        ]
        policy_hash = selection_policy_hash_for(protocol.definition.selection_policy.model_dump(mode="json"))
        selection_candidates, excluded = self._selection_candidates(protocol)
        completeness = self._scope_completeness(campaign_id, integrity)
        accounting = self.trial_accounting(campaign_id)
        hips = [
            self._hypothesis_from_row(row).model_dump(mode="json")
            for row in self.connection.execute(
                "SELECT hypothesis_id,campaign_id,protocol_hash,definition,hypothesis_definition_hash,status,created_at "
                "FROM alpha_research_hypotheses WHERE campaign_id=? ORDER BY created_at,hypothesis_id",
                (campaign_id,),
            ).fetchall()
        ]
        snapshot_body = {
            "schema_version": "alpha-research-experiment-registry-v1",
            "campaign_id": campaign_id,
            "protocol_hash": protocol_hash,
            "ledger_watermark": integrity.ledger_watermark,
            "ledger_event_root_hash": integrity.event_root_hash,
            "registered_hypotheses": hips,
            "registered_trials": [state.model_dump(mode="json") for state in terminal_states],
            "evidence_references": references,
            "excluded_candidates": [entry.model_dump(mode="json") for entry in excluded],
            "selection_policy_hash": policy_hash,
            "scope_completeness": completeness.model_dump(mode="json"),
            "trial_accounting": accounting.model_dump(mode="json"),
        }
        ledger_hash = snapshot_hash_for(snapshot_body)
        snapshot = ResearchLedgerSnapshot.from_stored_payload({
            **snapshot_body,
            "candidate_ledger_hash": ledger_hash,
        })
        selection = self._compute_selection(snapshot, protocol, trials, selection_candidates, excluded)
        existing_snapshot = self.connection.execute(
            "SELECT 1 FROM alpha_research_ledger_snapshots WHERE candidate_ledger_hash=?", (ledger_hash,)
        ).fetchone()
        if existing_snapshot is not None:
            return self.get_snapshot(ledger_hash), self.get_selection(ledger_hash)
        with _transaction(self.connection):
            self.connection.execute(
                "INSERT OR IGNORE INTO alpha_research_ledger_snapshots(candidate_ledger_hash,campaign_id,protocol_hash,"
                "ledger_watermark,payload,created_at) VALUES (?,?,?,?,?,?)",
                (ledger_hash, campaign_id, protocol_hash, integrity.ledger_watermark,
                 _json(snapshot.model_dump(mode="json")), _utcnow().isoformat().replace("+00:00", "Z")),
            )
            self._append_event(
                campaign_id=campaign_id, protocol_hash=protocol_hash, event_type="LEDGER_SEALED",
                detail={"candidate_ledger_hash": ledger_hash, "ledger_watermark": integrity.ledger_watermark},
            )
            self.connection.execute(
                "INSERT OR IGNORE INTO alpha_research_selections(selection_id,campaign_id,protocol_hash,candidate_ledger_hash,"
                "selection_hash,payload,created_at) VALUES (?,?,?,?,?,?,?)",
                (selection.selection_id, campaign_id, protocol_hash, ledger_hash, selection.selection_hash,
                 _json(selection.model_dump(mode="json")), _utcnow().isoformat().replace("+00:00", "Z")),
            )
            if selection.selection_id not in {row[0] for row in self.connection.execute(
                    "SELECT selection_id FROM alpha_research_selections WHERE candidate_ledger_hash=?", (ledger_hash,)).fetchall()}:
                raise AlphaError(AlphaErrorCode.RESEARCH_LEDGER_INTEGRITY_FAILED, "Selection persistence failed", {})
            self._append_event(
                campaign_id=campaign_id, protocol_hash=protocol_hash, event_type="SELECTION_REGISTERED",
                detail={
                    "candidate_ledger_hash": ledger_hash, "selection_id": selection.selection_id,
                    "selected_trial_id": selection.selected_trial_id, "selected_model_id": selection.selected_model_id,
                    "selection_status": selection.selection_status,
                },
            )
        return snapshot, selection

    @staticmethod
    def _coverage_evidence(metrics: dict[str, Any], baseline_report) -> dict[str, float | None]:
        validation = metrics.get("validation") or {}
        model_execution = validation.get("prediction_coverage")
        label_coverage = metrics.get("label_coverage")
        end_to_end = metrics.get("end_to_end_coverage")
        if baseline_report is not None:
            counts = baseline_report.cohort_identity
            if counts.raw_validation_candidate_count > 0:
                label_coverage = counts.matured_validation_label_count / counts.raw_validation_candidate_count
                end_to_end = counts.common_cohort_sample_count / counts.raw_validation_candidate_count
        return {
            "label_coverage": float(label_coverage) if isinstance(label_coverage, (int, float)) else None,
            "model_execution_coverage": float(model_execution) if isinstance(model_execution, (int, float)) else None,
            "end_to_end_coverage": float(end_to_end) if isinstance(end_to_end, (int, float)) else None,
        }

    def _selection_candidates(self, protocol: ResearchProtocolRecord) -> tuple[list[dict[str, Any]], list[ExcludedCandidate]]:
        candidates: list[dict[str, Any]] = []
        excluded: list[ExcludedCandidate] = []
        policy = protocol.definition.selection_policy
        compatible_trials = [
            trial for trial in self._all_trials(protocol.definition.campaign_id)
            if trial.protocol_hash == protocol.protocol_hash
        ]
        incompatible_trials = [
            trial for trial in self._all_trials(protocol.definition.campaign_id)
            if trial.protocol_hash != protocol.protocol_hash
        ]
        for trial in incompatible_trials:
            excluded.append(ExcludedCandidate(
                trial_id=trial.trial_id,
                reason="PROTOCOL_NOT_COMPATIBLE_WITH_SELECTION",
                detail={"trial_protocol_hash": trial.protocol_hash, "selection_protocol_hash": protocol.protocol_hash},
            ))
        requires_walk_forward = protocol.definition.evaluation_policy.get("requires_walk_forward") is True
        for trial in compatible_trials:
            binding = self.get_trial_job(trial.trial_id)
            if binding is None:
                rejection = (
                    self._pre_submission_rejection_event(
                        trial.trial_id,
                        self.ledger_watermark(trial.campaign_id),
                    )
                    if trial.status == "REJECTED"
                    else None
                )
                if rejection is not None:
                    excluded.append(ExcludedCandidate(
                        trial_id=trial.trial_id,
                        reason="TRIAL_REJECTED_BEFORE_SUBMISSION",
                        detail={
                            "canonical_request_hash": trial.canonical_request_hash,
                            "rejection_reason": rejection.reason_code,
                        },
                    ))
                else:
                    excluded.append(ExcludedCandidate(trial_id=trial.trial_id, reason="TRIAL_NOT_SUBMITTED"))
                continue
            job = self._job_row(binding.job_id)
            if job is None or job["status"] != "completed":
                excluded.append(ExcludedCandidate(trial_id=trial.trial_id, reason=f"JOB_{str(job and job['status']).upper()}"))
                continue
            if trial.canonical_request.get("kind") != "train_alpha_model":
                excluded.append(ExcludedCandidate(trial_id=trial.trial_id, reason="WALK_FORWARD_NOT_A_FREEZEABLE_MODEL"))
                continue
            if requires_walk_forward:
                excluded.append(ExcludedCandidate(
                    trial_id=trial.trial_id,
                    reason="WALK_FORWARD_CANDIDATE_BINDING_NOT_SUPPORTED",
                    detail={
                        "requires_walk_forward": True,
                        "bound_walk_forward_trial_id": None,
                        "bound_walk_forward_report_hash": None,
                    },
                ))
                continue
            result = self._job_result(binding.job_id)
            if result is None:
                excluded.append(ExcludedCandidate(trial_id=trial.trial_id, reason="RESULT_EVIDENCE_INVALID"))
                continue
            model_id = (result.get("dataset") or {}).get("model_id")
            if not isinstance(model_id, str):
                excluded.append(ExcludedCandidate(trial_id=trial.trial_id, reason="MODEL_ID_MISSING"))
                continue
            if self.job_store is not None:
                try:
                    from ..models.registry import AlphaModelRegistry
                    AlphaModelRegistry(Path(self.job_store.path).with_suffix(".models")).load(model_id)
                except Exception as error:  # noqa: BLE001 - any registry load failure is untrusted evidence
                    excluded.append(ExcludedCandidate(
                        trial_id=trial.trial_id, model_id=model_id,
                        reason="MODEL_NOT_FOUND_IN_ORIGINAL_REGISTRY", detail={"message": str(error)[:512]},
                    ))
                    continue
            job_result = self._job_result(binding.job_id)
            try:
                if job_result is None:
                    raise BaselineEvidenceError("Job result is unavailable")
                metrics, parsed_baseline = BaselineEvidenceLoader(self.job_store).load(
                    binding.job_id, job_result,
                )
            except Exception as error:  # noqa: BLE001 - malformed persisted evidence is invalid evidence
                excluded.append(ExcludedCandidate(
                    trial_id=trial.trial_id, model_id=model_id,
                    reason="BASELINE_EVIDENCE_HASH_MISMATCH", detail={"message": str(error)[:512]},
                ))
                continue
            validation = metrics.get("validation") or {}
            rank_ic = validation.get("rank_ic_mean")
            if rank_ic is None or not isinstance(rank_ic, (int, float)) or not math.isfinite(float(rank_ic)):
                excluded.append(ExcludedCandidate(trial_id=trial.trial_id, model_id=model_id, reason="PRIMARY_METRIC_NOT_ESTIMABLE"))
                continue
            if policy.minimum_rank_ic is not None and float(rank_ic) < policy.minimum_rank_ic:
                excluded.append(ExcludedCandidate(trial_id=trial.trial_id, model_id=model_id, reason="BELOW_PRIMARY_METRIC_THRESHOLD"))
                continue
            if parsed_baseline is not None:
                if parsed_baseline.comparison_status == "COMPARISON_INVALID":
                    excluded.append(ExcludedCandidate(trial_id=trial.trial_id, model_id=model_id, reason="COMPARISON_INVALID"))
                    continue
                if parsed_baseline.comparison_status == "COMPARISON_INSUFFICIENT_DATA":
                    excluded.append(ExcludedCandidate(trial_id=trial.trial_id, model_id=model_id, reason="COMPARISON_INSUFFICIENT_DATA"))
                    continue
            coverage = self._coverage_evidence(metrics, parsed_baseline)
            coverage_checks = (
                ("label_coverage", policy.minimum_label_coverage, "LABEL_COVERAGE_NOT_ESTIMABLE", "BELOW_LABEL_COVERAGE_THRESHOLD"),
                ("model_execution_coverage", policy.minimum_model_execution_coverage, "MODEL_EXECUTION_COVERAGE_NOT_ESTIMABLE", "BELOW_MODEL_EXECUTION_COVERAGE_THRESHOLD"),
                ("end_to_end_coverage", policy.minimum_end_to_end_coverage, "END_TO_END_COVERAGE_NOT_ESTIMABLE", "BELOW_END_TO_END_COVERAGE_THRESHOLD"),
            )
            failed_coverage = None
            for name, minimum, missing_reason, below_reason in coverage_checks:
                value = coverage[name]
                if minimum > 0 and value is None:
                    failed_coverage = (missing_reason, {"coverage": name, "minimum": minimum, "coverage_evidence": coverage})
                    break
                if minimum > 0 and value < minimum:
                    failed_coverage = (below_reason, {"coverage": name, "minimum": minimum, "actual": value, "coverage_evidence": coverage})
                    break
            if failed_coverage is not None:
                excluded.append(ExcludedCandidate(
                    trial_id=trial.trial_id, model_id=model_id,
                    reason=failed_coverage[0], detail=failed_coverage[1],
                ))
                continue
            valid_ic_sessions = validation.get("valid_ic_date_count", validation.get("valid_ic_sessions"))
            if policy.minimum_valid_ic_sessions and (
                valid_ic_sessions is None or int(valid_ic_sessions) < policy.minimum_valid_ic_sessions
            ):
                excluded.append(ExcludedCandidate(trial_id=trial.trial_id, model_id=model_id, reason="BELOW_VALID_IC_SESSION_THRESHOLD"))
                continue
            if policy.minimum_valid_folds:
                excluded.append(ExcludedCandidate(
                    trial_id=trial.trial_id, model_id=model_id,
                    reason="VALID_FOLD_EVIDENCE_NOT_AVAILABLE_FOR_TRAINING_CANDIDATE",
                ))
                continue
            if policy.stability_constraints:
                excluded.append(ExcludedCandidate(
                    trial_id=trial.trial_id, model_id=model_id,
                    reason="STABILITY_CONSTRAINT_NOT_ESTIMABLE",
                ))
                continue
            research_status = "RESEARCH_EXPLORATORY"
            if parsed_baseline is not None:
                readiness = [
                    (parsed_baseline.data_quality.get(path) or {}).get("research_readiness")
                    for path in ("feature", "label_source")
                ]
                if any(value == "RESEARCH_BLOCKED" for value in readiness):
                    research_status = "RESEARCH_BLOCKED"
                elif readiness and all(value == "RESEARCH_DATA_VALIDATED" for value in readiness):
                    research_status = "RESEARCH_DATA_VALIDATED"
            if research_status not in policy.allowed_research_status:
                excluded.append(ExcludedCandidate(
                    trial_id=trial.trial_id, model_id=model_id, reason="RESEARCH_STATUS_NOT_ALLOWED",
                    detail={"research_status": research_status, "allowed": policy.allowed_research_status},
                ))
                continue
            candidates.append({
                "trial_id": trial.trial_id, "model_id": model_id, "rank_ic": float(rank_ic),
                "coverage_evidence": coverage, "job_id": binding.job_id, "result_hash": job["result_hash"],
                "evidence_references": [self._result_ref(trial, job)],
            })
        return candidates, excluded

    def _compute_selection(
        self,
        snapshot: ResearchLedgerSnapshot,
        protocol: ResearchProtocolRecord,
        trials: list[ResearchTrialRecord],
        candidates: list[dict[str, Any]],
        excluded: list[ExcludedCandidate],
    ) -> ResearchSelection:
        policy = protocol.definition.selection_policy
        policy_hash = selection_policy_hash_for(policy.model_dump(mode="json"))
        ordered = list(candidates)
        if policy.tie_break_rule == "highest_primary_metric_then_earliest_trial":
            trial_order = {trial.trial_id: index for index, trial in enumerate(trials)}
            ordered.sort(key=lambda item: (-item["rank_ic"], trial_order.get(item["trial_id"], 10**9)))
        else:
            trial_order = {trial.trial_id: index for index, trial in enumerate(trials)}
            ordered.sort(key=lambda item: trial_order.get(item["trial_id"], 10**9))
        selected = ordered[0] if ordered else None
        status = "CANDIDATE_SELECTED_FOR_FURTHER_RESEARCH" if selected else "NO_ELIGIBLE_CANDIDATE"
        reason = (
            f"Selected by predeclared policy {policy.primary_metric} among {len(candidates)} eligible candidates."
            if selected else "No completed training candidate satisfied the predeclared selection policy."
        )
        body = {
            "schema_version": "alpha-research-experiment-registry-v1",
            "campaign_id": snapshot.campaign_id,
            "protocol_hash": snapshot.protocol_hash,
            "candidate_ledger_hash": snapshot.candidate_ledger_hash,
            "selection_policy_hash": policy_hash,
            "considered_trial_ids": [trial.trial_id for trial in trials],
            "eligible_candidate_ids": [item["trial_id"] for item in ordered],
            "excluded_candidates": [entry.model_dump(mode="json") for entry in excluded],
            "selected_trial_id": None if selected is None else selected["trial_id"],
            "selected_model_id": None if selected is None else selected["model_id"],
            "selection_reason": reason,
            "evidence_references": [] if selected is None else selected["evidence_references"],
            "selection_status": status,
        }
        selection_hash = selection_hash_for(body)
        selection_id = canonical_digest({"campaign_id": snapshot.campaign_id, "selection_hash": selection_hash})
        return ResearchSelection.model_validate({**body, "selection_id": selection_id, "selection_hash": selection_hash})

    def get_snapshot(self, candidate_ledger_hash: str) -> ResearchLedgerSnapshot:
        row = self.connection.execute(
            "SELECT payload FROM alpha_research_ledger_snapshots WHERE candidate_ledger_hash=?",
            (candidate_ledger_hash,),
        ).fetchone()
        if row is None:
            raise AlphaError(AlphaErrorCode.RESEARCH_EVIDENCE_HASH_MISMATCH, "Candidate Ledger Snapshot does not exist", {"candidate_ledger_hash": candidate_ledger_hash})
        payload = _load(row[0])
        if snapshot_hash_for(payload) != payload.get("candidate_ledger_hash"):
            raise AlphaError(AlphaErrorCode.RESEARCH_EVIDENCE_HASH_MISMATCH, "Candidate Ledger Snapshot hash mismatch", {})
        return ResearchLedgerSnapshot.from_stored_payload(payload)

    def get_selection(self, candidate_ledger_hash: str) -> ResearchSelection:
        row = self.connection.execute(
            "SELECT payload FROM alpha_research_selections WHERE candidate_ledger_hash=? ORDER BY created_at DESC LIMIT 1",
            (candidate_ledger_hash,),
        ).fetchone()
        if row is None:
            raise AlphaError(AlphaErrorCode.RESEARCH_SELECTION_BLOCKED, "Selection Record does not exist", {})
        selection = ResearchSelection.model_validate(_load(row[0]))
        if selection_hash_for(selection.model_dump(mode="json")) != selection.selection_hash:
            raise AlphaError(AlphaErrorCode.RESEARCH_EVIDENCE_HASH_MISMATCH, "Selection hash mismatch", {})
        return selection

    def verify_managed_freeze(self, *, protocol_hash: str, candidate_ledger_hash: str, selected_model_id: str) -> ResearchSelection:
        if self.connection.execute("SELECT 1 FROM alpha_research_protocols WHERE protocol_hash=?", (protocol_hash,)).fetchone() is None:
            raise AlphaError(AlphaErrorCode.RESEARCH_PROTOCOL_NOT_REGISTERED, "Managed Freeze uses an unregistered Protocol", {"protocol_hash": protocol_hash})
        snapshot = self.get_snapshot(candidate_ledger_hash)
        if snapshot.protocol_hash != protocol_hash:
            raise AlphaError(AlphaErrorCode.RESEARCH_PROTOCOL_MISMATCH, "Snapshot Protocol differs from Freeze Protocol", {})
        current_watermark = self.ledger_watermark(snapshot.campaign_id)
        latest = self._latest_snapshot(snapshot.campaign_id, protocol_hash)
        research_event_after_seal = self._research_activity_since_snapshot(snapshot)
        if research_event_after_seal:
            raise AlphaError(AlphaErrorCode.RESEARCH_LEDGER_STALE, "New research activity followed the Snapshot", {
                "candidate_ledger_hash": candidate_ledger_hash,
                "snapshot_watermark": snapshot.ledger_watermark,
                "current_watermark": current_watermark,
            })
        if latest is not None and latest.candidate_ledger_hash != snapshot.candidate_ledger_hash:
            raise AlphaError(AlphaErrorCode.RESEARCH_LEDGER_STALE, "A newer Candidate Ledger Snapshot exists", {
                "candidate_ledger_hash": candidate_ledger_hash,
                "latest_candidate_ledger_hash": latest.candidate_ledger_hash,
            })
        if not snapshot.scope_completeness.managed_campaign_history_complete:
            raise AlphaError(AlphaErrorCode.RESEARCH_LEDGER_INCOMPLETE, "Snapshot is not a reconciled complete ledger", {})
        selection = self.get_selection(candidate_ledger_hash)
        if selection.campaign_id != snapshot.campaign_id or selection.protocol_hash != snapshot.protocol_hash:
            raise AlphaError(AlphaErrorCode.RESEARCH_SELECTION_POLICY_MISMATCH, "Selection belongs to another Snapshot scope", {})
        if selection.selection_policy_hash != snapshot.selection_policy_hash:
            raise AlphaError(AlphaErrorCode.RESEARCH_SELECTION_POLICY_MISMATCH, "Selection policy differs from the sealed Snapshot", {})
        if selection.selected_model_id != selected_model_id:
            raise AlphaError(AlphaErrorCode.RESEARCH_SELECTION_POLICY_MISMATCH, "Freeze model differs from the registered Selection Record", {
                "expected": selection.selected_model_id, "actual": selected_model_id,
            })
        return selection

    # ------------------------------------------------------------------
    # Evidence report and paginated ledger reads
    # ------------------------------------------------------------------
    def _read_all_section(self, job_id: str, section: str) -> list[dict[str, Any]]:
        if self.job_store is None:
            raise AlphaError(AlphaErrorCode.RESEARCH_EVIDENCE_HASH_MISMATCH, "JobStore is required for evidence reconstruction", {})
        rows: list[dict[str, Any]] = []
        cursor = None
        expected_total = None
        while True:
            page = self.job_store.get_result_unexposed(job_id, section, 1000, cursor)
            if expected_total is None:
                expected_total = page["total_count"]
            elif expected_total != page["total_count"]:
                raise AlphaError(AlphaErrorCode.RESEARCH_EVIDENCE_HASH_MISMATCH, "Paginated evidence identity changed", {"job_id": job_id, "section": section})
            rows.extend(page["rows"])
            cursor = page.get("next_cursor")
            if cursor is None:
                break
        if expected_total != len(rows):
            raise AlphaError(AlphaErrorCode.RESEARCH_EVIDENCE_HASH_MISMATCH, "Paginated evidence is incomplete", {
                "job_id": job_id, "section": section, "expected": expected_total, "actual": len(rows),
            })
        return rows

    def _baseline_evidence(self, trial: ResearchTrialRecord, job_id: str) -> dict[str, Any] | None:
        result = self._job_result(job_id)
        if result is None or "training_metrics" not in result:
            return None
        try:
            _metrics, report = BaselineEvidenceLoader(self.job_store).load(job_id, result)
        except Exception as error:  # noqa: BLE001 - report exposes malformed evidence as invalid
            return {
                "trial_id": trial.trial_id, "job_id": job_id, "status": "INVALID",
                "reason": "BASELINE_EVIDENCE_HASH_MISMATCH", "message": str(error)[:512],
            }
        if report is None:
            return None
        return {
            "trial_id": trial.trial_id, "job_id": job_id, "status": report.comparison_status,
            "report_hash": report.report_hash, "comparison_protocol_hash": report.comparison_protocol_hash,
            "cohort_identity_hash": canonical_digest({
                "train": report.cohort_identity.train_identity_hash,
                "validation": report.cohort_identity.validation_identity_hash,
            }),
            "data_quality": report.data_quality,
            "registered_model_id": (result.get("dataset") or {}).get("model_id"),
        }

    def _walk_forward_evidence(self, trial: ResearchTrialRecord, job_id: str) -> dict[str, Any] | None:
        result = self._job_result(job_id)
        if result is None or "walk_forward" not in result:
            return None
        try:
            from ..models.walk_forward import WalkForwardReport
            reconstruction = (result.get("reproducibility") or {}).get("report_reconstruction") or {}
            report_without_folds = reconstruction.get("report_without_folds")
            if not isinstance(report_without_folds, dict):
                raise TypeError("report reconstruction metadata is missing")
            folds = self._read_all_section(job_id, "walk_forward")
            report = WalkForwardReport.model_validate({**report_without_folds, "folds": folds})
            canonical_trial_request = trial.canonical_request
            if report.research_protocol_hash != canonical_trial_request.get("research_protocol_hash"):
                raise ValueError("Walk-forward Protocol hash differs from the Trial")
            if report.feature_set_id != canonical_trial_request.get("feature_set_id"):
                raise ValueError("Walk-forward feature identity differs from the Trial")
            if report.stock_dataset_id != canonical_trial_request.get("stock_dataset_id"):
                raise ValueError("Walk-forward stock dataset differs from the Trial")
        except Exception as error:  # noqa: BLE001 - report exposes malformed evidence as invalid
            return {
                "trial_id": trial.trial_id, "job_id": job_id, "status": "INVALID",
                "reason": "WALK_FORWARD_REPORT_RECONSTRUCTION_FAILED", "message": str(error)[:512],
            }
        return {
            "trial_id": trial.trial_id, "job_id": job_id, "status": report.research_status,
            "report_hash": report.report_hash, "fold_plan_hash": report.fold_plan_hash,
            "fold_count": len(report.folds), "completed_fold_count": sum(fold.fold_status == "FOLD_COMPLETED" for fold in report.folds),
            "multiple_testing": report.multiple_testing,
            "data_quality": report.data_quality,
        }

    def _data_reuse(self, campaign: ResearchCampaignRecord, cutoff: str | None = None) -> list[ResearchDataReuseFinding]:
        this_scope = campaign.definition.development_data_scope
        findings: list[ResearchDataReuseFinding] = []
        if cutoff is None:
            rows = self.connection.execute(
                "SELECT campaign_id,definition FROM alpha_research_campaigns WHERE campaign_id<>? ORDER BY created_at,campaign_id",
                (campaign.campaign_id,),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT campaign_id,definition FROM alpha_research_campaigns WHERE campaign_id<>? AND created_at<=? "
                "ORDER BY created_at,campaign_id",
                (campaign.campaign_id, cutoff),
            ).fetchall()
        for other_id, encoded in rows:
            other = ResearchCampaignDefinition.model_validate(_load(encoded)).development_data_scope
            same_stock = sorted(set(this_scope.stock_dataset_ids) & set(other.stock_dataset_ids))
            same_feature = sorted(set(this_scope.feature_dataset_ids) & set(other.feature_dataset_ids))
            same_parent = sorted(set(this_scope.parent_dataset_ids) & set(other.parent_dataset_ids))
            if same_stock or same_feature or same_parent:
                findings.append(ResearchDataReuseFinding(
                    status="EXACT_EVIDENCE_REUSE", campaign_ids=[campaign.campaign_id, other_id],
                    reason="Campaigns share persisted Dataset identities",
                    detail={"stock_dataset_ids": same_stock, "feature_dataset_ids": same_feature, "parent_dataset_ids": same_parent},
                ))
                continue
            same_source = this_scope.data_provider and this_scope.data_provider == other.data_provider
            overlapping = (
                this_scope.development_start is not None and this_scope.development_end is not None
                and other.development_start is not None and other.development_end is not None
                and this_scope.development_start < other.development_end
                and other.development_start < this_scope.development_end
            )
            same_labels = bool(set(map(_json, this_scope.label_definitions)) & set(map(_json, other.label_definitions)))
            if same_source and (overlapping or same_labels):
                findings.append(ResearchDataReuseFinding(
                    status="POTENTIAL_OVERLAPPING_EVIDENCE", campaign_ids=[campaign.campaign_id, other_id],
                    reason="Different Dataset IDs do not prove independent source observations",
                    detail={"same_provider": True, "overlapping_development_window": overlapping, "same_label_definition": same_labels},
                ))
            elif same_source:
                findings.append(ResearchDataReuseFinding(
                    status="DISTINCT_DECLARED_SCOPE", campaign_ids=[campaign.campaign_id, other_id],
                    reason="Declared windows do not overlap, but sample independence is not independently proven",
                    detail={"same_provider": True},
                ))
            else:
                findings.append(ResearchDataReuseFinding(
                    status="INDEPENDENCE_UNVERIFIED", campaign_ids=[campaign.campaign_id, other_id],
                    reason="Source, universe and sample construction cannot be proven independent",
                ))
        if not findings:
            findings.append(ResearchDataReuseFinding(
                status="INDEPENDENCE_UNVERIFIED", campaign_ids=[campaign.campaign_id],
                reason="No cross-Campaign evidence is available; a new Campaign name does not establish independence",
            ))
        return findings

    def _latest_snapshot(self, campaign_id: str, protocol_hash: str | None = None) -> ResearchLedgerSnapshot | None:
        if protocol_hash:
            row = self.connection.execute(
                "SELECT candidate_ledger_hash FROM alpha_research_ledger_snapshots WHERE campaign_id=? AND protocol_hash=? "
                "ORDER BY ledger_watermark DESC, created_at DESC LIMIT 1", (campaign_id, protocol_hash),
            ).fetchone()
        else:
            row = self.connection.execute(
                "SELECT candidate_ledger_hash FROM alpha_research_ledger_snapshots WHERE campaign_id=? "
                "ORDER BY ledger_watermark DESC, created_at DESC LIMIT 1", (campaign_id,),
            ).fetchone()
        return None if row is None else self.get_snapshot(row[0])

    def _snapshot_sealed_at(self, snapshot: ResearchLedgerSnapshot) -> str | None:
        row = self.connection.execute(
            "SELECT created_at FROM alpha_research_events WHERE campaign_id=? AND event_type='LEDGER_SEALED' "
            "AND json_extract(detail,'$.candidate_ledger_hash')=? ORDER BY event_sequence LIMIT 1",
            (snapshot.campaign_id, snapshot.candidate_ledger_hash),
        ).fetchone()
        return None if row is None else row[0]

    def _event_cutoff(self, campaign_id: str, watermark: int) -> str | None:
        row = self.connection.execute(
            "SELECT created_at FROM alpha_research_events WHERE campaign_id=? AND event_sequence<=? "
            "ORDER BY event_sequence DESC LIMIT 1",
            (campaign_id, watermark),
        ).fetchone()
        return None if row is None else row[0]

    def _latest_protocol_at(self, campaign_id: str, watermark: int) -> str | None:
        rows = self.connection.execute(
            "SELECT protocol_hash,event_sequence FROM alpha_research_events WHERE campaign_id=? "
            "AND event_sequence<=? AND event_type IN ('PROTOCOL_REGISTERED','PROTOCOL_REVISED') "
            "AND protocol_hash IS NOT NULL ORDER BY event_sequence DESC LIMIT 1",
            (campaign_id, watermark),
        ).fetchone()
        return None if rows is None else rows[0]

    def build_evidence_report(
        self,
        *,
        campaign_id: str,
        protocol_hash: str | None = None,
        candidate_ledger_hash: str | None = None,
        ledger_watermark: int | None = None,
    ) -> ResearchEvidenceReport:
        campaign = self.get_campaign(campaign_id)
        snapshot = self.get_snapshot(candidate_ledger_hash) if candidate_ledger_hash else None
        if ledger_watermark is not None and (type(ledger_watermark) is not int or ledger_watermark < 0):
            raise ValueError("Invalid ledger watermark")
        data_reuse_cutoff = None
        if snapshot is not None:
            ledger_watermark = snapshot.ledger_watermark
            data_reuse_cutoff = self._snapshot_sealed_at(snapshot)
            protocol_hash = snapshot.protocol_hash
            integrity = ResearchLedgerIntegrity(
                status=snapshot.scope_completeness.ledger_integrity_status,
                campaign_id=campaign_id,
                ledger_watermark=snapshot.ledger_watermark,
                event_root_hash=snapshot.ledger_event_root_hash,
            )
            completeness = snapshot.scope_completeness
            watermark = snapshot.ledger_watermark
        else:
            if ledger_watermark is not None:
                current = self.ledger_watermark(campaign_id)
                if ledger_watermark > current:
                    raise AlphaError(AlphaErrorCode.RESEARCH_LEDGER_INCOMPLETE, "Requested report watermark is unavailable", {
                        "campaign_id": campaign_id, "requested_watermark": ledger_watermark, "current_watermark": current,
                    })
                integrity = self.verify_ledger(campaign_id, watermark=ledger_watermark)
                watermark = ledger_watermark
                data_reuse_cutoff = self._event_cutoff(campaign_id, watermark)
            else:
                integrity = self.verify_ledger(campaign_id)
                watermark = integrity.ledger_watermark
            completeness = self._scope_completeness(campaign_id, integrity, watermark=watermark)
        if protocol_hash is None:
            protocol_hash = self._latest_protocol_at(campaign_id, watermark)
            if protocol_hash is None:
                raise AlphaError(AlphaErrorCode.RESEARCH_PROTOCOL_NOT_REGISTERED, "Campaign has no Protocol at this watermark", {})
        protocol = self.get_protocol(protocol_hash)
        protocol_sequence = self.connection.execute(
            "SELECT min(event_sequence) FROM alpha_research_events WHERE campaign_id=? AND protocol_hash=? "
            "AND event_type IN ('PROTOCOL_REGISTERED','PROTOCOL_REVISED')",
            (campaign_id, protocol_hash),
        ).fetchone()[0]
        if protocol_sequence is None or protocol_sequence > watermark:
            raise AlphaError(AlphaErrorCode.RESEARCH_LEDGER_INCOMPLETE, "Protocol is not registered at this watermark", {
                "campaign_id": campaign_id, "protocol_hash": protocol_hash, "watermark": watermark,
            })
        hypotheses = [
            hypothesis for hypothesis in protocol.registered_hypotheses
            if (self._hypothesis_registered_sequence(hypothesis.hypothesis_id) or 0) <= watermark
        ]
        trials = [
            trial for trial in self._all_trials(campaign_id)
            if (self._trial_registered_sequence(trial.trial_id) or 0) <= watermark
        ]
        baseline_evidence: list[dict[str, Any]] = []
        walk_forward_evidence: list[dict[str, Any]] = []
        evidence_references: list[dict[str, Any]] = []
        data_quality: list[dict[str, Any]] = []
        for trial in trials:
            binding = self.get_trial_job(trial.trial_id)
            submitted_sequence = self._trial_submitted_sequence(trial.trial_id)
            if binding is None or submitted_sequence is None or submitted_sequence > watermark:
                continue
            if self._trial_status_at(trial.trial_id, watermark) != "COMPLETED":
                continue
            job = self._job_row(binding.job_id)
            if job is None or job["status"] != "completed":
                continue
            evidence_references.append(self._result_ref(trial, job))
            baseline = self._baseline_evidence(trial, binding.job_id)
            if baseline is not None:
                baseline_evidence.append(baseline)
                if baseline.get("data_quality"):
                    data_quality.append({"trial_id": trial.trial_id, "kind": "baseline", **baseline["data_quality"]})
            walk = self._walk_forward_evidence(trial, binding.job_id)
            if walk is not None:
                walk_forward_evidence.append(walk)
                if walk.get("data_quality"):
                    data_quality.append({"trial_id": trial.trial_id, "kind": "walk_forward", **walk["data_quality"]})
        accounting = snapshot.trial_accounting if snapshot is not None else self.trial_accounting(campaign_id, watermark=watermark)
        access_events = [
            event for event in self.list_events(campaign_id)
            if event.event_sequence <= watermark and event.event_type == "TRIAL_RESULT_ACCESSED"
        ]
        exposure_groups: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        for event in access_events:
            if not (event.trial_id and event.job_id and event.result_hash):
                continue
            section = str(event.detail.get("section", "unknown"))
            key = (event.trial_id, event.job_id, event.result_hash, section)
            created = event.created_at.isoformat().replace("+00:00", "Z")
            if key not in exposure_groups:
                exposure_groups[key] = {
                    "trial_id": key[0], "job_id": key[1], "result_hash": key[2], "section": key[3],
                    "first_accessed_at": created, "last_accessed_at": created, "access_count": 1,
                }
            else:
                exposure_groups[key]["last_accessed_at"] = created
                exposure_groups[key]["access_count"] += 1
        accessed_results = {
            (event.trial_id, event.job_id, event.result_hash)
            for event in access_events if event.trial_id and event.job_id and event.result_hash
        }
        exposure_summary = {
            "evaluation_generated_count": accounting.completed_evaluation_count,
            "evaluation_accessed_result_count": len(accessed_results),
            "access_request_count": len(access_events),
            "exposure_tracking_complete": completeness.exposure_tracking_complete,
            "accesses": list(exposure_groups.values()),
        }
        selection = None
        if snapshot is not None:
            try:
                selection = self.get_selection(snapshot.candidate_ledger_hash)
            except AlphaError:
                selection = None
        hypothesis_summary = []
        for hypothesis in hypotheses:
            related = [trial for trial in trials if trial.hypothesis_id == hypothesis.hypothesis_id]
            hypothesis_summary.append({
                "hypothesis_id": hypothesis.hypothesis_id,
                "hypothesis_key": hypothesis.definition.hypothesis_key,
                "title": hypothesis.definition.title,
                "status": hypothesis.status,
                "trial_count": len(related),
                "post_hoc_trial_count": sum(trial.preregistration_status == "POST_HOC_EXPLORATORY" for trial in related),
                "after_exposure_trial_count": sum(trial.preregistration_status == "REGISTERED_AFTER_PRIOR_EXPOSURE" for trial in related),
            })
        multiple_testing = {
            "scope": "CURRENT_PREREGISTERED_FAMILY_ONLY",
            "campaign_correction": "CAMPAIGN_CORRECTION_NOT_ESTIMABLE",
            "reason": "Trial count is not a statistical family and is never multiplied into a p-value.",
            "local_reports": [
                item.get("multiple_testing") for item in walk_forward_evidence
                if item.get("multiple_testing")
            ],
        }
        invalid_evidence = any(item.get("status") == "INVALID" for item in baseline_evidence + walk_forward_evidence)
        post_hoc = any(trial.preregistration_status != "PREREGISTERED_WITHIN_MANAGED_CAMPAIGN" for trial in trials)
        if integrity.status != "VERIFIED" or invalid_evidence:
            verdict = "RESEARCH_EVIDENCE_INVALID"
        elif not evidence_references:
            verdict = "RESEARCH_INSUFFICIENT_EVIDENCE"
        elif selection is not None and selection.selection_status == "SELECTION_BLOCKED":
            verdict = "RESEARCH_SELECTION_BLOCKED"
        elif post_hoc or any(item.get("status") in {"RESEARCH_EXPLORATORY", "WF_EXPLORATORY", "WF_INSUFFICIENT_EVIDENCE", "COMPARISON_EXPLORATORY", "COMPARISON_INSUFFICIENT_DATA"} for item in baseline_evidence + walk_forward_evidence):
            verdict = "RESEARCH_EXPLORATORY_ONLY"
        else:
            verdict = "RESEARCH_EVIDENCE_INTEGRITY_VERIFIED"
        unresolved = [
            "Global trial history remains incomplete across devices, direct Python calls and external tools.",
            "The Registry cannot prove that researchers never observed the same data outside this system.",
            "Current data authenticity and historical PIT coverage may remain unresolved.",
            "Local preregistered-family correction is not a global Alpha certification.",
            "A high-privilege attacker who rewrites SQLite and all hash chains is outside ordinary interface protection.",
        ]
        if not completeness.exposure_tracking_complete:
            unresolved.append("Result exposure tracking is incomplete because not every possible public read path is instrumented.")
        if protocol.definition.evaluation_policy.get("requires_walk_forward") is True:
            unresolved.append(
                "Walk-forward candidate binding is not supported; training candidates fail closed with "
                "WALK_FORWARD_CANDIDATE_BINDING_NOT_SUPPORTED."
            )
        body = {
            "campaign_identity": campaign.model_dump(mode="json"),
            "protocol_identity": {
                **protocol.model_dump(mode="json"),
                "registered_hypotheses": [item.model_dump(mode="json") for item in hypotheses],
            },
            "ledger_watermark": watermark,
            "ledger_integrity": integrity.model_dump(mode="json"),
            "scope_completeness": completeness.model_dump(mode="json"),
            "trial_accounting": accounting.model_dump(mode="json"),
            "resource_usage": {
                "model_fit_count": accounting.model_fit_count,
                "hard_limits": campaign.definition.resource_policy.hard_limits(),
                "reporting_only_limits": campaign.definition.resource_policy.reporting_only(),
            },
            "hypothesis_summary": hypothesis_summary,
            "data_quality_summary": data_quality,
            "data_reuse_summary": [item.model_dump(mode="json") for item in self._data_reuse(campaign, data_reuse_cutoff)],
            "result_exposure_summary": exposure_summary,
            "baseline_evidence": baseline_evidence,
            "walk_forward_evidence": walk_forward_evidence,
            "statistical_inference": multiple_testing,
            "multiple_testing_scope": "CURRENT_PREREGISTERED_FAMILY_ONLY",
            "candidate_selection": None if selection is None else selection.model_dump(mode="json"),
            "unresolved_risks": unresolved,
            "research_verdict": verdict,
            "evidence_references": evidence_references,
        }
        report_hash = report_hash_for(body)
        return ResearchEvidenceReport.model_validate({**body, "report_hash": report_hash})

    def read_research_ledger(
        self,
        *,
        campaign_id: str,
        section: str = "summary",
        limit: int = 100,
        cursor: str | None = None,
        ledger_watermark: int | None = None,
        mcp_overhead_bytes: int | None = None,
    ) -> dict[str, Any]:
        with _transaction(self.connection, immediate=False):
            return self._read_research_ledger_view(
                campaign_id=campaign_id, section=section, limit=limit, cursor=cursor,
                ledger_watermark=ledger_watermark, mcp_overhead_bytes=mcp_overhead_bytes,
            )

    @staticmethod
    def _cursor_view_identity(cursor: str) -> str:
        try:
            parts = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            identity = parts[0]
            if not isinstance(identity, str) or not identity.startswith("ledger-v1|"):
                raise ValueError
            return identity
        except Exception as error:
            raise ValueError("Invalid result cursor") from error

    def _trial_status_at(self, trial_id: str, watermark: int) -> str:
        status = "REGISTERED"
        mapping = {
            "TRIAL_REGISTERED": "REGISTERED",
            "TRIAL_SUBMITTED": "SUBMITTED",
            "TRIAL_EXECUTION_STARTED": "RUNNING",
            "TRIAL_RETRY_STARTED": "RUNNING",
            "TRIAL_PUBLISHING": "PUBLISHING",
            "TRIAL_COMPLETED": "COMPLETED",
            "TRIAL_FAILED": "FAILED",
            "TRIAL_CANCELLED": "CANCELLED",
            "TRIAL_WORKER_LOST": "FAILED",
            "TRIAL_REJECTED": "REJECTED",
        }
        for row in self.connection.execute(
            "SELECT event_type,detail FROM alpha_research_events WHERE trial_id=? AND event_sequence<=? ORDER BY event_sequence",
            (trial_id, watermark),
        ).fetchall():
            event_type, encoded_detail = row
            detail = _load(encoded_detail)
            if (
                event_type == "TRIAL_REJECTED"
                and isinstance(detail, dict)
                and detail.get("status_preserved") is True
            ):
                continue
            if event_type in mapping and status != "COMPLETED":
                status = mapping[event_type]
        return status

    def _event_sequence(self, *, trial_id: str | None = None, event_type: str | None = None,
                        protocol_hash: str | None = None) -> int | None:
        clauses, args = [], []
        if trial_id is not None:
            clauses.append("trial_id=?")
            args.append(trial_id)
        if event_type is not None:
            clauses.append("event_type=?")
            args.append(event_type)
        if protocol_hash is not None:
            clauses.append("protocol_hash=?")
            args.append(protocol_hash)
        row = self.connection.execute(
            f"SELECT min(event_sequence) FROM alpha_research_events WHERE {' AND '.join(clauses)}", args,
        ).fetchone()
        return None if row is None else row[0]

    def _trial_registered_sequence(self, trial_id: str) -> int | None:
        return self._event_sequence(trial_id=trial_id, event_type="TRIAL_REGISTERED")

    def _trial_submitted_sequence(self, trial_id: str) -> int | None:
        return self._event_sequence(trial_id=trial_id, event_type="TRIAL_SUBMITTED")

    def _hypothesis_registered_sequence(self, hypothesis_id: str) -> int | None:
        row = self.connection.execute(
            "SELECT min(event_sequence) FROM alpha_research_events WHERE event_type='HYPOTHESIS_REGISTERED' "
            "AND json_extract(detail,'$.hypothesis_id')=?", (hypothesis_id,),
        ).fetchone()
        return None if row is None else row[0]

    def _read_research_ledger_view(
        self,
        *,
        campaign_id: str,
        section: str = "summary",
        limit: int = 100,
        cursor: str | None = None,
        ledger_watermark: int | None = None,
        mcp_overhead_bytes: int | None = None,
    ) -> dict[str, Any]:
        campaign = self.get_campaign(campaign_id)
        if cursor is not None:
            identity = self._cursor_view_identity(cursor)
            _, encoded_campaign, encoded_watermark, encoded_snapshot = identity.split("|", 3)
            if encoded_campaign != campaign_id:
                raise ValueError("Invalid result cursor")
            watermark = int(encoded_watermark)
            snapshot_hash = encoded_snapshot or None
        else:
            snapshot = self._latest_snapshot(campaign_id)
            if ledger_watermark is not None:
                if type(ledger_watermark) is not int or ledger_watermark < 0:
                    raise ValueError("Invalid ledger watermark")
                watermark = ledger_watermark
                # An explicit watermark is an immutable event view; a Snapshot
                # created later must not retroactively change its rows or hash.
                snapshot_hash = None
            else:
                watermark = snapshot.ledger_watermark if snapshot is not None else self.ledger_watermark(campaign_id)
                snapshot_hash = None if snapshot is None else snapshot.candidate_ledger_hash
            identity = f"ledger-v1|{campaign_id}|{watermark}|{snapshot_hash or ''}"
        current_watermark = self.ledger_watermark(campaign_id)
        if watermark > current_watermark:
            raise AlphaError(AlphaErrorCode.RESEARCH_LEDGER_INCOMPLETE, "Requested ledger watermark is not available", {
                "campaign_id": campaign_id, "requested_watermark": watermark, "current_watermark": current_watermark,
            })
        integrity = self.verify_ledger(campaign_id, watermark=watermark)
        sections: dict[str, list[dict[str, Any]]] = {}
        events_models = [event for event in self.list_events(campaign_id) if event.event_sequence <= watermark]
        active_protocol_hashes = {
            event.protocol_hash for event in events_models
            if event.protocol_hash and event.event_type in {"PROTOCOL_REGISTERED", "PROTOCOL_REVISED"}
        }
        protocols = [
            self.get_protocol(row[0]).model_dump(mode="json")
            for row in self.connection.execute(
                "SELECT protocol_hash FROM alpha_research_protocols WHERE campaign_id=? ORDER BY created_at,protocol_hash",
                (campaign_id,),
            ).fetchall() if row[0] in active_protocol_hashes
        ]
        trials = []
        for trial in self._all_trials(campaign_id):
            if (self._trial_registered_sequence(trial.trial_id) or 0) > watermark:
                continue
            encoded = trial.model_dump(mode="json")
            encoded["status"] = self._trial_status_at(trial.trial_id, watermark)
            trials.append(encoded)
        events = [event.model_dump(mode="json") for event in events_models]
        hypotheses = [
            self._hypothesis_from_row(item).model_dump(mode="json")
            for item in self.connection.execute(
                "SELECT hypothesis_id,campaign_id,protocol_hash,definition,hypothesis_definition_hash,status,created_at "
                "FROM alpha_research_hypotheses WHERE campaign_id=? ORDER BY created_at,hypothesis_id",
                (campaign_id,),
            ).fetchall()
            if (self._hypothesis_registered_sequence(item[0]) or 0) <= watermark
        ]
        sealed_snapshot_hashes: list[str] = []
        registered_selection_ids: set[str] = set()
        for event in events_models:
            if event.event_type == "LEDGER_SEALED":
                snapshot_hash_value = event.detail.get("candidate_ledger_hash")
                if isinstance(snapshot_hash_value, str) and snapshot_hash_value not in sealed_snapshot_hashes:
                    sealed_snapshot_hashes.append(snapshot_hash_value)
            elif event.event_type == "SELECTION_REGISTERED":
                selection_id_value = event.detail.get("selection_id")
                if isinstance(selection_id_value, str):
                    registered_selection_ids.add(selection_id_value)
        snapshots = [self.get_snapshot(value).model_dump(mode="json") for value in sealed_snapshot_hashes]
        allowed_snapshot_hashes = set(sealed_snapshot_hashes)
        selections = [
            ResearchSelection.model_validate(_load(row[0])).model_dump(mode="json")
            for row in self.connection.execute(
                "SELECT payload FROM alpha_research_selections WHERE campaign_id=? ORDER BY created_at,selection_id",
                (campaign_id,),
            ).fetchall()
            if _load(row[0]).get("selection_id") in registered_selection_ids
            and _load(row[0]).get("candidate_ledger_hash") in allowed_snapshot_hashes
        ]
        sections.update({
            "campaigns": [campaign.model_dump(mode="json")],
            "protocols": protocols,
            "hypotheses": hypotheses,
            "trials": trials,
            "events": events,
            "snapshots": snapshots,
            "selections": selections,
        })
        if snapshot_hash is not None:
            accounting = self.get_snapshot(snapshot_hash).trial_accounting
            completeness = self.get_snapshot(snapshot_hash).scope_completeness
        else:
            accounting = self.trial_accounting(campaign_id, watermark=watermark)
            completeness = self._scope_completeness(campaign_id, integrity, watermark=watermark)
        summary = {
            "schema_version": "alpha-research-experiment-registry-v1",
            "campaign_id": campaign_id,
            "campaign_status": campaign.campaign_status,
            "ledger_watermark": watermark,
            "current_ledger_watermark": self.ledger_watermark(campaign_id),
            "ledger_integrity": integrity.model_dump(mode="json"),
            "scope_completeness": completeness.model_dump(mode="json"),
            "trial_accounting": accounting.model_dump(mode="json"),
            "latest_protocol_hash": protocols[-1]["protocol_hash"] if protocols else None,
            "latest_candidate_ledger_hash": snapshot_hash,
        }
        if section == "summary":
            return summary
        if section == "evidence":
            rows = []
            for trial in self._all_trials(campaign_id):
                if (self._trial_registered_sequence(trial.trial_id) or 0) > watermark:
                    continue
                binding = self.get_trial_job(trial.trial_id)
                submitted_sequence = self._trial_submitted_sequence(trial.trial_id)
                binding_visible = (
                    binding is not None and submitted_sequence is not None and submitted_sequence <= watermark
                )
                job = None if not binding_visible else self._job_row(binding.job_id)
                state = self._trial_status_at(trial.trial_id, watermark)
                rows.append({
                    "trial_id": trial.trial_id,
                    "job_id": binding.job_id if binding_visible else None,
                    "job_status": (
                        None if not binding_visible else
                        "completed" if state == "COMPLETED" else
                        "failed" if state in {"FAILED", "REJECTED"} else
                        "cancelled" if state == "CANCELLED" else
                        state.lower()
                    ),
                    "job_config_hash": binding.job_config_hash if binding_visible else None,
                    "result_hash": (
                        None if job is None or state != "COMPLETED" else job["result_hash"]
                    ),
                    "evidence_scope_hash": trial.evidence_scope_hash,
                })
            sections[section] = rows
        if section not in sections:
            raise ValueError("Unknown research ledger section")
        resource_hash = canonical_digest({"identity": identity, "section": section})
        return result_page(
            identity, section, resource_hash, len(sections[section]), limit, cursor,
            lambda offset, size: sections[section][offset:offset + size],
            resource_uri=f"quant://research-ledger/{campaign_id}/{section}",
            byte_limit=3 * 1024 * 1024,
            metadata={"ledger_watermark": watermark, "snapshot_hash": snapshot_hash},
            mcp_overhead_bytes=mcp_overhead_bytes,
        )

    @staticmethod
    def _report_cursor_view(cursor: str) -> tuple[str, int, str | None, str | None, str]:
        try:
            parts = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            identity = parts[0]
            if not isinstance(identity, str) or not identity.startswith("report-v1|"):
                raise ValueError
            _, campaign_id, watermark, candidate, protocol, report_hash = identity.split("|", 5)
            return campaign_id, int(watermark), candidate or None, protocol or None, report_hash
        except Exception as error:
            raise ValueError("Invalid result cursor") from error

    def read_research_report(
        self,
        *,
        campaign_id: str,
        protocol_hash: str | None = None,
        candidate_ledger_hash: str | None = None,
        ledger_watermark: int | None = None,
        section: str = "summary",
        limit: int = 100,
        cursor: str | None = None,
        mcp_overhead_bytes: int | None = None,
    ) -> dict[str, Any]:
        cursor_report_hash = None
        if cursor is not None:
            encoded_campaign, ledger_watermark, candidate_ledger_hash, protocol_hash, cursor_report_hash = (
                self._report_cursor_view(cursor)
            )
            if encoded_campaign != campaign_id:
                raise ValueError("Invalid result cursor")
        report = self.build_evidence_report(
            campaign_id=campaign_id, protocol_hash=protocol_hash,
            candidate_ledger_hash=candidate_ledger_hash, ledger_watermark=ledger_watermark,
        )
        if cursor_report_hash is not None and report.report_hash != cursor_report_hash:
            raise AlphaError(AlphaErrorCode.RESEARCH_EVIDENCE_HASH_MISMATCH, "Fixed historical report changed", {
                "expected_report_hash": cursor_report_hash, "actual_report_hash": report.report_hash,
            })
        for reference in report.evidence_references:
            if reference.get("job_id"):
                self.record_result_access(job_id=reference["job_id"], section="research_report")
        if section == "report":
            return report.model_dump(mode="json")
        if section == "summary":
            return {
                "schema_version": report.schema_version,
                "campaign_id": report.campaign_identity["campaign_id"],
                "protocol_hash": report.protocol_identity["protocol_hash"],
                "ledger_watermark": report.ledger_watermark,
                "ledger_integrity": report.ledger_integrity.model_dump(mode="json"),
                "scope_completeness": report.scope_completeness.model_dump(mode="json"),
                "trial_accounting": report.trial_accounting.model_dump(mode="json"),
                "research_verdict": report.research_verdict,
                "multiple_testing_scope": report.multiple_testing_scope,
                "report_hash": report.report_hash,
            }
        rows_by_section = {
            "hypotheses": report.hypothesis_summary,
            "data_quality": report.data_quality_summary,
            "data_reuse": [item.model_dump(mode="json") for item in report.data_reuse_summary],
            "exposures": [report.result_exposure_summary],
            "baseline_evidence": report.baseline_evidence,
            "walk_forward_evidence": report.walk_forward_evidence,
            "evidence": report.evidence_references,
            "unresolved_risks": [{"risk": risk} for risk in report.unresolved_risks],
        }
        if section not in rows_by_section:
            raise ValueError("Unknown research report section")
        rows = rows_by_section[section]
        identity = (
            f"report-v1|{campaign_id}|{report.ledger_watermark}|"
            f"{candidate_ledger_hash or ''}|{protocol_hash or ''}|{report.report_hash}"
        )
        return result_page(
            identity, section, report.report_hash, len(rows), limit, cursor,
            lambda offset, size: rows[offset:offset + size],
            resource_uri=f"quant://research-reports/{report.report_hash}/{section}",
            byte_limit=3 * 1024 * 1024,
            metadata={"ledger_watermark": report.ledger_watermark, "report_hash": report.report_hash},
            mcp_overhead_bytes=mcp_overhead_bytes,
        )
