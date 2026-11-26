#!/usr/bin/env python3
"""PRODUCTION_RESEARCH_CAPACITY_PROFILE_V1 isolated synthetic acceptance run."""
from __future__ import annotations

import argparse
import gc
import json
import resource
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import numpy as np
import pyarrow as pa
from stock_quant.alpha.contracts import StockDatasetRequest
from stock_quant.alpha.data.catalog import DatasetStore
from stock_quant.alpha.factors.ast import FactorNode, FactorSpec
from stock_quant.alpha.factors.compiler import compile_factor_spec
from stock_quant.alpha.jobs import executor as alpha_executor
from stock_quant.alpha.models import baseline_comparison as comparison
from stock_quant.alpha.models import baseline_evidence
from stock_quant.alpha.models import development_cohort, lightgbm_trainer
from stock_quant.alpha.models.baseline_comparison import BaselineComparisonConfig
from stock_quant.alpha.models.baseline_evidence import BaselineEvidenceLoader, SECTION
from stock_quant.alpha.models.lightgbm_trainer import AlphaModelRegistry
from stock_quant.compiler import digest
from stock_quant.jobs import JobStore, execute_job
from stock_quant.pagination import _envelope_sizes

SYMBOLS = 128
DAYS = 4_532
TRAIN_DECISIONS = 3_007
VALIDATION_DECISIONS = 1_501
FACTORS = 18
HORIZON = 5
MAX_ROWS = 25_000_000
MAX_SECONDS = 1_800
EXPECTED_STOCK_ROWS = SYMBOLS * DAYS
EXPECTED_FEATURE_ROWS = EXPECTED_STOCK_ROWS * FACTORS
EXPECTED_TRAIN_SAMPLES = TRAIN_DECISIONS * SYMBOLS
EXPECTED_VALIDATION_SAMPLES = VALIDATION_DECISIONS * SYMBOLS
START = datetime(2003, 1, 2, tzinfo=UTC)
TIMES: dict[str, float] = {}


def _rss_kib():
    with open("/proc/self/status", encoding="ascii") as stream:
        for line in stream:
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    return 0


class RssSampler:
    def __init__(self):
        self.max_kib = _rss_kib()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self.stop.wait(0.05):
            self.max_kib = max(self.max_kib, _rss_kib())

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join(timeout=1)
        self.max_kib = max(self.max_kib, _rss_kib())


@contextmanager
def timed(name):
    started = time.monotonic()
    try:
        yield
    finally:
        TIMES[name] = round(TIMES.get(name, 0.0) + time.monotonic() - started, 6)


def factor_spec() -> FactorSpec:
    nodes = [
        FactorNode(id="reversal_source", operator="pct_change", inputs=["close"], params={"period": 5}),
        FactorNode(id="rsi_source", operator="rsi", inputs=["close"], params={"period": 5}),
    ]
    outputs = ["reversal_source", "rsi_source"]
    for index in range(3, FACTORS + 1):
        name = f"f{index:02d}"
        nodes.append(FactorNode(id=name, operator="rolling_mean", inputs=["close"], params={"period": 5 + index}))
        outputs.append(name)
    return FactorSpec(schema_version="1.0", timeframe="1d", nodes=nodes, outputs=outputs)


def campaign_payload(protocol_hash: str):
    from stock_quant.alpha.research.protocol import protocol_hash_for
    base = {
        "campaign_name": "PRODUCTION_RESEARCH_CAPACITY_PROFILE_V1",
        "research_objective": "Exercise the production Alpha capacity path with synthetic data only.",
        "research_scope": {"synthetic": True, "capacity_profile": "PRODUCTION_RESEARCH_CAPACITY_PROFILE_V1"},
        "development_data_scope": {
            "asset_class": "equity", "market": "US",
            "universe_definition": {"symbols": [f"S{i:03d}" for i in range(SYMBOLS)]},
            "development_start": START.isoformat().replace("+00:00", "Z"),
            "development_end": (START + timedelta(days=DAYS)).isoformat().replace("+00:00", "Z"),
            "data_provider": "synthetic-capacity-fixture",
            "stock_dataset_ids": ["sha256:" + "0" * 64],
            "feature_dataset_ids": ["sha256:" + "0" * 64],
            "label_definitions": [{"label_name": "stock_forward_return", "horizon_days": HORIZON}],
        },
        "resource_policy": {
            "max_registered_trials": 4, "max_submitted_trials": 4,
            "max_distinct_candidate_definitions": 4, "max_evaluation_jobs": 4,
            "max_execution_attempts": 8, "max_protocol_revisions": 2,
        },
    }
    return base


