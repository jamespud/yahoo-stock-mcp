"""Bounded JSON request process used by the TypeScript control plane.

This entry point executes installed control code only. Python source artifacts
are syntax-checked and stored as data; isolated job workers load strategy code.
"""
import json
import os
import sys
from importlib.metadata import version
from pathlib import Path

from .alpha.capabilities import AlphaCapabilities
from .alpha.errors import AlphaError
from .calendar import CALENDAR_PROVIDER_VERSION, CalendarSchedule, generate_calendar
from .compiler import OPERATORS, CompileError, validate_strategy
from .contracts import Strategy
from .jobs import JobStore
from .pagination import MAX_FRAME_BYTES, fits_response
from .sandbox import PythonArtifact, SandboxSession, validate_python
from .worker import wake_worker


def capabilities():
    sandbox = {"available": False, "backend": "docker", "reason": "operator image not configured",
               "randomness_policy": "stdlib_random_seed_v1; initialized before module/class loading; ctx.random_seed for custom RNGs"}
    image = os.environ.get("QUANT_SANDBOX_IMAGE")
    if image:
        try:
            SandboxSession.preflight(image)
            sandbox.update(available=True, image=image, reason="operator image and required isolation controls passed preflight")
        except (ValueError, KeyError, IndexError) as error:
            sandbox["reason"] = str(error)[:1024]
    result = {
        "schema_version": "1.0", "strategy_modes": ["dsl", "python"] if sandbox["available"] else ["dsl"],
        "dsl_schema": Strategy.model_json_schema(),
        "calendar_schema": CalendarSchedule.model_json_schema(),
        "data_processing": {"quality": "explicit calendar/PIT coverage; no suspension inference", "resampling": "session_ohlcv_v1; complete constituents; derived availability", "calendar_providers": ["declared", "exchange_calendars"], "calendar_announcement_history": False, "calendar_provider_version": CALENDAR_PROVIDER_VERSION, "calendar_generation_available": version("exchange-calendars") == CALENDAR_PROVIDER_VERSION},
        "python_artifact_schema": PythonArtifact.model_json_schema(), "sandbox": sandbox,
        "operators": [{"name": name, "version": version, "parameters": {"period": {"type": "integer", "minimum": 1, "maximum": 10000}}} for name, version in OPERATORS.items()],
        "timeframes": ["1m", "5m", "15m", "1h", "1d", "1w"],
        "data_providers": [{"name": "local_yahoo_sqlite", "timeframes": ["1m", "5m", "15m", "1h", "1d"], "availability_policy": "modeled_bar_end_plus_delay", "requires_explicit_session_schedule": True, "coverage": "local_database_only", "historical_revisions": False, "price_basis": "vendor_ohlc_unverified", "raw_execution_requires_explicit_attestation": True, "corporate_action_acquisition": False}], "python_sandbox": sandbox["available"],
        "mcp_execution_enabled": True,
        "signal_backends": {"pit": {"alignment": "frozen_target_publication_grid", "rolling_after_alignment": True},
                            "vector": {"implementation": "numpy", "scope": "one execution symbol/timeframe, monotone publication; no alignment or time-varying split adjustment", "broker": "shared_event_kernel", "max_cells": 2000000, "max_operations": 20000000}},
        "execution_models": ["single_asset_long_only_next_bar_open", "portfolio_long_only_next_bar_open"],
        "result_sections": ["artifacts", "analytics", "attribution", "daily_returns", "summary", "orders", "fills", "equity_curve", "reproducibility", "final_account", "diagnostics", "cash_ledger", "corporate_actions", "exposures", "risk_events", "trials", "walk_forward", "parameter_sensitivity", "screen", "dataset", "data_quality", "factors", "factor_diagnostics", "option_analytics", "training_metrics", "baseline_comparison", "model_registry", "predictions", "rank_ic", "portfolio_metrics", "target_weights", "trades"],
        "artifact_storage": {"format": "parquet-json-v1", "root": "registry_path + .artifacts", "max_rows_per_section": 1000000, "max_json_bytes_per_section": 268435456, "legacy_json_reads": True},
        "research": {"methods": ["grid_search", "walk_forward", "cross_sectional_screen"], "max_trials": 100, "max_evaluations": 1000, "max_windows": 10, "max_symbols": 500},
        "limits": {"max_expression_depth": 64, "max_expression_visits": 10000, "max_request_bytes": 1048576},
    }
    result.update(AlphaCapabilities.detect().model_dump(mode="json"))
    try:
        from .alpha.jobs.requests import ALPHA_REQUEST_ADAPTER
        result["alpha_job_request_schema"] = ALPHA_REQUEST_ADAPTER.json_schema()
    except ImportError:
        result["alpha_job_request_schema"] = None
    return result


