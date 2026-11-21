"""Durable bounded jobs, DB-clock leases and fenced publication.

Workers may outlive the MCP process. Cancellation is a terminal compare-and-set;
a cancelled or expired owner cannot publish even if computation returns later.
"""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sqlite3
import threading
import time
from uuid import uuid4
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, TypeAdapter, ValidationError, model_validator

from .artifacts import ArtifactStore, MAX_RESPONSE_ROW_BYTES
from .compiler import compile_strategy, digest
from .contracts import StrictModel, Timeframe
from .data import MarketBar, Snapshot
from .quality import data_quality, resample_snapshot
from .registry import Registry
from .runtime import Evaluator, ExecutionConfig, run_backtest
from .portfolio import run_portfolio
from .pagination import result_page
from .research import OptimizationRequest, ScreenRequest, validate_research, optimize, screen, candidates
from .provenance import environment
from .python_plans import PythonPlan, compile_python
from .sandbox import PythonArtifact
from .strategy_runtime import run_python_backtest


class BacktestRequest(StrictModel):
    kind: Literal["backtest"] = "backtest"
    plan_id: str | None = None
    data_snapshot_id: str
    symbol: str | None = Field(default=None, min_length=1, max_length=64)
    timeframe: Timeframe | None = None
    portfolio: dict[str, str] | None = Field(default=None, min_length=1, max_length=500)
    config: ExecutionConfig = Field(default_factory=ExecutionConfig)
    start: AwareDatetime | None = None
    end: AwareDatetime | None = None
    random_seed: int = Field(default=0, strict=True)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def interval(self):
        if self.portfolio is not None:
            if any(value is not None for value in (self.plan_id, self.symbol, self.timeframe)):
                raise ValueError("Use portfolio OR plan_id/symbol/timeframe")
            if any(not symbol or len(symbol) > 64 or not plan_id for symbol, plan_id in self.portfolio.items()):
                raise ValueError("Invalid portfolio symbol or plan ID")
        elif not all((self.plan_id, self.symbol, self.timeframe)):
            raise ValueError("plan_id, symbol and timeframe are required for single-asset execution")
        if self.start and self.end and self.start >= self.end:
            raise ValueError("start must be before exclusive end")
        return self


REQUEST_ADAPTER = TypeAdapter(Annotated[BacktestRequest | OptimizationRequest | ScreenRequest, Field(discriminator="kind")])


class OwnershipLost(RuntimeError):
    pass