def protocol_payload(campaign_id: str, stock_id: str, feature_id: str, spec: FactorSpec):
    return {
        "campaign_id": campaign_id,
        "hypothesis_definitions": [{
            "hypothesis_key": "capacity_four_candidate",
            "title": "Synthetic four-candidate capacity",
            "economic_rationale": "Synthetic capacity verification only.",
            "expected_direction": "positive",
            "target_variable": "stock_forward_return",
            "factor_spec_hash": None,
            "primary_metric": "rank_ic",
            "null_hypothesis": "Synthetic capacity candidates have no positive rank IC.",
            "comparison_family": ["lightgbm_vs_ridge"],
        }],
        "candidate_definitions": [{"seed": 7}],
        "allowed_job_kinds": ["train_alpha_model"],
        "data_constraints": {
            "feature_set_ids": [feature_id], "stock_dataset_ids": [stock_id],
        },
        "label_specifications": [{
            "label_name": "stock_forward_return", "target_type": "stock_return", "horizon_days": HORIZON,
        }],
        "selection_policy": {
            "primary_metric": "rank_ic", "minimum_rank_ic": -1.0,
            "minimum_label_coverage": 0.0,
            "allowed_research_status": [
                "RESEARCH_DATA_VALIDATED", "RESEARCH_EXPLORATORY",
            ],
        },
        "stopping_policy": {
            "max_seconds": MAX_SECONDS, "max_total_training_samples": MAX_ROWS,
        },
        "hyperparameter_constraints": {
            "fixed": lightgbm_parameters_for_protocol(),
        },
        "factor_specifications": [spec.model_dump(mode="json")],
    }


def lightgbm_parameters_for_protocol():
    return {
        "learning_rate": 0.05, "num_leaves": 15, "max_depth": 4,
        "min_child_samples": 20, "n_estimators": 100,
    }


def training_request(protocol_hash: str, stock_id: str, feature_id: str):
    split = {
        "train_start": START.isoformat().replace("+00:00", "Z"),
        "train_end": (START + timedelta(days=TRAIN_DECISIONS + HORIZON + 1)).isoformat().replace("+00:00", "Z"),
        "validation_start": (START + timedelta(days=DAYS - VALIDATION_DECISIONS - HORIZON - 1)).isoformat().replace("+00:00", "Z"),
        "validation_end": (START + timedelta(days=DAYS)).isoformat().replace("+00:00", "Z"),
    }
    spec = factor_spec()
    config = BaselineComparisonConfig(
        schema_version="1.0", factor_spec=spec,
        reversal_output="reversal_source", rsi_output="rsi_source", ridge_alpha=1.0,
    )
    return {
        "kind": "train_alpha_model",
        "feature_set_id": feature_id, "label_name": "stock_forward_return",
        "target_type": "stock_return", "stock_dataset_id": stock_id,
        "horizon_days": HORIZON, "evaluation_mode": "validation_only",
        "research_protocol_hash": protocol_hash, "split": split,
        "hyperparameters": lightgbm_parameters_for_protocol(), "random_seed": 7,
        "min_rank_ic": -1.0,
        "resource_budget": {"max_seconds": MAX_SECONDS, "max_rows": MAX_ROWS},
        "baseline_comparison": config.model_dump(mode="json"),
    }