def main():
    try:
        raw = sys.stdin.buffer.read(1048577)
        if len(raw) > 1048576:
            raise ValueError("RESOURCE_LIMIT_EXCEEDED: request too large")
        request = json.loads(raw)
        method = request["method"]
        if method == "capabilities":
            result = capabilities()
        elif method == "calendar":
            result = generate_calendar(calendar_name=request["calendar_name"], exchange=request["exchange"], start_date=request["start_date"], end_date=request["end_date"]).model_dump(mode="json")
        elif method == "validate_calendar":
            result = CalendarSchedule.model_validate(request["calendar"]).model_dump(mode="json")
        elif method == "alpha_model_diagnostics":
            from .alpha.models.feature_diagnostics import diagnose_alpha_model
            from .alpha.models.registry import AlphaModelRegistry
            result = diagnose_alpha_model(
                request["model_id"],
                registry=AlphaModelRegistry(Path(request["registry_path"]).with_suffix(".models")),
            )
        elif method == "alpha_data_quality":
            from .alpha.data.catalog import DatasetStore, dataset_summary
            from .alpha.data.research_quality import assess_research_quality
            datasets = DatasetStore(Path(request["registry_path"]).with_suffix(".alpha"))
            dataset_id, section = request["dataset_id"], request.get("section")
            overhead = request.get("mcp_overhead_bytes")
            if section == "research_readiness":
                if any(request.get(key) is not None for key in ("limit", "cursor", "symbol")):
                    raise ValueError("Pagination fields do not apply to research_readiness")
                result = assess_research_quality(dataset_id, datasets).model_dump(mode="json")
            elif section not in (None, "overview"):
                result = datasets.audit_dataset(dataset_id, section, request.get("limit", 100),
                                                request.get("cursor"), request.get("symbol"),
                                                mcp_overhead_bytes=overhead)
            else:
                if any(request.get(key) is not None for key in ("limit", "cursor", "symbol")):
                    raise ValueError("Dataset pagination fields require a detail section")
                manifest = datasets.load_manifest(dataset_id)
                validation = datasets.validate_dataset(dataset_id)
                metadata = {key: getattr(manifest, key) for key in (
                    "dataset_id", "dataset_kind", "source", "source_version", "calendar_version", "price_basis",
                )}
                metadata["validation"] = validation.model_dump(mode="json")
                legacy = {"schema_version": "alpha-data-quality-v1", **metadata, "data_quality": manifest.data_quality}
                result = (legacy if section is None and fits_response(legacy, mcp_overhead_bytes=overhead) else
                          {"schema_version": "alpha-data-quality-overview-v1", **metadata, **dataset_summary(manifest)})
        elif method == "validate":
            result = (validate_python(request["strategy"]) if request["strategy"].get("mode") == "python"
                      else validate_strategy(request["strategy"], request.get("parameters"), request.get("symbol")))
        elif method in ("register", "compile", "import_snapshot", "run_backtest", "submit_alpha", "audit_training_rejection", "audit_walk_forward_rejection", "register_alpha_research_protocol", "register_alpha_trial", "get_alpha_research_ledger", "seal_alpha_candidate_ledger", "get_alpha_research_report", "get_job", "cancel_job", "get_result", "evaluate_signal", "optimize", "screen", "data_quality", "resample"):
            with JobStore(request["registry_path"]) as registry:
                if method == "register":
                    result = registry.register(request["strategy"])
                elif method == "compile":
                    result = registry.compile(request["strategy_id"], request.get("parameters"), request.get("symbol"))
                elif method == "import_snapshot":
                    result = registry.import_snapshot(request["bars"], request["calendar_version"], request.get("actions"), request.get("calendars"))
                elif method == "data_quality":
                    result = registry.quality(request["data_snapshot_id"], symbol=request["symbol"], timeframe=request["timeframe"],
                                              as_of=request["as_of"], max_staleness_seconds=request.get("max_staleness_seconds"))
                elif method == "resample":
                    result = registry.resample(request["data_snapshot_id"], symbol=request["symbol"], source_timeframe=request["source_timeframe"],
                                               target_timeframe=request["target_timeframe"], as_of=request.get("as_of"))
                elif method == "evaluate_signal":
                    result = registry.evaluate(request["strategy_id"], request["data_snapshot_id"], request["as_of"], symbol=request.get("symbol"), parameters=request.get("parameters"))
                elif method in ("optimize", "screen"):
                    result = registry.submit({**request["research"], "kind": method})
                    wake_worker(request["registry_path"])
                elif method == "run_backtest":
                    result = registry.submit(request["backtest"])
                    wake_worker(request["registry_path"])
                elif method == "submit_alpha":
                    result = registry.submit_alpha(request["job"])
                    wake_worker(request["registry_path"])
                elif method == "register_alpha_research_protocol":
                    if request.get("campaign") is not None:
                        campaign = registry.research_registry.register_campaign(request["campaign"])
                        campaign_id = campaign.campaign_id
                    else:
                        campaign = registry.research_registry.get_campaign(request["campaign_id"])
                        campaign_id = campaign.campaign_id
                    protocol = registry.research_registry.register_protocol({
                        **request["protocol"], "campaign_id": campaign_id,
                    })
                    result = {
                        "campaign": campaign.model_dump(mode="json"),
                        "protocol": protocol.model_dump(mode="json"),
                    }
                elif method == "register_alpha_trial":
                    result = registry.research_registry.register_trial(
                        campaign_id=request["campaign_id"],
                        protocol_hash=request["protocol_hash"],
                        hypothesis_id=request["hypothesis_id"],
                        request=request["request"],
                        trial_purpose=request.get("trial_purpose", "INITIAL_HYPOTHESIS_TEST"),
                        parent_trial_id=request.get("parent_trial_id"),
                    ).model_dump(mode="json")
                elif method == "get_alpha_research_ledger":
                    result = registry.research_registry.read_research_ledger(
                        campaign_id=request["campaign_id"], section=request.get("section", "summary"),
                        limit=request.get("limit", 100), cursor=request.get("cursor"),
                        ledger_watermark=request.get("ledger_watermark"),
                        mcp_overhead_bytes=request.get("mcp_overhead_bytes"),
                    )
                elif method == "seal_alpha_candidate_ledger":
                    snapshot, selection = registry.research_registry.seal_candidate_ledger(
                        campaign_id=request["campaign_id"], protocol_hash=request["protocol_hash"],
                    )
                    result = {
                        "candidate_ledger_snapshot": snapshot.model_dump(mode="json"),
                        "selection": selection.model_dump(mode="json"),
                    }
                elif method == "get_alpha_research_report":
                    result = registry.research_registry.read_research_report(
                        campaign_id=request["campaign_id"], protocol_hash=request.get("protocol_hash"),
                        candidate_ledger_hash=request.get("candidate_ledger_hash"),
                        ledger_watermark=request.get("ledger_watermark"),
                        section=request.get("section", "summary"), limit=request.get("limit", 100),
                        cursor=request.get("cursor"), mcp_overhead_bytes=request.get("mcp_overhead_bytes"),
                    )
                elif method == "audit_training_rejection":
                    fields = request["fields"]
                    if not isinstance(fields, list) or not 1 <= len(fields) <= 20 or any(not isinstance(field, str) for field in fields):
                        raise ValueError("Invalid training rejection fields")
                    result = registry.audit_training_rejection("mcp", fields)
                elif method == "audit_walk_forward_rejection":
                    fields = request["fields"]
                    if not isinstance(fields, list) or not 1 <= len(fields) <= 20 or any(not isinstance(field, str) for field in fields):
                        raise ValueError("Invalid walk-forward rejection fields")
                    result = registry.audit_walk_forward_rejection("mcp", fields)
                elif method == "get_job":
                    result = registry.get_job(request["job_id"])
                    # Reads can restart consumers for queued or expired work, never
                    # replace the owner of a currently valid lease.
                    queued = registry.connection.execute("SELECT 1 FROM jobs WHERE status='queued' OR (status='running' AND lease_until<=unixepoch()) LIMIT 1").fetchone()
                    if queued:
                        wake_worker(request["registry_path"])
                elif method == "cancel_job":
                    result = registry.cancel(request["job_id"])
                else:
                    result = registry.get_result(request["job_id"], request.get("section", "summary"),
                                                 request.get("limit", 100), request.get("cursor"),
                                                 mcp_overhead_bytes=request.get("mcp_overhead_bytes"))
        else:
            raise ValueError("Unsupported quant method")
        encoded = json.dumps({"result": result}, allow_nan=False)
        if len(encoded.encode()) + 1 > MAX_FRAME_BYTES:
            raise ValueError("RESOURCE_LIMIT_EXCEEDED: quant response exceeds 4 MiB")
        print(encoded)
    except CompileError as error:
        print(json.dumps({"error": error.diagnostic}))
    except AlphaError as error:
        print(json.dumps({"error": error.to_dict()}))
        sys.exit(1)
    except Exception as error:  # noqa: BLE001 - JSON bridge must return a bounded error
        print(json.dumps({"error": {"code": "REQUEST_FAILED", "message": str(error)}}))
        sys.exit(1)


if __name__ == "__main__":
    main()