class JobStore(Registry):
    def __init__(self, path: str | Path):
        super().__init__(path)
        self.path = str(path)
        self.artifacts = ArtifactStore(str(path) + ".artifacts")
        self.connection.executescript('''
            CREATE TABLE IF NOT EXISTS data_snapshots (
                id TEXT PRIMARY KEY, payload TEXT NOT NULL, content_hash TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, status TEXT NOT NULL,
                request TEXT NOT NULL, config_hash TEXT NOT NULL,
                owner TEXT, lease_until INTEGER, attempts INTEGER NOT NULL DEFAULT 0,
                progress REAL NOT NULL DEFAULT 0,
                result TEXT, result_hash TEXT, error_code TEXT, error_message TEXT,
                created_at INTEGER NOT NULL DEFAULT (unixepoch()),
                started_at INTEGER, finished_at INTEGER);
            CREATE TABLE IF NOT EXISTS result_artifacts (
                job_id TEXT NOT NULL REFERENCES jobs(id), section TEXT NOT NULL,
                manifest TEXT NOT NULL, PRIMARY KEY(job_id, section));
            CREATE TABLE IF NOT EXISTS alpha_dataset_publications (
                dataset_id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id));
            CREATE TABLE IF NOT EXISTS job_requests (
                request_key TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id),
                config_hash TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS alpha_research_audit (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                freeze_id TEXT NOT NULL,
                job_id TEXT NOT NULL,
                idempotency_key TEXT,
                attempt INTEGER,
                detail TEXT NOT NULL,
                created_at INTEGER NOT NULL DEFAULT (unixepoch()));
            CREATE INDEX IF NOT EXISTS jobs_claim ON jobs(status, lease_until, created_at);
            CREATE INDEX IF NOT EXISTS alpha_research_audit_freeze
                ON alpha_research_audit(freeze_id, event_id);
        ''')

    def import_snapshot(self, bars: list[dict], calendar_version: str, actions: list[dict] | None = None, calendars: list[dict] | None = None) -> dict:
        if not 1 <= len(bars) <= 10000:
            raise ValueError("RESOURCE_LIMIT_EXCEEDED: expected 1..10000 bars")
        if actions is not None and len(actions) > 5000:
            raise ValueError("RESOURCE_LIMIT_EXCEEDED: too many corporate actions")
        snapshot = Snapshot([MarketBar.model_validate(b) for b in bars], calendar_version=calendar_version, actions=actions, calendars=calendars)
        return self.persist_snapshot(snapshot)

    def persist_snapshot(self, snapshot: Snapshot) -> dict:
        if not 1 <= len(snapshot.bars) <= 10000 or len(snapshot.actions) > 5000:
            raise ValueError("RESOURCE_LIMIT_EXCEEDED: snapshot bar/action limits exceeded")
        calendar_version = snapshot.calendar_version
        summary = {"data_snapshot_id": snapshot.id, "bar_count": len(snapshot.bars), "action_count": len(snapshot.actions),
                   "calendar_version": calendar_version, "calendar_ids": [c.id for c in snapshot.calendars],
                   "lineage": snapshot.lineage.model_dump(mode="json") if snapshot.lineage else None}
        existing = self.connection.execute("SELECT 1 FROM data_snapshots WHERE id=?", (snapshot.id,)).fetchone()
        if existing:
            self.get_snapshot(snapshot.id)
            return summary
        payload = {"storage_version": "parquet-v1", "bars": self.artifacts.write_rows(b.model_dump(mode="json") for b in snapshot.bars),
                   "calendar_version": calendar_version, "actions": self.artifacts.write_rows(a.model_dump(mode="json") for a in snapshot.actions)}
        if snapshot.calendars:
            payload["calendars"] = [c.model_dump(mode="json") for c in snapshot.calendars]
        if snapshot.lineage:
            payload["lineage"] = snapshot.lineage.model_dump(mode="json")
        with self.connection:
            self.connection.execute("INSERT OR IGNORE INTO data_snapshots VALUES (?, ?, ?)", (snapshot.id, json.dumps(payload, allow_nan=False), digest(payload)))
        self.get_snapshot(snapshot.id)
        return summary

    def get_snapshot(self, snapshot_id: str) -> Snapshot:
        row = self.connection.execute("SELECT payload, content_hash FROM data_snapshots WHERE id=?", (snapshot_id,)).fetchone()
        if row is None:
            raise KeyError("Unknown data_snapshot_id")
        payload = json.loads(row[0])
        if digest(payload) != row[1]:
            raise ValueError("Snapshot integrity check failed")
        if payload.get("storage_version") == "parquet-v1":
            bars = self.artifacts.read_rows(payload["bars"], limit=10000)
            actions = self.artifacts.read_rows(payload["actions"], limit=5000)
        else:
            bars, actions = payload["bars"], payload.get("actions", [])
        snapshot = Snapshot([MarketBar.model_validate(b) for b in bars], calendar_version=payload["calendar_version"], actions=actions, calendars=payload.get("calendars"), lineage=payload.get("lineage"))
        if snapshot.id != snapshot_id:
            raise ValueError("Snapshot identity integrity check failed")
        return snapshot

    def quality(self, snapshot_id: str, **arguments) -> dict:
        return data_quality(self.get_snapshot(snapshot_id), **arguments)

    def resample(self, snapshot_id: str, **arguments) -> dict:
        derived, diagnostics = resample_snapshot(self.get_snapshot(snapshot_id), **arguments)
        return {**(self.persist_snapshot(derived) if derived else {"data_snapshot_id": None, "bar_count": 0}),
                "valid": derived is not None, "code": None if derived else "DATA_UNAVAILABLE", "diagnostics": diagnostics}

    def load_plan(self, plan_id):
        persisted = self.get_plan(plan_id)
        row = self.connection.execute("SELECT strategy_id FROM execution_plans WHERE id=?", (plan_id,)).fetchone()
        definition = self.get_strategy(row[0])["definition"]
        plan = (compile_python(definition, symbol=persisted["symbol_binding"]) if persisted.get("mode") == "python"
                else compile_strategy(definition, persisted["parameters"], persisted["symbol_binding"]))
        if json.loads(json.dumps({"plan_id": plan.plan_hash, **plan.to_dict()})) != persisted:
            raise ValueError("Plan semantic integrity check failed")
        return plan

    def evaluate(self, strategy_id: str, snapshot_id: str, as_of: str, *, symbol: str | None = None, parameters: dict | None = None) -> dict:
        now = TypeAdapter(AwareDatetime).validate_python(as_of)
        registered = self.compile(strategy_id, parameters, symbol)
        plan = self.load_plan(registered["plan_id"])
        if isinstance(plan, PythonPlan):
            raise ValueError("UNSUPPORTED_EXECUTION_MODEL: Python signals require a stateful backtest lifecycle")
        snapshot = self.get_snapshot(snapshot_id)
        errors, dependencies = [], []
        for dependency in plan.requirements:
            bars = snapshot.history(dependency["symbol"], dependency["timeframe"], now, dependency["warmup_bars"])
            dependencies.append({**dependency, "available_bars": len(bars), "latest_end_time": bars[-1].end_time.isoformat() if bars else None,
                                 "latest_available_at": bars[-1].available_at.isoformat() if bars else None})
            if len(bars) < dependency["warmup_bars"]:
                errors.append({"code": "INSUFFICIENT_HISTORY" if bars else "DATA_UNAVAILABLE", "dependency": dependency})
        evaluator = Evaluator(plan, snapshot, symbol=symbol)
        grid_dependencies = evaluator.grid_dependencies(now)
        for dependency in grid_dependencies:
            if dependency["available_bars"] < dependency["warmup_bars"]:
                errors.append({"code": "INSUFFICIENT_HISTORY" if dependency["available_bars"] else "DATA_UNAVAILABLE", "grid_dependency": dependency})
        signals = {"entry": evaluator.value(plan.entry.id, now), "exit": evaluator.value(plan.exit.id, now)} if not errors else None
        if signals is not None and any(value is None for value in signals.values()):
            errors.append({"code": "SIGNAL_UNDEFINED", "message": "A condition is undefined despite available history; inspect numerical operations"})
        return {"as_of": now.isoformat(), "strategy_id": strategy_id, "plan_id": plan.plan_hash,
                "data_snapshot_id": snapshot.id, "valid": not errors, "errors": errors,
                "signals": signals, "data_dependencies": dependencies, "grid_dependencies": grid_dependencies,
                "condition_nodes": {"entry": plan.entry.id, "exit": plan.exit.id}}

    def submit(self, raw: dict) -> dict:
        request = REQUEST_ADAPTER.validate_python({"kind": "backtest", **raw})
        snapshot = self.get_snapshot(request.data_snapshot_id)
        if isinstance(request, BacktestRequest):
            execution_plans = request.portfolio or {request.symbol: request.plan_id}
            loaded = []
            for symbol, plan_id in execution_plans.items():
                plan = self.load_plan(plan_id)
                loaded.append(plan)
                if isinstance(plan, PythonPlan) and plan.symbol_binding is not None and plan.symbol_binding != symbol:
                    raise ValueError("UNKNOWN_REFERENCE: Python plan symbol binding differs from execution")
                if request.timeframe and plan.decision_timeframe != request.timeframe:
                    raise ValueError("TIMEFRAME_MISMATCH")
                if any(d["symbol"] == "$symbol" for d in plan.requirements):
                    raise ValueError("UNKNOWN_REFERENCE: compile the strategy with an explicit symbol binding")
                execution_rows = [b for b in snapshot.bars if b.symbol == symbol and b.timeframe == plan.decision_timeframe]
                if execution_rows and any(b.adjustment != "raw" for b in execution_rows):
                    raise ValueError("UNSUPPORTED_EXECUTION_MODEL: broker requires raw prices; provider basis is adjusted or unverified")
                if not execution_rows:
                    raise ValueError("DATA_UNAVAILABLE: missing execution series")
            if any(isinstance(p, PythonPlan) for p in loaded):
                if not all(isinstance(p, PythonPlan) for p in loaded) or len({p.execution_identity for p in loaded}) != 1 or request.config.signal_backend != "pit":
                    raise ValueError("UNSUPPORTED_EXECUTION_MODEL: a Python job uses one shared artifact and the event/PIT path")
            if request.config.fill_policy != "next_bar_open":
                raise ValueError("UNSUPPORTED_EXECUTION_MODEL")
        else:
            validate_research(request, self.get_strategy(request.strategy_id)["definition"])
        payload = request.model_dump(mode="json")
        job_id = {"backtest": "bt_", "optimize": "opt_", "screen": "screen_"}[request.kind] + uuid4().hex
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            if request.idempotency_key:
                previous = self.connection.execute("SELECT job_id,config_hash FROM job_requests WHERE request_key=?", (request.idempotency_key,)).fetchone()
                if previous:
                    if previous[1] != digest(payload):
                        raise ValueError("IDEMPOTENCY_CONFLICT: key already identifies another configuration")
                    self.connection.commit()
                    return self.get_job(previous[0])
            count = self.connection.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]
            if count >= 32:
                raise ValueError("RESOURCE_LIMIT_EXCEEDED: job queue is full")
            self.connection.execute("INSERT INTO jobs(id,status,request,config_hash) VALUES (?, 'queued', ?, ?)", (job_id, json.dumps(payload, allow_nan=False), digest(payload)))
            if request.idempotency_key:
                self.connection.execute("INSERT INTO job_requests VALUES (?, ?, ?)", (request.idempotency_key, job_id, digest(payload)))
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        return self.get_job(job_id)

    def _record_alpha_research_audit(
        self,
        *,
        event_type: str,
        freeze_id: str,
        job_id: str,
        idempotency_key: str | None = None,
        attempt: int | None = None,
        detail: dict | None = None,
    ) -> None:
        self.connection.execute(
            "INSERT INTO alpha_research_audit(event_type,freeze_id,job_id,idempotency_key,attempt,detail) VALUES (?,?,?,?,?,?)",
            (
                event_type,
                freeze_id,
                job_id,
                idempotency_key,
                attempt,
                json.dumps(detail or {}, sort_keys=True, allow_nan=False),
            ),
        )

    def list_alpha_research_audit(self, *, freeze_id: str | None = None) -> list[dict]:
        if freeze_id is None:
            rows = self.connection.execute(
                "SELECT event_id,event_type,freeze_id,job_id,idempotency_key,attempt,detail,created_at "
                "FROM alpha_research_audit ORDER BY event_id"
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT event_id,event_type,freeze_id,job_id,idempotency_key,attempt,detail,created_at "
                "FROM alpha_research_audit WHERE freeze_id=? ORDER BY event_id",
                (freeze_id,),
            ).fetchall()
        return [
            {
                "event_id": row[0], "event_type": row[1], "freeze_id": row[2], "job_id": row[3],
                "idempotency_key": row[4], "attempt": row[5], "detail": json.loads(row[6]), "created_at": row[7],
            }
            for row in rows
        ]

    def completed_freeze_job(self, freeze_id: str) -> dict | None:
        rows = self.connection.execute(
            "SELECT id,request FROM jobs WHERE status='completed' ORDER BY finished_at,id"
        ).fetchall()
        for job_id, encoded_request in rows:
            request_payload = json.loads(encoded_request)
            if request_payload.get("kind") != "freeze_alpha_selection":
                continue
            try:
                summary = self.get_result(job_id, "summary")
            except ValueError:
                continue
            if isinstance(summary, dict) and summary.get("freeze_id") == freeze_id:
                return {"job_id": job_id, "freeze_id": freeze_id, "record": summary}
        return None

    def audit_training_rejection(self, source: str, fields: list[str]) -> dict:
        with self.connection:
            self._record_alpha_research_audit(
                event_type="TRAINING_SUBMISSION_REJECTED", freeze_id="", job_id="",
                detail={"source": source, "fields": sorted({field[:128] for field in fields[:20]})},
            )
        return {"recorded": True}

    def _validate_signal_datasets(self, request) -> None:
        from .alpha.data.catalog import DatasetStore
        from .alpha.jobs.requests import RESERVED_TEST_START
        from .alpha.models.diagnostic_protocol import RESERVED_TEST_END, validate_frozen_membership
        datasets = DatasetStore(Path(self.path).with_suffix(".alpha"), defer_registration=True)
        try:
            stock = datasets.load_manifest(request.stock_dataset_id)
        except KeyError as error:
            raise ValueError("evaluate_alpha_signal missing stock dataset") from error
        try:
            feature = datasets.load_manifest(request.feature_set_id)
        except KeyError as error:
            raise ValueError("evaluate_alpha_signal missing feature dataset") from error
        if stock.dataset_kind != "stock_bars":
            raise ValueError("evaluate_alpha_signal stock_dataset_id must identify stock_bars")
        if feature.dataset_kind != "feature_panel":
            raise ValueError("evaluate_alpha_signal feature_set_id must identify feature_panel")
        if stock.start_at < RESERVED_TEST_END and stock.end_at > RESERVED_TEST_START:
            raise ValueError("evaluate_alpha_signal stock dataset physical coverage enters reserved Test")
        if feature.start_at < RESERVED_TEST_END and feature.end_at > RESERVED_TEST_START:
            raise ValueError("evaluate_alpha_signal feature dataset physical coverage enters reserved Test")
        if stock.dataset_id not in feature.parent_dataset_ids:
            raise ValueError("evaluate_alpha_signal feature set is not derived from the stock dataset")
        if request.protocol_mode == "frozen_v1_3":
            try:
                validate_frozen_membership(feature.instrument_ids)
                validate_frozen_membership(stock.instrument_ids)
            except ValueError as error:
                raise ValueError(f"frozen_v1_3 membership mismatch: {error}") from error

    def _validate_training_datasets(self, request) -> None:
        from .alpha.data.catalog import DatasetStore
        from .alpha.data.session_coverage import mapped_stock_calendar
        from .alpha.jobs.requests import RESERVED_TEST_START
        from .alpha.models.diagnostic_protocol import RESERVED_TEST_END, validate_frozen_membership

        is_validation_training = request.kind == "train_alpha_model" and request.evaluation_mode == "validation_only"
        is_fold_training = request.kind == "train_alpha_diagnostic_folds"
        if not (is_validation_training or is_fold_training):
            return
        datasets = DatasetStore(Path(self.path).with_suffix(".alpha"), defer_registration=True)
        try:
            feature = datasets.load_manifest(request.feature_set_id)
        except KeyError as error:
            raise ValueError("diagnostic training missing feature dataset") from error
        stock_dataset_id = request.stock_dataset_id
        try:
            stock = datasets.load_manifest(stock_dataset_id)
        except KeyError as error:
            raise ValueError("diagnostic training missing stock dataset") from error
        if feature.dataset_kind != "feature_panel" or stock.dataset_kind != "stock_bars":
            raise ValueError("diagnostic training dataset kinds are invalid")
        if any(
            manifest.start_at < RESERVED_TEST_END and manifest.end_at > RESERVED_TEST_START
            for manifest in (feature, stock)
        ):
            raise ValueError("diagnostic training dataset physical coverage enters reserved Test")
        if stock.dataset_id not in feature.parent_dataset_ids:
            raise ValueError("diagnostic training feature set is not derived from the stock dataset")
        if any(
            sum(partition.row_count for partition in manifest.partition_manifest) > request.resource_budget.max_rows
            for manifest in (feature, stock)
        ):
            raise ValueError("diagnostic training read rows exceed budget")
        if request.protocol_mode == "frozen_v1_3":
            try:
                validate_frozen_membership(feature.instrument_ids)
                validate_frozen_membership(stock.instrument_ids)
            except ValueError as error:
                raise ValueError(f"frozen_v1_3 membership mismatch: {error}") from error
            if mapped_stock_calendar(stock) is None:
                raise ValueError("frozen_v1_3 requires a mapped calendar")

    def submit_alpha(self, raw: dict) -> dict:
        try:
            from .alpha.jobs.requests import ALPHA_REQUEST_ADAPTER
        except ImportError as error:
            raise ValueError("DATA_UNAVAILABLE: Alpha optional dependencies are not installed") from error
        try:
            request = ALPHA_REQUEST_ADAPTER.validate_python(raw)
        except ValidationError as error:
            if isinstance(raw, dict) and raw.get("kind") == "train_alpha_model":
                fields = [".".join(map(str, issue["loc"][1:])) or "request"
                          for issue in error.errors(include_input=False)]
                self.audit_training_rejection("runtime", fields)
                raise ValueError(f"Invalid Alpha training fields: {', '.join(fields[:20])}") from error
            raise
        if request.kind == "evaluate_alpha_signal":
            self._validate_signal_datasets(request)
        if request.kind in {"train_alpha_model", "train_alpha_diagnostic_folds"}:
            self._validate_training_datasets(request)
        payload = request.model_dump(mode="json")
        job_id = "alpha_" + uuid4().hex
        is_oos = request.kind == "evaluate_alpha_oos"
        freeze_id = getattr(request, "freeze_id", None)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            if request.idempotency_key:
                previous = self.connection.execute("SELECT job_id,config_hash FROM job_requests WHERE request_key=?", (request.idempotency_key,)).fetchone()
                if previous:
                    if previous[1] != digest(payload):
                        raise ValueError("IDEMPOTENCY_CONFLICT: key already identifies another configuration")
                    if is_oos:
                        self._record_alpha_research_audit(
                            event_type="OOS_REPEAT_REQUEST", freeze_id=freeze_id, job_id=previous[0],
                            idempotency_key=request.idempotency_key, detail={"config_hash": previous[1]},
                        )
                    self.connection.commit()
                    job = self.get_job(previous[0])
                    return {**job, "job_type": "alpha_" + request.kind, "schema_version": "1.0"}
            if is_oos:
                existing_oos = None
                for existing_id, encoded_request, existing_hash in self.connection.execute(
                    "SELECT id,request,config_hash FROM jobs ORDER BY created_at,id"
                ):
                    existing_request = json.loads(encoded_request)
                    if existing_request.get("kind") == "evaluate_alpha_oos" and existing_request.get("freeze_id") == freeze_id:
                        existing_oos = (existing_id, existing_hash)
                        break
                if existing_oos is not None:
                    self._record_alpha_research_audit(
                        event_type="OOS_REPEAT_REQUEST", freeze_id=freeze_id, job_id=existing_oos[0],
                        idempotency_key=request.idempotency_key, detail={"config_hash": existing_oos[1]},
                    )
                    self.connection.commit()
                    job = self.get_job(existing_oos[0])
                    return {**job, "job_type": "alpha_" + request.kind, "schema_version": "1.0"}
            count = self.connection.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]
            if count >= 32:
                raise ValueError("RESOURCE_LIMIT_EXCEEDED: job queue is full")
            self.connection.execute("INSERT INTO jobs(id,status,request,config_hash) VALUES (?, 'queued', ?, ?)", (job_id, json.dumps(payload, allow_nan=False), digest(payload)))
            if request.idempotency_key:
                self.connection.execute("INSERT INTO job_requests VALUES (?, ?, ?)", (request.idempotency_key, job_id, digest(payload)))
            if is_oos:
                self._record_alpha_research_audit(
                    event_type="OOS_SUBMISSION", freeze_id=freeze_id, job_id=job_id,
                    idempotency_key=request.idempotency_key, detail={"config_hash": digest(payload)},
                )
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        job = self.get_job(job_id)
        return {**job, "job_type": "alpha_" + request.kind, "schema_version": "1.0"}

    def claim(self) -> dict | None:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            exhausted = self.connection.execute(
                "SELECT id,request,attempts FROM jobs "
                "WHERE status IN ('running','publishing') AND lease_until<=unixepoch() AND attempts>=3"
            ).fetchall()
            for exhausted_id, encoded_request, attempt in exhausted:
                request_payload = json.loads(encoded_request)
                if request_payload.get("kind") == "evaluate_alpha_oos":
                    self._record_alpha_research_audit(
                        event_type="OOS_FAILED", freeze_id=request_payload["freeze_id"],
                        job_id=exhausted_id, idempotency_key=request_payload.get("idempotency_key"),
                        attempt=attempt, detail={"status": "failed", "error_code": "WORKER_LOST"},
                    )
            self.connection.execute("UPDATE jobs SET status='failed',error_code='WORKER_LOST',finished_at=unixepoch() WHERE status IN ('running','publishing') AND lease_until<=unixepoch() AND attempts>=3")
            active = self.connection.execute("SELECT count(*) FROM jobs WHERE status IN ('running','publishing') AND lease_until>unixepoch()").fetchone()[0]
            row = None if active >= 4 else self.connection.execute("SELECT id,request,config_hash FROM jobs WHERE status='queued' OR (status IN ('running','publishing') AND lease_until<=unixepoch() AND attempts<3) ORDER BY created_at,id LIMIT 1").fetchone()
            if row is None:
                self.connection.commit()
                return None
            owner = uuid4().hex
            self.connection.execute("UPDATE jobs SET status='running',owner=?,lease_until=unixepoch()+15,attempts=attempts+1,started_at=coalesce(started_at,unixepoch()),progress=0 WHERE id=?", (owner, row[0]))
            request_payload = json.loads(row[1])
            if request_payload.get("kind") == "evaluate_alpha_oos":
                attempt = self.connection.execute("SELECT attempts FROM jobs WHERE id=?", (row[0],)).fetchone()[0]
                self._record_alpha_research_audit(
                    event_type="OOS_EXECUTION_ATTEMPT", freeze_id=request_payload["freeze_id"],
                    job_id=row[0], idempotency_key=request_payload.get("idempotency_key"),
                    attempt=attempt, detail={"owner": owner, "config_hash": row[2]},
                )
            self.connection.commit()
            return {"id": row[0], "owner": owner, "request": request_payload, "config_hash": row[2]}
        except BaseException:
            self.connection.rollback()
            raise

    def heartbeat(self, claim: dict, progress=0.) -> bool:
        with self.connection:
            changed = self.connection.execute("UPDATE jobs SET lease_until=unixepoch()+15,progress=? WHERE id=? AND owner=? AND status IN ('running','publishing') AND lease_until>unixepoch()", (min(.99, max(0., progress)), claim["id"], claim["owner"]))
        return changed.rowcount == 1

    def _serialize_result(self, claim: dict, result: dict) -> tuple[dict, dict]:
        references = {}
        def checkpoint():
            if not self.heartbeat(claim, .99):
                raise OwnershipLost()
        compact = {}
        for section, value in result.items():
            if isinstance(value, list):
                references[section] = self.artifacts.write_rows(iter(value), checkpoint=checkpoint, enforce_mcp_budget=True)
                compact[section] = {"$artifact": references[section]}
            else:
                compact[section] = value
        if len(json.dumps(compact, allow_nan=False).encode()) > 16777216:
            raise ValueError("RESOURCE_LIMIT_EXCEEDED: result metadata exceeds 16 MiB")
        return compact, references

    def begin_publish(self, claim: dict, result: dict) -> bool:
        """Persist result artifacts and enter an internal fenced publishing phase."""
        if not self.connection.execute("SELECT 1 FROM jobs WHERE id=? AND owner=? AND status='running' AND lease_until>unixepoch()", (claim["id"], claim["owner"])).fetchone():
            return False
        try:
            compact, references = self._serialize_result(claim, result)
        except OwnershipLost:
            return False
        except Exception as error:
            self.fail_publish(claim, error)
            return False
        encoded = json.dumps(compact, allow_nan=False)
        with self.connection:
            changed = self.connection.execute(
                "UPDATE jobs SET status='publishing',progress=.99,result=?,result_hash=?,error_code=NULL,error_message=NULL,lease_until=unixepoch()+15 WHERE id=? AND owner=? AND status='running' AND lease_until>unixepoch()",
                (encoded, digest(compact), claim["id"], claim["owner"]),
            )
            if changed.rowcount == 1:
                self.connection.execute("DELETE FROM result_artifacts WHERE job_id=?", (claim["id"],))
                for section, reference in references.items():
                    self.connection.execute("INSERT INTO result_artifacts VALUES (?, ?, ?)", (claim["id"], section, json.dumps(reference, allow_nan=False)))
        return changed.rowcount == 1

    def complete_publish(self, claim: dict, *, dataset_ids=()) -> bool:
        with self.connection:
            changed = self.connection.execute(
                "UPDATE jobs SET status='completed',progress=1.,finished_at=unixepoch(),lease_until=NULL WHERE id=? AND owner=? AND status='publishing' AND lease_until>unixepoch()",
                (claim["id"], claim["owner"]),
            )
            if changed.rowcount == 1:
                self.connection.executemany(
                    "INSERT OR IGNORE INTO alpha_dataset_publications VALUES (?, ?)",
                    [(dataset_id, claim["id"]) for dataset_id in dataset_ids],
                )
            if changed.rowcount == 1 and claim["request"].get("kind") == "evaluate_alpha_oos":
                row = self.connection.execute("SELECT attempts FROM jobs WHERE id=?", (claim["id"],)).fetchone()
                self._record_alpha_research_audit(
                    event_type="OOS_COMPLETED", freeze_id=claim["request"]["freeze_id"],
                    job_id=claim["id"], idempotency_key=claim["request"].get("idempotency_key"),
                    attempt=row[0] if row else None, detail={"status": "completed"},
                )
        return changed.rowcount == 1

    def fail_publish(self, claim: dict, error: Exception, error_code: str | None = None) -> bool:
        with self.connection:
            changed = self.connection.execute(
                "UPDATE jobs SET status='failed',progress=0.,error_code=?,error_message=?,finished_at=unixepoch(),lease_until=NULL WHERE id=? AND owner=? AND status IN ('running','publishing') AND lease_until>unixepoch()",
                (error_code or "EXECUTION_FAILED", str(error)[:4096], claim["id"], claim["owner"]),
            )
            if changed.rowcount == 1 and claim["request"].get("kind") == "evaluate_alpha_oos":
                row = self.connection.execute("SELECT attempts FROM jobs WHERE id=?", (claim["id"],)).fetchone()
                self._record_alpha_research_audit(
                    event_type="OOS_FAILED", freeze_id=claim["request"]["freeze_id"],
                    job_id=claim["id"], idempotency_key=claim["request"].get("idempotency_key"),
                    attempt=row[0] if row else None,
                    detail={"status": "failed", "error_code": error_code or "EXECUTION_FAILED"},
                )
        return changed.rowcount == 1

    def finish(self, claim: dict, result: dict | None = None, error: Exception | None = None, error_code: str | None = None) -> bool:
        if not self.connection.execute("SELECT 1 FROM jobs WHERE id=? AND owner=? AND status='running' AND lease_until>unixepoch()", (claim["id"], claim["owner"])).fetchone():
            return False
        references = {}
        try:
            if result is not None:
                result, references = self._serialize_result(claim, result)
        except OwnershipLost:
            return False
        except Exception as serialization_error:
            error, result, references = serialization_error, None, {}
        status = "failed" if error else "completed"
        encoded = json.dumps(result, allow_nan=False) if result is not None else None
        with self.connection:
            changed = self.connection.execute("UPDATE jobs SET status=?,progress=?,result=?,result_hash=?,error_code=?,error_message=?,finished_at=unixepoch(),lease_until=NULL WHERE id=? AND owner=? AND status='running' AND lease_until>unixepoch()",
                (status, 1. if not error else 0., encoded, digest(result) if result is not None else None,
                 (error_code or "EXECUTION_FAILED") if error else None, str(error)[:4096] if error else None, claim["id"], claim["owner"]))
            if changed.rowcount == 1 and not error:
                for section, reference in references.items():
                    self.connection.execute("INSERT INTO result_artifacts VALUES (?, ?, ?)", (claim["id"], section, json.dumps(reference, allow_nan=False)))
            if changed.rowcount == 1 and claim["request"].get("kind") == "evaluate_alpha_oos":
                row = self.connection.execute("SELECT attempts FROM jobs WHERE id=?", (claim["id"],)).fetchone()
                self._record_alpha_research_audit(
                    event_type="OOS_FAILED" if error else "OOS_COMPLETED",
                    freeze_id=claim["request"]["freeze_id"], job_id=claim["id"],
                    idempotency_key=claim["request"].get("idempotency_key"),
                    attempt=row[0] if row else None,
                    detail={"status": "failed" if error else "completed", "error_code": error_code} if error else {"status": "completed"},
                )
        return changed.rowcount == 1

    def get_job(self, job_id: str) -> dict:
        row = self.connection.execute("SELECT id,status,progress,error_code,error_message,attempts,created_at,started_at,finished_at FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError("Unknown job_id")
        result = dict(zip(("job_id", "status", "progress", "error_code", "error_message", "attempts", "created_at", "started_at", "finished_at"), row))
        if result["status"] == "publishing":
            result["status"] = "running"
            result["phase"] = "publishing"
        return result

    def cancel(self, job_id: str) -> dict:
        with self.connection:
            row = self.connection.execute("SELECT request FROM jobs WHERE id=?", (job_id,)).fetchone()
            changed = self.connection.execute(
                "UPDATE jobs SET status='cancelled',finished_at=unixepoch(),lease_until=NULL "
                "WHERE id=? AND status IN ('queued','running','publishing')",
                (job_id,),
            )
            if changed.rowcount == 1 and row is not None:
                request_payload = json.loads(row[0])
                if request_payload.get("kind") == "evaluate_alpha_oos":
                    self._record_alpha_research_audit(
                        event_type="OOS_CANCELLED", freeze_id=request_payload["freeze_id"],
                        job_id=job_id, idempotency_key=request_payload.get("idempotency_key"),
                        detail={"status": "cancelled"},
                    )
        return self.get_job(job_id)

    def get_result(self, job_id: str, section="summary", limit=100, cursor=None, *, mcp_overhead_bytes=None) -> dict:
        sections = ("artifacts", "analytics", "attribution", "daily_returns", "summary", "orders", "fills", "equity_curve", "reproducibility", "final_account", "diagnostics", "cash_ledger", "corporate_actions", "exposures", "risk_events", "trials", "walk_forward", "parameter_sensitivity", "screen", "dataset", "data_quality", "factors", "factor_diagnostics", "option_analytics", "training_metrics", "model_registry", "predictions", "rank_ic", "rank_ic_by_decision", "weekly_rank_ic", "top20_forward_labels", "portfolio_metrics", "target_weights", "trades")
        if section not in sections or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Invalid result section or limit")
        job = self.get_job(job_id)
        if job["status"] != "completed":
            raise ValueError("RESULT_NOT_READY: job must be completed")
        row = self.connection.execute("SELECT result,result_hash FROM jobs WHERE id=?", (job_id,)).fetchone()
        result = json.loads(row[0])
        if digest(result) != row[1]:
            raise ValueError("Result integrity check failed")
        expected_catalog = {name: value["$artifact"] for name, value in result.items()
                            if isinstance(value, dict) and set(value) == {"$artifact"}}
        actual_catalog = {name: json.loads(manifest) for name, manifest in self.connection.execute(
            "SELECT section,manifest FROM result_artifacts WHERE job_id=?", (job_id,))}
        if actual_catalog != expected_catalog:
            raise ValueError("Artifact catalog integrity check failed")
        if section == "artifacts":
            result[section] = [{"section": name, **value["$artifact"], "resource_uri": f"quant://results/{job_id}/{name}"}
                               for name, value in result.items() if isinstance(value, dict) and set(value) == {"$artifact"}]
        if section not in result:
            raise ValueError("RESULT_SECTION_UNAVAILABLE: section does not belong to this job type")
        value = result[section]
        reference = value["$artifact"] if isinstance(value, dict) and set(value) == {"$artifact"} else None
        if reference is None and not isinstance(value, list):
            if cursor:
                raise ValueError("Object sections do not accept cursors")
            return value
        count = reference["row_count"] if reference else len(value)
        def read_rows(offset, size):
            if reference:
                return self.artifacts.read_rows(reference, offset, size, byte_limit=MAX_RESPONSE_ROW_BYTES)
            return value[offset:offset + size]
        return result_page(job_id, section, row[1], count, limit, None if cursor == "" else cursor, read_rows,
                           resource_uri=f"quant://results/{job_id}/{section}", byte_limit=MAX_RESPONSE_ROW_BYTES,
                           mcp_overhead_bytes=mcp_overhead_bytes)



def execute_job(store: JobStore, claim: dict) -> bool:
    try:
        from .alpha.jobs.requests import ALPHA_KINDS
        if claim["request"].get("kind") in ALPHA_KINDS:
            try:
                from .alpha.jobs.executor import execute_alpha_job
            except ImportError as error:
                return store.finish(claim, error=ValueError("DATA_UNAVAILABLE: Alpha optional dependencies are not installed"), error_code="DATA_UNAVAILABLE")
            if not store.heartbeat(claim):
                return False
            heartbeat_stop = threading.Event()
            def alpha_heartbeat():
                connection = sqlite3.connect(store.path, timeout=30)
                connection.execute("PRAGMA busy_timeout=30000")
                try:
                    while not heartbeat_stop.wait(1.0):
                        row = connection.execute(
                            "SELECT 1 FROM jobs WHERE id=? AND owner=? AND status IN ('running','publishing') AND lease_until>unixepoch()",
                            (claim["id"], claim["owner"]),
                        ).fetchone()
                        if row is None:
                            break
                        connection.execute(
                            "UPDATE jobs SET lease_until=unixepoch()+15 WHERE id=? AND owner=? AND status IN ('running','publishing') AND lease_until>unixepoch()",
                            (claim["id"], claim["owner"]),
                        )
                        connection.commit()
                finally:
                    connection.close()
            heartbeat_thread = threading.Thread(target=alpha_heartbeat, name="alpha-job-heartbeat", daemon=True)
            heartbeat_thread.start()
            began = time.monotonic()
            last_check = 0.0
            def alpha_checkpoint(progress):
                nonlocal last_check
                now = time.monotonic()
                if now - last_check >= 0.25:
                    if not store.heartbeat(claim, progress):
                        raise OwnershipLost()
                    last_check = now
            try:
                result = execute_alpha_job(store, claim, alpha_checkpoint)
                return result
            except OwnershipLost:
                return False
            finally:
                heartbeat_stop.set()
                heartbeat_thread.join(timeout=2)
        request = REQUEST_ADAPTER.validate_python(claim["request"])
        if digest(request.model_dump(mode="json")) != claim["config_hash"]:
            raise ValueError("Job configuration integrity check failed")
        if not store.heartbeat(claim):
            return False
        snapshot = store.get_snapshot(request.data_snapshot_id)
        began = time.monotonic()
        last_check = 0.
        def checkpoint(progress):
            nonlocal last_check
            now = time.monotonic()
            if now - began > 120:
                raise TimeoutError("RESOURCE_LIMIT_EXCEEDED: backtest exceeded 120 seconds")
            if now - last_check >= .25:
                if not store.heartbeat(claim, progress):
                    raise OwnershipLost()
                last_check = now
        if isinstance(request, BacktestRequest):
            plans = {symbol: store.load_plan(plan_id) for symbol, plan_id in (request.portfolio or {request.symbol: request.plan_id}).items()}
            python_plans = [p for p in plans.values() if isinstance(p, PythonPlan)]
            if python_plans:
                if len(python_plans) != len(plans) or len({p.execution_identity for p in python_plans}) != 1:
                    raise ValueError("UNSUPPORTED_EXECUTION_MODEL: mixed Python/DSL portfolio")
                plan = python_plans[0]
                artifact = PythonArtifact.model_validate(store.get_strategy(plan.strategy_hash)["definition"])
                result = run_python_backtest(artifact, snapshot, symbols=list(plans), timeframe=plan.decision_timeframe, image=plan.sandbox_image,
                                            config=request.config, start=request.start, end=request.end, checkpoint=checkpoint, runtime=plan.sandbox_runtime,
                                            random_seed=request.random_seed)
                result["reproducibility"].update(plan_hashes={s: p.plan_hash for s, p in plans.items()}, sandbox_protocol=plan.protocol_version)
            elif request.portfolio:
                result = run_portfolio(plans, snapshot, config=request.config, start=request.start, end=request.end, checkpoint=checkpoint)
            else:
                result = run_backtest(plans[request.symbol], snapshot, symbol=request.symbol, timeframe=request.timeframe, config=request.config, start=request.start, end=request.end, checkpoint=checkpoint)
        else:
            definition = store.get_strategy(request.strategy_id)["definition"]
            result = optimize(request, definition, snapshot, checkpoint) if isinstance(request, OptimizationRequest) else screen(request, definition, snapshot, checkpoint)
            # A result's selected/trial/screen plan IDs must be executable
            # references, not ephemeral hashes that disappear with the worker.
            bindings = [(parameters, request.symbol) for parameters in candidates(request)] if isinstance(request, OptimizationRequest) else [(request.parameters, row["symbol"]) for row in result["screen"]]
            for parameters, symbol in bindings:
                checkpoint(.99)
                store.compile(request.strategy_id, parameters, symbol)
            result["reproducibility"]["environment"] = environment()
        result["reproducibility"]["random_seed"] = request.random_seed
        result["reproducibility"]["job_config_hash"] = claim["config_hash"]
        return store.finish(claim, result)
    except OwnershipLost:
        return False
    except Exception as error:
        return store.finish(claim, error=error)