def build_stock_rows(rng):
    rows = []
    for symbol_index in range(SYMBOLS):
        symbol = f"S{symbol_index:03d}"
        base = 50 + symbol_index * 0.1
        for day in range(DAYS):
            start = START + timedelta(days=day)
            end = start.replace(hour=20)
            available = start.replace(hour=21, minute=1)
            overnight = rng.normal(0, 0.002)
            close = base * (1 + 0.00002 * day + overnight)
            open_price = close * (1 + rng.normal(0, 0.001))
            high = max(open_price, close) * 1.003
            low = min(open_price, close) * 0.997
            rows.append({
                "instrument_id": symbol, "start_at": start, "end_at": end,
                "available_at": available, "open": open_price, "high": high,
                "low": low, "close": close, "volume": 1_000_000 + day,
                "adjustment": "raw", "source": "synthetic", "source_version": "capacity-v1",
                "currency": "USD",
            })
    return rows


def build_feature_table(rng, decisions):
    n = EXPECTED_STOCK_ROWS
    decision_values = np.repeat(decisions.astype("datetime64[us]"), SYMBOLS)
    instrument_values = np.tile(np.array([f"S{i:03d}" for i in range(SYMBOLS)], dtype=object), DAYS)
    decision_array = pa.array(decision_values, type=pa.timestamp("us", tz="UTC"))
    available_array = decision_array
    instrument_array = pa.array(instrument_values, type=pa.string())
    offsets = pa.array(np.zeros(n + 1, dtype=np.int32))
    empty_flags = pa.array([], type=pa.string())
    quality_flags = pa.ListArray.from_arrays(offsets, empty_flags)
    eligible = pa.array(np.ones(n, dtype=bool))
    tables = []
    outputs = factor_spec().outputs
    for index, output in enumerate(outputs):
        values = rng.normal(size=n) + (index % 3) * 0.01
        tables.append(pa.Table.from_arrays([
            decision_array, instrument_array,
            pa.array([output] * n, type=pa.string()),
            pa.array(values, type=pa.float64()), available_array,
            eligible, quality_flags,
            pa.array([f"capacity-f{index+1:02d}-v1"] * n, type=pa.string()),
        ], schema=development_feature_schema()))
    return pa.concat_tables(tables)


def development_feature_schema():
    from stock_quant.alpha.data.partitions import FEATURE_SCHEMA
    return FEATURE_SCHEMA


def install_timing_hooks():
    original_read = DatasetStore.read_rows
    original_train = lightgbm_trainer.lgb.train
    original_quality = comparison._quality_reports
    original_compare = comparison.compare_development_models
    original_cohort = development_cohort.prepare_development_cohort
    original_report_transport = alpha_executor.training_metrics_with_report
    original_begin_publish = JobStore.begin_publish
    original_complete_publish = JobStore.complete_publish
    publication_started = [None]

    def read(self, *args, **kwargs):
        with timed("data_read_total"):
            return original_read(self, *args, **kwargs)

    def train(*args, **kwargs):
        with timed("lightgbm_fit"):
            return original_train(*args, **kwargs)

    def quality(*args, **kwargs):
        with timed("quality_scan"):
            return original_quality(*args, **kwargs)

    def compare(*args, **kwargs):
        with timed("baseline_comparison_total"):
            return original_compare(*args, **kwargs)

    def cohort(*args, **kwargs):
        with timed("cohort_pivot_join"):
            return original_cohort(*args, **kwargs)

    def report_transport(*args, **kwargs):
        with timed("baseline_report_assembly"):
            return original_report_transport(*args, **kwargs)

    def begin_publish(self, *args, **kwargs):
        publication_started[0] = time.monotonic()
        try:
            with timed("publish_serialization"):
                return original_begin_publish(self, *args, **kwargs)
        finally:
            TIMES["compute_to_first_publish"] = round(time.monotonic() - compile_started[0], 6) if compile_started[0] else 0.0

    def complete_publish(self, *args, **kwargs):
        with timed("publish_finalize"):
            return original_complete_publish(self, *args, **kwargs)

    DatasetStore.read_rows = read
    lightgbm_trainer.lgb.train = train
    comparison._quality_reports = quality
    comparison.compare_development_models = compare
    development_cohort.prepare_development_cohort = cohort
    alpha_executor.training_metrics_with_report = report_transport
    JobStore.begin_publish = begin_publish
    JobStore.complete_publish = complete_publish
    return lambda: (
        setattr(DatasetStore, "read_rows", original_read),
        setattr(lightgbm_trainer.lgb, "train", original_train),
        setattr(comparison, "_quality_reports", original_quality),
        setattr(comparison, "compare_development_models", original_compare),
        setattr(development_cohort, "prepare_development_cohort", original_cohort),
        setattr(alpha_executor, "training_metrics_with_report", original_report_transport),
        setattr(JobStore, "begin_publish", original_begin_publish),
        setattr(JobStore, "complete_publish", original_complete_publish),
    )


