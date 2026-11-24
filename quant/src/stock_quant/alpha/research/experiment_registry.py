"""Append-only research Campaign, Protocol, Trial and selection registry."""
from __future__ import annotations

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
from .contracts import (
    ExcludedCandidate,
    HypothesisRecord,
    ResearchCampaignDefinition,
    ResearchCampaignRecord,
    ResearchDataReuseFinding,
    ResearchEvidenceReport,
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

    def verify_ledger(self, campaign_id: str) -> ResearchLedgerIntegrity:
        events = self.list_events(campaign_id)
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
        protocol = self.get_protocol(protocol_hash)
        if protocol.definition.campaign_id != campaign_id:
            raise AlphaError(AlphaErrorCode.RESEARCH_PROTOCOL_MISMATCH, "Protocol belongs to another Campaign", {})
        parsed = ALPHA_REQUEST_ADAPTER.validate_python(request)
        if not isinstance(parsed, (TrainAlphaModelJob, WalkForwardAlphaRequest)):
            raise TypeError("Managed Research trials currently support training and walk-forward Jobs only")
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
        self._validate_protocol_constraints(protocol.definition, request_payload)
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
            self._check_registration_budget(campaign, definition_hash)
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

    def _validate_protocol_constraints(self, protocol: ResearchProtocolDefinition, request: dict[str, Any]) -> None:
        label = request.get("label_spec") or {}
        stock_id = request.get("stock_dataset_id") or label.get("stock_dataset_id")
        constraints = protocol.data_constraints
        for key, value in (("stock_dataset_id", stock_id), ("feature_set_id", request.get("feature_set_id"))):
            allowed = constraints.get(f"{key}s") or constraints.get(key)
            if isinstance(allowed, str):
                allowed = [allowed]
            if isinstance(allowed, list) and value not in allowed:
                raise AlphaError(AlphaErrorCode.RESEARCH_PROTOCOL_MISMATCH, "Trial data identity is outside the Protocol", {"field": key})
        labels = protocol.label_specifications
        if labels and label and not any(self._contains(item, label) for item in labels):
            raise AlphaError(AlphaErrorCode.RESEARCH_PROTOCOL_MISMATCH, "Trial label definition is outside the Protocol", {"field": "label"})
        window = request.get("split") or request.get("development_window")
        if protocol.development_windows and not any(self._contains(item, window) for item in protocol.development_windows):
            raise AlphaError(AlphaErrorCode.RESEARCH_PROTOCOL_MISMATCH, "Trial development window is outside the Protocol", {"field": "development_window"})
        if protocol.factor_specifications:
            factor_spec = (request.get("baseline_comparison") or {}).get("factor_spec") or request.get("factor_spec")
            if not any(self._contains(item, factor_spec) for item in protocol.factor_specifications):
                raise AlphaError(AlphaErrorCode.RESEARCH_PROTOCOL_MISMATCH, "Trial FactorSpec is outside the Protocol", {"field": "factor_spec"})
        model_config = request.get("hyperparameters") or request.get("lightgbm_hyperparameters") or {}
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
            request_seed = request.get("random_seed", 0)
            if fixed_seed is not None and request_seed != fixed_seed:
                raise AlphaError(AlphaErrorCode.RESEARCH_PROTOCOL_MISMATCH, "Trial seed differs from the Protocol", {"field": "random_seed"})
            if isinstance(allowed_seeds, list) and request_seed not in allowed_seeds:
                raise AlphaError(AlphaErrorCode.RESEARCH_PROTOCOL_MISMATCH, "Trial seed is outside the Protocol", {"field": "random_seed"})
        if request.get("kind") == "walk_forward_alpha" and protocol.inference_policy:
            inference = (request.get("walk_forward_config") or {}).get("inference") or {}
            if not self._contains(protocol.inference_policy, inference):
                raise AlphaError(AlphaErrorCode.RESEARCH_PROTOCOL_MISMATCH, "Walk-forward inference differs from the Protocol", {"field": "walk_forward_config.inference"})

    def _check_registration_budget(self, campaign: ResearchCampaignRecord, definition_hash: str) -> None:
        budget = campaign.definition.resource_policy
        registered = self.connection.execute(
            "SELECT count(*) FROM alpha_research_trials WHERE campaign_id=?", (campaign.campaign_id,)
        ).fetchone()[0]
        if registered >= budget.max_registered_trials:
            raise AlphaError(AlphaErrorCode.RESEARCH_TRIAL_BUDGET_EXCEEDED, "Trial registration budget exceeded", {"limit": budget.max_registered_trials})
        distinct = self.connection.execute(
            "SELECT count(DISTINCT trial_definition_hash) FROM alpha_research_trials WHERE campaign_id=?",
            (campaign.campaign_id,),
        ).fetchone()[0]
        exists = self.connection.execute(
            "SELECT 1 FROM alpha_research_trials WHERE campaign_id=? AND trial_definition_hash=? LIMIT 1",
            (campaign.campaign_id, definition_hash),
        ).fetchone()
        if exists is None and distinct >= budget.max_distinct_candidate_definitions:
            raise AlphaError(AlphaErrorCode.RESEARCH_TRIAL_BUDGET_EXCEEDED, "Distinct candidate definition budget exceeded", {"limit": budget.max_distinct_candidate_definitions})

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
        budget = campaign.definition.resource_policy
        submitted = self.connection.execute(
            "SELECT count(*) FROM alpha_research_trial_jobs WHERE campaign_id=?", (trial.campaign_id,)
        ).fetchone()[0]
        if submitted >= min(budget.max_submitted_trials, budget.max_evaluation_jobs):
            raise AlphaError(AlphaErrorCode.RESEARCH_TRIAL_BUDGET_EXCEEDED, "Managed research submission budget exceeded", {"limit": min(budget.max_submitted_trials, budget.max_evaluation_jobs)})
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

    def record_rejection(self, request: dict[str, Any], *, reason_code: str, detail: dict[str, Any] | None = None) -> None:
        trial_id = request.get("research_trial_id") if isinstance(request, dict) else None
        if not trial_id:
            return
        try:
            trial = self.get_trial(trial_id)
        except AlphaError:
            return
        with _transaction(self.connection):
            self._append_event(
                campaign_id=trial.campaign_id, protocol_hash=trial.protocol_hash, trial_id=trial.trial_id,
                reason_code=reason_code, event_type="TRIAL_REJECTED", detail=detail or {},
            )
            self.connection.execute("UPDATE alpha_research_trials SET status='REJECTED' WHERE trial_id=?", (trial.trial_id,))

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
            campaign = self.get_campaign(trial.campaign_id)
            used = self.connection.execute(
                "SELECT count(*) FROM alpha_research_events WHERE campaign_id=? AND event_type IN "
                "('TRIAL_EXECUTION_STARTED','TRIAL_RETRY_STARTED')",
                (trial.campaign_id,),
            ).fetchone()[0]
            if used >= campaign.definition.resource_policy.max_execution_attempts:
                raise AlphaError(AlphaErrorCode.RESEARCH_TRIAL_BUDGET_EXCEEDED, "Execution attempt budget exceeded", {"limit": campaign.definition.resource_policy.max_execution_attempts})
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
    def trial_accounting(self, campaign_id: str) -> TrialAccounting:
        self.get_campaign(campaign_id)
        registered = self.connection.execute(
            "SELECT count(*) FROM alpha_research_trials WHERE campaign_id=?", (campaign_id,)
        ).fetchone()[0]
        submitted = self.connection.execute(
            "SELECT count(*) FROM alpha_research_trial_jobs WHERE campaign_id=?", (campaign_id,)
        ).fetchone()[0]
        distinct = self.connection.execute(
            "SELECT count(DISTINCT trial_definition_hash) FROM alpha_research_trials WHERE campaign_id=?",
            (campaign_id,),
        ).fetchone()[0]
        attempts = self.connection.execute(
            "SELECT count(*) FROM alpha_research_events WHERE campaign_id=? AND event_type IN "
            "('TRIAL_EXECUTION_STARTED','TRIAL_RETRY_STARTED')", (campaign_id,),
        ).fetchone()[0]
        completed = self.connection.execute(
            "SELECT count(*) FROM alpha_research_events WHERE campaign_id=? AND event_type='TRIAL_COMPLETED'",
            (campaign_id,),
        ).fetchone()[0]
        exposure = self.connection.execute(
            "SELECT count(DISTINCT trial_id || ':' || job_id || ':' || result_hash) FROM alpha_research_exposures "
            "WHERE trial_id IN (SELECT trial_id FROM alpha_research_trials WHERE campaign_id=?)", (campaign_id,),
        ).fetchone()[0]
        access_requests = self.connection.execute(
            "SELECT coalesce(sum(access_count),0) FROM alpha_research_exposures WHERE trial_id IN "
            "(SELECT trial_id FROM alpha_research_trials WHERE campaign_id=?)", (campaign_id,),
        ).fetchone()[0]
        status_counts = dict(self.connection.execute(
            "SELECT status,count(*) FROM alpha_research_trials WHERE campaign_id=? GROUP BY status", (campaign_id,)
        ).fetchall())
        rejected = self.connection.execute(
            "SELECT count(*) FROM alpha_research_events WHERE campaign_id=? AND event_type='TRIAL_REJECTED'",
            (campaign_id,),
        ).fetchone()[0]
        statistical = self.connection.execute(
            "SELECT count(DISTINCT hypothesis_id) FROM alpha_research_trials WHERE campaign_id=?", (campaign_id,)
        ).fetchone()[0]
        model_fits = sum(self._model_fit_estimate(row[0]) for row in self.connection.execute(
            "SELECT tr.trial_id FROM alpha_research_trials tr JOIN alpha_research_trial_jobs tj ON tj.trial_id=tr.trial_id "
            "JOIN jobs j ON j.id=tj.job_id WHERE tr.campaign_id=? AND j.status='completed'", (campaign_id,)
        ).fetchall())
        return TrialAccounting(
            registered_trial_count=registered,
            submitted_trial_count=submitted,
            distinct_trial_definition_count=distinct,
            execution_attempt_count=attempts,
            completed_evaluation_count=completed,
            result_exposure_count=exposure,
            failed_trial_count=status_counts.get("FAILED", 0),
            cancelled_trial_count=status_counts.get("CANCELLED", 0),
            rejected_submission_count=rejected,
            model_fit_count=model_fits,
            statistical_hypothesis_count=statistical,
            result_access_request_count=access_requests,
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
                metrics = self._read_all_section(binding.job_id, "training_metrics")
            except (ValueError, AlphaError):
                return 0
            baseline = metrics[0].get("baseline_comparison") if metrics else None
            return 2 if baseline else 1
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

    def _trial_terminal_state(self, trial: ResearchTrialRecord) -> ResearchTrialTerminalState:
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
            exclusion = "TRIAL_NOT_SUBMITTED"
        elif job["status"] == "completed":
            if self._job_result(binding.job_id) is None:
                exclusion = "RESULT_EVIDENCE_INVALID"
        elif job["status"] in {"failed", "cancelled"}:
            exclusion = f"JOB_{job['status'].upper()}"
        return ResearchTrialTerminalState(
            trial_id=trial.trial_id, hypothesis_id=trial.hypothesis_id, protocol_hash=trial.protocol_hash,
            trial_definition_hash=trial.trial_definition_hash, trial_purpose=trial.trial_purpose,
            preregistration_status=trial.preregistration_status,
            terminal_state=trial.status, job_id=None if binding is None else binding.job_id,
            job_status=None if job is None else job["status"],
            job_config_hash=None if job is None else job["config_hash"],
            result_hash=None if job is None else job["result_hash"],
            evidence_references=evidence, exclusion_reason=exclusion,
        )

    def _scope_completeness(self, campaign_id: str, integrity: ResearchLedgerIntegrity) -> ScopeCompleteness:
        trials = self.connection.execute(
            "SELECT tr.status,tj.job_id,j.status FROM alpha_research_trials tr "
            "LEFT JOIN alpha_research_trial_jobs tj ON tj.trial_id=tr.trial_id "
            "LEFT JOIN jobs j ON j.id=tj.job_id WHERE tr.campaign_id=?", (campaign_id,),
        ).fetchall()
        complete = all(
            status in TERMINAL_TRIAL_STATES
            and (job_id is None or job_status in TERMINAL_JOB_STATES)
            for status, job_id, job_status in trials
        )
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
        for trial in trials:
            binding = self.get_trial_job(trial.trial_id)
            if binding is None or trial.status not in TERMINAL_TRIAL_STATES:
                raise AlphaError(AlphaErrorCode.RESEARCH_LEDGER_INCOMPLETE, "A registered trial is not terminal", {"trial_id": trial.trial_id, "status": trial.status})
            job = self._job_row(binding.job_id)
            if job is None or job["status"] not in TERMINAL_JOB_STATES:
                raise AlphaError(AlphaErrorCode.RESEARCH_LEDGER_INCOMPLETE, "A managed Job is not terminal", {"trial_id": trial.trial_id})
        terminal_states = [self._trial_terminal_state(trial) for trial in trials]
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
        snapshot = ResearchLedgerSnapshot.model_validate({**snapshot_body, "candidate_ledger_hash": ledger_hash})
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

    def _selection_candidates(self, protocol: ResearchProtocolRecord) -> tuple[list[dict[str, Any]], list[ExcludedCandidate]]:
        candidates: list[dict[str, Any]] = []
        excluded: list[ExcludedCandidate] = []
        policy = protocol.definition.selection_policy
        for trial in self._all_trials(protocol.definition.campaign_id):
            binding = self.get_trial_job(trial.trial_id)
            if binding is None:
                excluded.append(ExcludedCandidate(trial_id=trial.trial_id, reason="TRIAL_NOT_SUBMITTED"))
                continue
            job = self._job_row(binding.job_id)
            if job is None or job["status"] != "completed":
                excluded.append(ExcludedCandidate(trial_id=trial.trial_id, reason=f"JOB_{str(job and job['status']).upper()}"))
                continue
            if trial.canonical_request.get("kind") != "train_alpha_model":
                excluded.append(ExcludedCandidate(trial_id=trial.trial_id, reason="WALK_FORWARD_NOT_A_FREEZEABLE_MODEL"))
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
            training_rows = self._read_all_section(binding.job_id, "training_metrics")
            metrics = training_rows[0] if training_rows else {}
            validation = metrics.get("validation") or {}
            rank_ic = validation.get("rank_ic_mean")
            coverage = validation.get("prediction_coverage")
            if rank_ic is None or not isinstance(rank_ic, (int, float)) or not math.isfinite(float(rank_ic)):
                excluded.append(ExcludedCandidate(trial_id=trial.trial_id, model_id=model_id, reason="PRIMARY_METRIC_NOT_ESTIMABLE"))
                continue
            if policy.minimum_rank_ic is not None and float(rank_ic) < policy.minimum_rank_ic:
                excluded.append(ExcludedCandidate(trial_id=trial.trial_id, model_id=model_id, reason="BELOW_PRIMARY_METRIC_THRESHOLD"))
                continue
            if coverage is None or float(coverage) < policy.minimum_label_coverage:
                excluded.append(ExcludedCandidate(trial_id=trial.trial_id, model_id=model_id, reason="BELOW_COVERAGE_THRESHOLD"))
                continue
            if coverage is None or float(coverage) < policy.minimum_end_to_end_coverage:
                excluded.append(ExcludedCandidate(trial_id=trial.trial_id, model_id=model_id, reason="BELOW_END_TO_END_COVERAGE_THRESHOLD"))
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
            baseline = metrics.get("baseline_comparison")
            if baseline is not None:
                try:
                    from ..models.baseline_comparison import BaselineComparisonReport
                    parsed_baseline = BaselineComparisonReport.model_validate(baseline)
                except Exception as error:  # noqa: BLE001 - malformed persisted evidence is invalid evidence
                    excluded.append(ExcludedCandidate(
                        trial_id=trial.trial_id, model_id=model_id,
                        reason="BASELINE_EVIDENCE_HASH_MISMATCH", detail={"message": str(error)[:512]},
                    ))
                    continue
                if parsed_baseline.comparison_status == "COMPARISON_INVALID":
                    excluded.append(ExcludedCandidate(trial_id=trial.trial_id, model_id=model_id, reason="COMPARISON_INVALID"))
                    continue
                if parsed_baseline.comparison_status == "COMPARISON_INSUFFICIENT_DATA":
                    excluded.append(ExcludedCandidate(trial_id=trial.trial_id, model_id=model_id, reason="COMPARISON_INSUFFICIENT_DATA"))
                    continue
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
                "coverage": float(coverage), "job_id": binding.job_id, "result_hash": job["result_hash"],
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
        snapshot = ResearchLedgerSnapshot.model_validate(_load(row[0]))
        if snapshot_hash_for(snapshot.model_dump(mode="json")) != snapshot.candidate_ledger_hash:
            raise AlphaError(AlphaErrorCode.RESEARCH_EVIDENCE_HASH_MISMATCH, "Candidate Ledger Snapshot hash mismatch", {})
        return snapshot

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
        metrics_rows = self._read_all_section(job_id, "training_metrics")
        metrics = metrics_rows[0] if metrics_rows else {}
        baseline = metrics.get("baseline_comparison")
        if baseline is None:
            return None
        try:
            from ..models.baseline_comparison import BaselineComparisonReport
            report = BaselineComparisonReport.model_validate(baseline)
        except Exception as error:  # noqa: BLE001 - report exposes malformed evidence as invalid
            return {
                "trial_id": trial.trial_id, "job_id": job_id, "status": "INVALID",
                "reason": "BASELINE_EVIDENCE_HASH_MISMATCH", "message": str(error)[:512],
            }
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

    def _data_reuse(self, campaign: ResearchCampaignRecord) -> list[ResearchDataReuseFinding]:
        this_scope = campaign.definition.development_data_scope
        findings: list[ResearchDataReuseFinding] = []
        rows = self.connection.execute(
            "SELECT campaign_id,definition FROM alpha_research_campaigns WHERE campaign_id<>? ORDER BY created_at,campaign_id",
            (campaign.campaign_id,),
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

    def build_evidence_report(
        self,
        *,
        campaign_id: str,
        protocol_hash: str | None = None,
        candidate_ledger_hash: str | None = None,
    ) -> ResearchEvidenceReport:
        campaign = self.get_campaign(campaign_id)
        snapshot = self.get_snapshot(candidate_ledger_hash) if candidate_ledger_hash else self._latest_snapshot(campaign_id, protocol_hash)
        if snapshot is not None:
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
            integrity = self.verify_ledger(campaign_id)
            completeness = self._scope_completeness(campaign_id, integrity)
            watermark = integrity.ledger_watermark
        if protocol_hash is None:
            row = self.connection.execute(
                "SELECT protocol_hash FROM alpha_research_protocols WHERE campaign_id=? ORDER BY created_at DESC LIMIT 1",
                (campaign_id,),
            ).fetchone()
            if row is None:
                raise AlphaError(AlphaErrorCode.RESEARCH_PROTOCOL_NOT_REGISTERED, "Campaign has no Protocol", {})
            protocol_hash = row[0]
        protocol = self.get_protocol(protocol_hash)
        trials = [trial for trial in self._all_trials(campaign_id) if trial.registered_ledger_watermark <= watermark]
        baseline_evidence: list[dict[str, Any]] = []
        walk_forward_evidence: list[dict[str, Any]] = []
        evidence_references: list[dict[str, Any]] = []
        data_quality: list[dict[str, Any]] = []
        for trial in trials:
            binding = self.get_trial_job(trial.trial_id)
            if binding is None:
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
        accounting = snapshot.trial_accounting if snapshot is not None else self.trial_accounting(campaign_id)
        exposures = self.connection.execute(
            "SELECT trial_id,job_id,result_hash,section,first_accessed_at,last_accessed_at,access_count "
            "FROM alpha_research_exposures WHERE trial_id IN (SELECT trial_id FROM alpha_research_trials WHERE campaign_id=?) "
            "AND last_access_event_sequence<=? ORDER BY first_accessed_at,trial_id",
            (campaign_id, watermark),
        ).fetchall()
        exposure_summary = {
            "evaluation_generated_count": accounting.completed_evaluation_count,
            "evaluation_accessed_result_count": accounting.result_exposure_count,
            "access_request_count": accounting.result_access_request_count,
            "exposure_tracking_complete": completeness.exposure_tracking_complete,
            "accesses": [
                {
                    "trial_id": row[0], "job_id": row[1], "result_hash": row[2], "section": row[3],
                    "first_accessed_at": row[4], "last_accessed_at": row[5], "access_count": row[6],
                }
                for row in exposures
            ],
        }
        selection = None
        if snapshot is not None:
            try:
                selection = self.get_selection(snapshot.candidate_ledger_hash)
            except AlphaError:
                selection = None
        hypothesis_summary = []
        for hypothesis in protocol.registered_hypotheses:
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
        body = {
            "campaign_identity": campaign.model_dump(mode="json"),
            "protocol_identity": protocol.model_dump(mode="json"),
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
            "data_reuse_summary": [item.model_dump(mode="json") for item in self._data_reuse(campaign)],
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
        mcp_overhead_bytes: int | None = None,
    ) -> dict[str, Any]:
        with _transaction(self.connection, immediate=False):
            return self._read_research_ledger_view(
                campaign_id=campaign_id, section=section, limit=limit, cursor=cursor,
                mcp_overhead_bytes=mcp_overhead_bytes,
            )

    def _read_research_ledger_view(
        self,
        *,
        campaign_id: str,
        section: str = "summary",
        limit: int = 100,
        cursor: str | None = None,
        mcp_overhead_bytes: int | None = None,
    ) -> dict[str, Any]:
        campaign = self.get_campaign(campaign_id)
        integrity = self.verify_ledger(campaign_id)
        watermark = integrity.ledger_watermark
        snapshot = self._latest_snapshot(campaign_id)
        snapshot_hash = None if snapshot is None else snapshot.candidate_ledger_hash
        identity = canonical_digest({"campaign_id": campaign_id, "watermark": watermark, "snapshot": snapshot_hash})
        sections: dict[str, list[dict[str, Any]]] = {}
        protocols = [
            self.get_protocol(row[0]).model_dump(mode="json")
            for row in self.connection.execute(
                "SELECT protocol_hash FROM alpha_research_protocols WHERE campaign_id=? ORDER BY created_at,protocol_hash",
                (campaign_id,),
            ).fetchall()
        ]
        trials = [trial.model_dump(mode="json") for trial in self._all_trials(campaign_id)]
        events = [event.model_dump(mode="json") for event in self.list_events(campaign_id)]
        hypotheses = [
            self._hypothesis_from_row(item).model_dump(mode="json")
            for item in self.connection.execute(
                "SELECT hypothesis_id,campaign_id,protocol_hash,definition,hypothesis_definition_hash,status,created_at "
                "FROM alpha_research_hypotheses WHERE campaign_id=? ORDER BY created_at,hypothesis_id",
                (campaign_id,),
            ).fetchall()
        ]
        snapshots = [
            self.get_snapshot(row[0]).model_dump(mode="json") for row in self.connection.execute(
                "SELECT candidate_ledger_hash FROM alpha_research_ledger_snapshots WHERE campaign_id=? ORDER BY ledger_watermark,created_at",
                (campaign_id,),
            ).fetchall()
        ]
        selections = [
            ResearchSelection.model_validate(_load(row[0])).model_dump(mode="json")
            for row in self.connection.execute(
                "SELECT payload FROM alpha_research_selections WHERE campaign_id=? ORDER BY created_at,selection_id",
                (campaign_id,),
            ).fetchall()
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
        accounting = self.trial_accounting(campaign_id)
        summary = {
            "schema_version": "alpha-research-experiment-registry-v1",
            "campaign_id": campaign_id,
            "campaign_status": campaign.campaign_status,
            "ledger_watermark": watermark,
            "ledger_integrity": integrity.model_dump(mode="json"),
            "scope_completeness": self._scope_completeness(campaign_id, integrity).model_dump(mode="json"),
            "trial_accounting": accounting.model_dump(mode="json"),
            "latest_protocol_hash": protocols[-1]["protocol_hash"] if protocols else None,
            "latest_candidate_ledger_hash": snapshot_hash,
        }
        if section == "summary":
            return summary
        if section == "evidence":
            rows = [
                {
                    "trial_id": trial.trial_id,
                    "job_id": binding.job_id,
                    "job_status": (job or {}).get("status"),
                    "job_config_hash": (job or {}).get("config_hash"),
                    "result_hash": (job or {}).get("result_hash"),
                    "evidence_scope_hash": trial.evidence_scope_hash,
                }
                for trial in self._all_trials(campaign_id)
                for binding in [self.get_trial_job(trial.trial_id)]
                for job in [None if binding is None else self._job_row(binding.job_id)]
            ]
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

    def read_research_report(
        self,
        *,
        campaign_id: str,
        protocol_hash: str | None = None,
        candidate_ledger_hash: str | None = None,
        section: str = "summary",
        limit: int = 100,
        cursor: str | None = None,
        mcp_overhead_bytes: int | None = None,
    ) -> dict[str, Any]:
        report = self.build_evidence_report(
            campaign_id=campaign_id, protocol_hash=protocol_hash, candidate_ledger_hash=candidate_ledger_hash,
        )
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
        identity = digest({"report_hash": report.report_hash, "section": section})
        return result_page(
            identity, section, report.report_hash, len(rows), limit, cursor,
            lambda offset, size: rows[offset:offset + size],
            resource_uri=f"quant://research-reports/{report.report_hash}/{section}",
            byte_limit=3 * 1024 * 1024,
            metadata={"ledger_watermark": report.ledger_watermark, "report_hash": report.report_hash},
            mcp_overhead_bytes=mcp_overhead_bytes,
        )