def artifact_audit(jobs, job_id):
    sections = []
    for section, encoded in jobs.connection.execute(
        "SELECT section,manifest FROM result_artifacts WHERE job_id=? ORDER BY section", (job_id,),
    ):
        manifest = json.loads(encoded)
        page = jobs.get_result_unexposed(job_id, section, 1000)
        rows = []
        counts = page["total_count"]
        max_page = max(_envelope_sizes(page, page["resource_uri"]))
        while True:
            rows.extend(page["rows"])
            max_page = max(max_page, max(_envelope_sizes(page, page["resource_uri"])))
            if page["next_cursor"] is None:
                break
            page = jobs.get_result_unexposed(job_id, section, 1000, page["next_cursor"])
        serialized_sizes = [len(json.dumps(row, allow_nan=False).encode()) for row in rows]
        max_row = max(serialized_sizes, default=0)
        max_envelope = max(
            max(_envelope_sizes({"rows": [row], "next_cursor": "x" * 2048, "metadata_reserve": "x" * 1024}, ""))
            for row in rows
        )
        sections.append({
            "section": section, "rows": counts, "parquet_bytes": manifest["byte_count"],
            "serialized_json_bytes": sum(serialized_sizes), "max_row_json_bytes": max_row, "max_mcp_row_envelope_bytes": max_envelope,
            "max_mcp_response_bytes": max_page,
        })
    return sections


def run_job_child(registry: Path, job_id: str, output: Path) -> None:
    with JobStore(registry) as jobs:
        claim = jobs.claim()
        assert claim is not None and claim["id"] == job_id
        compile_began = time.monotonic()
        job_cpu_began = time.process_time()
        compile_started[0] = compile_began
        restore = install_timing_hooks()
        try:
            with RssSampler() as rss, timed("execute_attempt_total"):
                completed = execute_job(jobs, claim)
        finally:
            restore()
        final_job = jobs.get_job(job_id)
        payload = {
            "completed": completed,
            "final_job": final_job,
            "attempt_total": time.monotonic() - compile_began,
            "job_cpu_seconds": time.process_time() - job_cpu_began,
            "job_peak_rss_kib": rss.max_kib,
            "timings": TIMES,
        }
        output.write_text(json.dumps(payload, sort_keys=True))


compile_started = [None]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-job", nargs=3, metavar=("REGISTRY", "JOB_ID", "OUTPUT"))
    args = parser.parse_args()
    if args.run_job:
        run_job_child(Path(args.run_job[0]), args.run_job[1], Path(args.run_job[2]))
        return
    started = time.monotonic()
    process_cpu = time.process_time()
    with TemporaryDirectory(prefix="stock-mcp-capacity-") as temporary:
        root = Path(temporary)
        registry = root / "capacity.sqlite3"
        abc = DatasetStore(registry.with_suffix(".alpha"))
        rng = np.random.default_rng(20261126)
        with timed("stock_dataset_generation"):
            stock_rows = build_stock_rows(rng)
        decision_start = np.datetime64(START.replace(hour=21, minute=1).replace(tzinfo=None), "us")
        decisions = decision_start + np.arange(DAYS) * np.timedelta64(1, "D")
        with timed("feature_table_generation"):
            feature_table = build_feature_table(rng, decisions)
        stock_request = StockDatasetRequest(
            source="synthetic", symbols=[f"S{i:03d}" for i in range(SYMBOLS)],
            start=START, end=START + timedelta(days=DAYS), calendar_version="capacity-fixture-v1",
        )
        with timed("stock_dataset_publish"):
            stock = abc.publish_stock(
                stock_request, stock_rows, calendar_version="capacity-fixture-v1",
                source_version="capacity-v1", data_quality={
                    "row_count": EXPECTED_STOCK_ROWS, "requested_symbols": SYMBOLS,
                    "observed_symbols": SYMBOLS, "missing_symbols": [],
                    "price_basis": "price_basis_unverified", "missing_rows_filled": False,
                },
            )
        with timed("feature_dataset_publish"):
            feature = abc.publish_feature_panel(
                feature_table, parent_dataset_id=stock.dataset_id,
                factor_spec_hash=compile_factor_spec(factor_spec()).spec_hash,
                instrument_ids=[f"S{i:03d}" for i in range(SYMBOLS)],
                start_at=START.replace(hour=21, minute=1),
                end_at=START.replace(hour=21, minute=1) + timedelta(days=DAYS - 1, microseconds=1),
                calendar_version="capacity-fixture-v1", price_basis="price_basis_unverified",
            )
        assert stock.data_quality["row_count"] == EXPECTED_STOCK_ROWS
        assert feature.data_quality["row_count"] == EXPECTED_FEATURE_ROWS
        del stock_rows, feature_table, rng
        gc.collect()
        with JobStore(registry) as jobs:
            research = jobs.research_registry
            spec = factor_spec()
            campaign = research.register_campaign(campaign_payload(""))
            protocol = research.register_protocol(protocol_payload(
                campaign.campaign_id, stock.dataset_id, feature.dataset_id, spec,
            ))
            request = training_request(protocol.protocol_hash, stock.dataset_id, feature.dataset_id)
            trial = research.register_trial(
                campaign_id=campaign.campaign_id, protocol_hash=protocol.protocol_hash,
                hypothesis_id=protocol.registered_hypotheses[0].hypothesis_id,
                request=request, trial_purpose="INITIAL_HYPOTHESIS_TEST",
            )
            submission = jobs.submit_alpha({
                **request, "research_trial_id": trial.trial_id,
                "idempotency_key": "capacity-profile-v1",
            })
        child_output = root / "job-result.json"
        child = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--run-job", str(registry), submission["job_id"], str(child_output)],
            check=False, capture_output=True, text=True,
            timeout=MAX_SECONDS + 30 + 5,
        )
        if child.returncode != 0:
            raise RuntimeError(child.stderr[-4096:] or f"child exited {child.returncode}")
        child_result = json.loads(child_output.read_text())
        TIMES.update(child_result["timings"])
        with JobStore(registry) as jobs:
            research = jobs.research_registry
            completed = child_result["completed"]
            final_job = child_result["final_job"]
            print("final_job", json.dumps(final_job, sort_keys=True))
            assert completed is True
            assert final_job["status"] == "completed", final_job
            attempt_elapsed = child_result["attempt_total"]
            job_cpu_seconds = child_result["job_cpu_seconds"]
            job_peak_rss_kib = child_result["job_peak_rss_kib"]
            publication_elapsed = TIMES.get("publish_serialization", 0.0) + TIMES.get("publish_finalize", 0.0)
            compute_elapsed = max(0.0, attempt_elapsed - publication_elapsed)
            result = jobs.get_result_unexposed(submission["job_id"], "training_metrics")
            metrics = result["rows"][0]
            assert metrics["baseline_comparison"]["record_type"] == "paginated-reference"
            compact = json.loads(jobs.connection.execute(
                "SELECT result FROM jobs WHERE id=?", (submission["job_id"],),
            ).fetchone()[0])
            with timed("baseline_reconstruction"):
                _metrics, report = BaselineEvidenceLoader(jobs).load(submission["job_id"], compact)
            assert report is not None
            assert report.report_hash == metrics["baseline_comparison"]["report_hash"]
            assert digest({k: v for k, v in report.model_dump(mode="json").items() if k != "report_hash"}) == report.report_hash
            with timed("registry_selection_seal"):
                snapshot, selection = research.seal_candidate_ledger(
                    campaign_id=campaign.campaign_id, protocol_hash=protocol.protocol_hash,
                )
            with timed("evidence_report_build"):
                evidence = research.build_evidence_report(
                    campaign_id=campaign.campaign_id, candidate_ledger_hash=snapshot.candidate_ledger_hash,
                )
            first_hash = evidence.report_hash
            with timed("evidence_report_repeat"):
                repeated = research.build_evidence_report(
                    campaign_id=campaign.campaign_id, candidate_ledger_hash=snapshot.candidate_ledger_hash,
                )
            assert repeated.report_hash == first_hash
            ledger = research.verify_ledger(campaign.campaign_id)
            assert ledger.status == "VERIFIED"
            with timed("mcp_pagination_reconstruction"):
                artifacts = artifact_audit(jobs, submission["job_id"])
            with timed("fixed_report_repeat_read"):
                report_page = research.read_research_report(
                    campaign_id=campaign.campaign_id, candidate_ledger_hash=snapshot.candidate_ledger_hash,
                    section="baseline_evidence", limit=100,
                )
                repeated_page = research.read_research_report(
                    campaign_id=campaign.campaign_id, candidate_ledger_hash=snapshot.candidate_ledger_hash,
                    section="baseline_evidence", limit=100,
                )
            assert report_page == repeated_page
            assert report_page["rows"][0]["report_hash"] == report.report_hash
            assert report.cohort_identity.train_sample_count == EXPECTED_TRAIN_SAMPLES
            assert report.cohort_identity.validation_sample_count == EXPECTED_VALIDATION_SAMPLES
            assert report.cohort_identity.validation_decision_count == VALIDATION_DECISIONS
            result_summary = {
                "profile": "PRODUCTION_RESEARCH_CAPACITY_PROFILE_V1",
                "status": "PASS",
                "synthetic_only": True,
                "counts": {
                    "symbols": SYMBOLS, "days": DAYS, "stock_bars": EXPECTED_STOCK_ROWS,
                    "feature_outputs": FACTORS, "feature_rows": EXPECTED_FEATURE_ROWS,
                    "lineage_rows": EXPECTED_FEATURE_ROWS + EXPECTED_STOCK_ROWS,
                    "train_samples": EXPECTED_TRAIN_SAMPLES,
                    "validation_samples": EXPECTED_VALIDATION_SAMPLES,
                    "development_samples": EXPECTED_TRAIN_SAMPLES + EXPECTED_VALIDATION_SAMPLES,
                    "validation_decisions": VALIDATION_DECISIONS,
                    "quality_scan_rows": EXPECTED_FEATURE_ROWS + EXPECTED_STOCK_ROWS,
                    "baseline_candidates": len(report.candidates),
                },
                "timings_seconds": TIMES | {
                    "attempt_compute_observed": round(compute_elapsed, 6),
                    "attempt_total": round(attempt_elapsed, 6),
                    "publication_observed": round(publication_elapsed, 6),
                    "wall_total": round(time.monotonic() - started, 6),
                },
                "completion": {
                    "job_id": submission["job_id"], "trial_id": trial.trial_id,
                    "report_hash": report.report_hash,
                    "candidate_ledger_hash": snapshot.candidate_ledger_hash,
                    "selected_model_id": selection.selected_model_id,
                    "evidence_report_hash": first_hash,
                    "model_fit_count": evidence.trial_accounting.model_fit_count,
                    "research_verdict": evidence.research_verdict,
                    "ledger_status": ledger.status,
                },
                "artifacts": artifacts,
                "resources": {
                    "cpu_seconds": round(time.process_time() - process_cpu, 6),
                    "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                    "job_peak_rss_kib": job_peak_rss_kib,
                    "job_cpu_seconds": round(job_cpu_seconds, 6),
                },
                "resource_contract": {
                    "max_seconds": MAX_SECONDS, "max_rows": MAX_ROWS,
                    "effective_compute_remaining_before_publish": round(MAX_SECONDS - TIMES.get("compute_to_first_publish", MAX_SECONDS), 6),
                    "effective_publication_remaining_after_publish": round(30 - publication_elapsed, 6),
                    "publication_seconds": 30, "hard_termination_grace_seconds": 5,
                },
            }
            output = root / "capacity-report.json"
            output.write_text(json.dumps(result_summary, indent=2, sort_keys=True))
            print(json.dumps(result_summary, indent=2, sort_keys=True))
            print(f"report_path={output}")


if __name__ == "__main__":
    main()
