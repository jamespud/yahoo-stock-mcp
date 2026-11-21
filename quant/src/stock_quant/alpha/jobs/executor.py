"""Trusted Alpha job executor using the shared durable lease/fencing protocol."""
from __future__ import annotations

import os
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import time

from pydantic import ValidationError

from ...calendar import CALENDAR_PROVIDER_VERSION, generate_long_calendar
from ...compiler import digest
from ..contracts import DatasetManifest, OptionDatasetRequest, StockDatasetRequest
from ...artifacts import MAX_RESPONSE_ROW_BYTES
from ...pagination import fits_response, fits_response_row
from ..data.catalog import DatasetStore, dataset_summary
from ..data.corporate_actions import CorporateActionDataset
from ..data.price_attestation import PriceAttestation
from ..data.option_dataset import OptionSnapshotBatch, prepare_option_dataset
from ..data.thetadata_adapter import ThetaDataHttpClient, ThetaDataV3Provider, ThetaDataV3ResponseError
from ..data.stock_dataset import StockHistoryBatch, prepare_stock_dataset
from ..data.session_coverage import mapped_stock_calendar
from ..data.yahoo_option_import import YahooOptionChainImport
from ..data.yahoo_stock_adapter import YahooStockAdapter
from ..errors import AlphaError, AlphaErrorCode
from ..factors.ast import FactorSpec
from ..factors.compiler import FactorCompileError, compile_factor_spec
from ..factors.engine import combine_feature_panels, compute_stock_factors
from ..factors.option_factors import compute_option_factors
from ..models.feature_diagnostics import diagnose_alpha_model
from ..models.inference import predict_alpha
from ..models.labels import LabelRow, historical_option_labels, stock_forward_labels
from ..models.oos_evaluation import evaluate_alpha_oos
from ..models.signal_diagnostics import (
    ANALYSIS_LABEL,
    evaluate_direct_signal,
    evaluate_weekly_signal,
    expected_session_dates,
    top20_forward_diagnostics,
)
from ..models.research_freeze import FreezeRequest, FrozenResearchRecord, FrozenResearchStore, freeze_alpha_selection
from ..models.lightgbm_trainer import AlphaModelRegistry, TrainAlphaRequest, train_alpha
from ..options.pricing import OptionPricingConfig
from ..portfolio.option_backtest import OptionExecutionConfig, run_option_backtest
from ..portfolio.stock_backtest import TopKPortfolioConfig, run_top_k_backtest
from .requests import (
    ALPHA_REQUEST_ADAPTER, BacktestAlphaPortfolioJob, BacktestOptionsJob, CaptureOptionChainJob,
    ComputeFactorsJob, ComputeOptionFactorsJob, EvaluateAlphaOOSJob, EvaluateAlphaSignalJob, FreezeAlphaSelectionJob,
    PrepareOptionsDatasetJob, PrepareStockDatasetJob, PredictAlphaJob, RESERVED_TEST_START, TrainAlphaModelJob,
)


class _ProviderRows:
    def __init__(self, rows):
        self.rows = rows
    def read(self, request):
        return self.rows


def _store(root: Path) -> DatasetStore:
    return DatasetStore(root)

def _staged_rows(store, reference: str) -> list[dict]:
    root = Path(store.path).with_suffix(".alpha-inbox")
    path = root / (reference.removeprefix("sha256:") + ".json")
    content = path.read_bytes()
    actual = "sha256:" + hashlib.sha256(content).hexdigest()
    if actual != reference:
        raise AlphaError(AlphaErrorCode.DATASET_MISMATCH, "Staged artifact integrity check failed")
    payload = json.loads(content)
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise AlphaError(AlphaErrorCode.DATA_UNAVAILABLE, "Staged artifact contains no rows")
    return rows


def _manifest_payload(manifest: DatasetManifest) -> dict:
    legacy = {"dataset_id": manifest.dataset_id, "dataset_kind": manifest.dataset_kind, "content_hash": manifest.content_hash,
            "partition_manifest": [part.model_dump(mode="json") for part in manifest.partition_manifest],
            "data_quality": manifest.data_quality, "dataset": manifest.model_dump(mode="json")}
    if manifest.dataset_kind == "stock_bars":
        row = {"dataset_id": manifest.dataset_id, **manifest.data_quality}
        size = len(json.dumps(row, allow_nan=False).encode())
        if (size > MAX_RESPONSE_ROW_BYTES or not fits_response_row(row, size)
                or not fits_response({"section": "dataset", **legacy}, resource_uri="quant://results/" + "x" * 48)):
            return dataset_summary(manifest)
    return legacy


def train_development_labels(label_rows, split):
    """Filter externally supplied label rows to the development split cutoff."""
    from ..models.temporal_split import split_matured_development
    matured = split_matured_development(label_rows, split)
    return matured["train"] + matured["validation"]


def execute_alpha_job(store, claim: dict, checkpoint) -> bool:
    began = time.monotonic()
    try:
        request = ALPHA_REQUEST_ADAPTER.validate_python(claim["request"])
        if digest(request.model_dump(mode="json")) != claim["config_hash"]:
            raise AlphaError(AlphaErrorCode.DATASET_MISMATCH, "Job configuration integrity check failed")
        root = Path(store.path).with_suffix(".alpha")
        datasets = DatasetStore(root, defer_registration=True)
        final_registry = AlphaModelRegistry(Path(store.path).with_suffix(".models"))
        staging_registry = AlphaModelRegistry(Path(store.path).with_suffix(f".models-staging-{claim['id']}"))
        pending = []
        staged_model = None
        staged_freeze = None
        def bounded(progress):
            if time.monotonic() - began > request.max_seconds:
                raise AlphaError(AlphaErrorCode.RESOURCE_LIMIT_EXCEEDED, "Alpha job exceeded max_seconds")
            checkpoint(progress)
        if isinstance(request, PrepareStockDatasetJob):
            database = os.environ.get("STOCK_MCP_DB_PATH")
            if not database:
                raise AlphaError(AlphaErrorCode.DATA_UNAVAILABLE, "STOCK_MCP_DB_PATH is not configured")
            req = StockDatasetRequest(source=request.source, symbols=request.symbols, start=request.start, end=request.end,
                                      price_basis=request.price_basis, calendar_version=request.calendar_version)
            manifest = prepare_stock_dataset(req, store=datasets, provider=YahooStockAdapter(database), checkpoint=bounded)
            pending.append(manifest)
            dataset = _manifest_payload(manifest)
            quality = ({"dataset_id": manifest.dataset_id, **dataset["data_quality"]}
                       if "data_quality" in dataset else dataset)
            result = {"dataset": {"section": "dataset", **dataset}, "data_quality": [quality],
                      "reproducibility": {"request_hash": claim["config_hash"], "manifest_hash": manifest.content_hash}}
        elif isinstance(request, CaptureOptionChainJob):
            captured_rows = request.rows if request.rows is not None else _staged_rows(store, request.rows_artifact)
            quotes = YahooOptionChainImport(observed_at=request.observed_at).convert({
                "symbol": request.underlying, "asOf": request.observed_at.isoformat(), "underlying": {"currency": "USD"}, "legs": captured_rows,
            })
            req = OptionDatasetRequest(source="yahoo", underlyings=[request.underlying], start=request.observed_at,
                                       end=request.observed_at + timedelta(days=1), quote_policy="observed_snapshot",
                                       expirations=request.expirations)
            manifest = prepare_option_dataset(req, store=datasets, provider=_ProviderRows(OptionSnapshotBatch(rows=quotes, source_version="yahoo-capture-v1", quality={"capture": True})), checkpoint=bounded)
            pending.append(manifest)
            result = {"dataset": {"section": "dataset", **_manifest_payload(manifest)},
                      "data_quality": [{"dataset_id": manifest.dataset_id, **manifest.data_quality}]}
        elif isinstance(request, PrepareOptionsDatasetJob):
            staged = request.rows if request.rows is not None else (_staged_rows(store, request.rows_artifact) if request.rows_artifact else None)
            req = OptionDatasetRequest(source=request.source, underlyings=request.underlyings, start=request.start, end=request.end, quote_policy=request.quote_policy, provider_dataset=request.provider_dataset)
            if staged is None:
                if request.source != "thetadata" or not os.environ.get("THETADATA_API_KEY"):
                    raise AlphaError(AlphaErrorCode.OPTION_HISTORY_UNAVAILABLE, "Provider rows are unavailable; configure licensed ThetaData credentials or a trusted staged artifact")
                provider = ThetaDataV3Provider(ThetaDataHttpClient.from_env())
            else:
                from ..options.quotes import OptionQuote
                quotes = [OptionQuote.model_validate(row) for row in staged]
                provider = _ProviderRows(OptionSnapshotBatch(rows=quotes, source_version="staged-provider-v1", quality={}))
            manifest = prepare_option_dataset(req, store=datasets, provider=provider, checkpoint=bounded)
            pending.append(manifest)
            result = {"dataset": {"section": "dataset", **_manifest_payload(manifest)}, "data_quality": [{"dataset_id": manifest.dataset_id, **manifest.data_quality}]}
        elif isinstance(request, ComputeFactorsJob):
            try:
                manifest = datasets.load_manifest(request.dataset_id)
            except KeyError as error:
                raise AlphaError(AlphaErrorCode.DATA_UNAVAILABLE, "Dataset was not found", {"dataset_id": request.dataset_id}) from error
            compiled = compile_factor_spec(FactorSpec.model_validate(request.factor_spec))
            factor_diagnostics = []
            feature = compute_stock_factors(manifest, compiled, store=datasets, checkpoint=bounded, diagnostics=factor_diagnostics)
            if request.auxiliary_feature_set_ids:
                feature = combine_feature_panels(feature, [datasets.load_manifest(item) for item in request.auxiliary_feature_set_ids], store=datasets)
            pending.append(feature)
            result = {"dataset": {"section": "dataset", **_manifest_payload(feature)},
                      "factors": [{"dataset_id": feature.dataset_id, "factor_spec_hash": compiled.spec_hash}],
                      "factor_diagnostics": factor_diagnostics, "reproducibility": {"dataset_id": request.dataset_id, "factor_spec_hash": compiled.spec_hash}}
        elif isinstance(request, ComputeOptionFactorsJob):
            manifest = datasets.load_manifest(request.option_dataset_id)
            prices = {}
            if request.underlying_price_dataset_id:
                stock = datasets.load_manifest(request.underlying_price_dataset_id)
                for row in datasets.read_rows(stock.dataset_id):
                    prices.setdefault(row["instrument_id"], []).append({"available_at": row["available_at"], "close": row["close"]})
            pricing_payload = dict(request.pricing_config)
            if pricing_payload and "valuation_at" not in pricing_payload:
                pricing_payload["valuation_at"] = manifest.start_at
            config = OptionPricingConfig.model_validate(pricing_payload) if pricing_payload else None
            selected_outputs = request.factor_spec.get("outputs") if request.factor_spec else None
            if selected_outputs is not None and (not isinstance(selected_outputs, list) or any(not isinstance(item, str) for item in selected_outputs)):
                raise AlphaError(AlphaErrorCode.DATASET_MISMATCH, "Option factor_spec.outputs must be a string list")
            tolerance = request.factor_spec.get("quote_tolerance_seconds", 60) if request.factor_spec else 60
            feature = compute_option_factors(manifest, store=datasets, underlying_prices=prices, pricing_config=config,
                                             selected_outputs=selected_outputs, quote_tolerance_seconds=tolerance, checkpoint=bounded)
            pending.append(feature)
            result = {"dataset": {"section": "dataset", **_manifest_payload(feature)},
                      "factors": [{"dataset_id": feature.dataset_id}], "option_analytics": [], "factor_diagnostics": []}
        elif isinstance(request, EvaluateAlphaSignalJob):
            feature_manifest = datasets.load_manifest(request.feature_set_id)
            if feature_manifest.dataset_kind != "feature_panel":
                raise AlphaError(AlphaErrorCode.DATASET_MISMATCH, "Signal diagnostics require a feature_panel dataset")
            stock_manifest = datasets.load_manifest(request.stock_dataset_id)
            if stock_manifest.dataset_kind != "stock_bars":
                raise AlphaError(AlphaErrorCode.DATASET_MISMATCH, "Signal diagnostics require a stock_bars dataset")
            if feature_manifest.end_at > RESERVED_TEST_START or stock_manifest.end_at > RESERVED_TEST_START:
                raise AlphaError(
                    AlphaErrorCode.DATASET_MISMATCH,
                    "Signal dataset physical coverage enters reserved Test",
                    {"feature_end": feature_manifest.end_at.isoformat(), "stock_end": stock_manifest.end_at.isoformat()},
                )
            if stock_manifest.dataset_id not in feature_manifest.parent_dataset_ids:
                raise AlphaError(AlphaErrorCode.DATASET_MISMATCH, "Feature set is not derived from the requested stock dataset")
            calendar = mapped_stock_calendar(stock_manifest)
            if calendar is None:
                raise AlphaError(AlphaErrorCode.DATA_UNAVAILABLE, "Signal diagnostics require a mapped stock calendar")
            expected_sessions = expected_session_dates(
                calendar,
                request.evaluation_window.start.date(),
                request.evaluation_window.end.date(),
            )
            if not expected_sessions:
                raise AlphaError(AlphaErrorCode.INSUFFICIENT_HISTORY, "Evaluation window contains no scheduled sessions")
            bounded(.1)
            feature_rows = datasets.read_rows(
                request.feature_set_id,
                columns=(
                    "decision_at", "instrument_id", "factor_name", "factor_value",
                    "feature_available_at", "eligible", "quality_flags", "factor_version",
                ),
                start_at=request.evaluation_window.start,
                end_at=request.evaluation_window.end,
            )
            bounded(.45)
            stock_rows = datasets.read_rows(
                request.stock_dataset_id,
                start_at=request.evaluation_window.start,
                end_at=request.split.validation_end,
            )
            labels = stock_forward_labels(
                stock_rows,
                horizon=request.horizon_days,
                calendar=calendar,
            )
            if not labels or any(label.label_version != "session-aligned-v2" for label in labels):
                raise AlphaError(AlphaErrorCode.DATA_UNAVAILABLE, "No session-aligned-v2 labels generated")
            bounded(.75)
            diagnostics = evaluate_direct_signal(
                feature_rows,
                labels,
                expected_instruments=feature_manifest.instrument_ids,
                expected_sessions=expected_sessions,
                spec=request.signal_spec,
            )
            weekly = evaluate_weekly_signal(
                feature_rows,
                labels,
                expected_instruments=feature_manifest.instrument_ids,
                expected_sessions=expected_sessions,
                spec=request.signal_spec,
            )
            top20 = top20_forward_diagnostics(
                feature_rows,
                labels,
                expected_instruments=feature_manifest.instrument_ids,
                expected_sessions=expected_sessions,
                spec=request.signal_spec,
            )
            bounded(.95)
            evaluation_window = {
                "start": request.evaluation_window.start.isoformat(),
                "end": request.evaluation_window.end.isoformat(),
            }
            summary = {
                **diagnostics["summary"],
                **weekly["summary"],
                "feature_set_id": request.feature_set_id,
                "stock_dataset_id": request.stock_dataset_id,
                "label_name": request.label_name,
                "label_version": "session-aligned-v2",
                "target_type": request.target_type,
                "horizon_days": request.horizon_days,
                "evaluation_window": evaluation_window,
                "daily_decision_count": diagnostics["summary"]["decision_count"],
                "daily_sample_count": diagnostics["coverage"]["valid_matched_opportunities"],
                "daily_prediction_coverage": diagnostics["coverage"]["prediction_coverage"],
                "daily_label_coverage": diagnostics["coverage"]["label_coverage"],
                "top20_forward_label_summary": top20["summary"],
            }
            source_windows = {
                "feature_start": request.evaluation_window.start.isoformat(),
                "feature_end": request.evaluation_window.end.isoformat(),
                "stock_start": request.evaluation_window.start.isoformat(),
                "stock_end": request.split.validation_end.isoformat(),
            }
            result = {
                "summary": summary,
                "rank_ic_by_decision": diagnostics["rank_ic_by_decision"],
                "weekly_rank_ic": weekly["weekly_rank_ic"],
                "top20_forward_labels": top20["top20_forward_labels"],
                "reproducibility": {
                    "signal_spec": request.signal_spec.model_dump(mode="json"),
                    "signal_spec_hash": summary["signal_spec_hash"],
                    "feature_set_id": request.feature_set_id,
                    "stock_dataset_id": request.stock_dataset_id,
                    "label_name": request.label_name,
                    "label_version": summary["label_version"],
                    "target_type": request.target_type,
                    "horizon_days": request.horizon_days,
                    "calendar_version": stock_manifest.calendar_version,
                    "calendar_id": getattr(calendar, "id", None),
                    "evaluation_window": evaluation_window,
                    "source_windows": source_windows,
                    "coverage": diagnostics["coverage"],
                    "excluded_opportunities": summary["excluded_opportunities"],
                    "daily_decision_count": summary["daily_decision_count"],
                    "daily_sample_count": summary["daily_sample_count"],
                    "daily_prediction_coverage": summary["daily_prediction_coverage"],
                    "daily_label_coverage": summary["daily_label_coverage"],
                    "weekly_decision_dates": summary["weekly_decision_dates"],
                    "weekly_decision_count": summary["weekly_decision_count"],
                    "weekly_sample_count": summary["weekly_sample_count"],
                    "weekly_prediction_coverage": summary["weekly_prediction_coverage"],
                    "weekly_label_coverage": summary["weekly_label_coverage"],
                    "weekly_coverage": summary["weekly_coverage"],
                    "top20_forward_label_summary": summary["top20_forward_label_summary"],
                    "analysis_label": ANALYSIS_LABEL,
                    "reserved_test_start": RESERVED_TEST_START.isoformat(),
                },
            }
        elif isinstance(request, TrainAlphaModelJob):
            labels = None
            if request.evaluation_mode == "validation_only":
                if not request.stock_dataset_id:
                    raise AlphaError(AlphaErrorCode.DATASET_MISMATCH, "Validation-only training currently requires stock_dataset_id")
                stock_manifest = datasets.load_manifest(request.stock_dataset_id)
                labels = stock_forward_labels(
                    datasets.read_rows(request.stock_dataset_id, start_at=request.split.train_start, end_at=request.split.validation_end),
                    horizon=request.horizon_days,
                    calendar=mapped_stock_calendar(stock_manifest),
                )
            elif request.stock_dataset_id:
                stock_manifest = datasets.load_manifest(request.stock_dataset_id)
                labels = stock_forward_labels(
                    datasets.read_rows(request.stock_dataset_id),
                    horizon=request.horizon_days,
                    calendar=mapped_stock_calendar(stock_manifest),
                )
            elif request.option_dataset_id:
                manifest = datasets.load_manifest(request.option_dataset_id)
                labels, _ = historical_option_labels(manifest, datasets, horizon_days=request.horizon_days)
            if labels is None:
                raise AlphaError(AlphaErrorCode.DATA_UNAVAILABLE, "A label source dataset is required")
            train_req = TrainAlphaRequest(
                feature_set_id=request.feature_set_id,
                label_source_dataset_id=request.stock_dataset_id or request.option_dataset_id,
                label_name=request.label_name, horizon_days=request.horizon_days,
                target_type=request.target_type, evaluation_mode=request.evaluation_mode,
                research_protocol_hash=request.research_protocol_hash, split=request.split,
                hyperparameters={key: value for key, value in request.hyperparameters.items()},
                random_seed=request.random_seed, resource_budget=request.resource_budget,
                min_rank_ic=request.min_rank_ic,
            )
            model = train_alpha(train_req, store=datasets, registry=staging_registry, label_rows=labels, checkpoint=bounded)
            staged_model = model
            model_diagnostics = diagnose_alpha_model(model.model_id, registry=staging_registry)
            result = {"dataset": {"section": "dataset", "model_set_id": model.model_set_id, "model_id": model.model_id},
                      "training_metrics": [model.metrics], "model_registry": [model.model_dump(mode="json")],
                      "diagnostics": [model_diagnostics],
                      "reproducibility": {"model_hash": model.model_hash, "environment_hash": model.environment_hash}}
        elif isinstance(request, FreezeAlphaSelectionJob):
            freeze_store = FrozenResearchStore(Path(store.path).with_suffix(".freezes"))
            freeze_request = FreezeRequest(
                research_protocol_hash=request.research_protocol_hash,
                candidate_ledger_hash=request.candidate_ledger_hash,
                selected_model_ids=request.selected_model_ids,
                test_feature_set_id=request.test_feature_set_id,
                test_label_source_dataset_id=request.test_label_source_dataset_id,
                test_start=request.test_start,
                test_end=request.test_end,
                selection_reason=request.selection_reason,
                label_name=request.label_name,
                target_type=request.target_type,
                horizon_days=request.horizon_days,
                label_version=request.label_version,
            )
            record = freeze_alpha_selection(
                freeze_request, store=datasets, model_registry=final_registry,
            )
            staged_freeze = (freeze_store, record)
            result = {
                "summary": record.model_dump(mode="json"),
                "reproducibility": {
                    "freeze_id": record.freeze_id,
                    "research_protocol_hash": record.research_protocol_hash,
                    "candidate_ledger_hash": record.candidate_ledger_hash,
                },
            }
        elif isinstance(request, EvaluateAlphaOOSJob):
            completed_freeze = store.completed_freeze_job(request.freeze_id)
            if completed_freeze is None:
                raise AlphaError(
                    AlphaErrorCode.DATASET_MISMATCH,
                    "OOS evaluation requires a completed Alpha freeze job",
                    {"freeze_id": request.freeze_id},
                )
            try:
                completed_record = FrozenResearchRecord.model_validate(completed_freeze["record"])
            except Exception as error:
                raise AlphaError(
                    AlphaErrorCode.DATASET_MISMATCH,
                    "Completed freeze job contains an invalid freeze record",
                    {"freeze_id": request.freeze_id, "reason": str(error)},
                ) from error
            freeze_store = FrozenResearchStore(Path(store.path).with_suffix(".freezes"))
            record = freeze_store.materialize(completed_record)
            evaluation = evaluate_alpha_oos(
                record, store=datasets, model_registry=final_registry, checkpoint=bounded,
            )
            prediction = datasets.load_published_manifest(evaluation["prediction_dataset_id"])
            pending.append(prediction)
            result = {
                "summary": evaluation["summary"],
                "rank_ic": evaluation["rank_ic"],
                "reproducibility": {
                    "freeze_id": record.freeze_id,
                    "prediction_dataset_id": prediction.dataset_id,
                    "model_ids": record.frozen_model_ids,
                },
            }
        elif isinstance(request, PredictAlphaJob):
            prediction = predict_alpha(request.model_set_id, request.feature_set_id, request.start, request.end, store=datasets, registry=final_registry)
            pending.append(prediction)
            result = {"dataset": {"section": "dataset", **_manifest_payload(prediction)},
                      "predictions": [{"dataset_id": prediction.dataset_id}], "rank_ic": []}
        elif isinstance(request, BacktestAlphaPortfolioJob):
            if request.price_attestation is None or request.corporate_action_dataset is None:
                raise AlphaError(AlphaErrorCode.PRICE_BASIS_UNVERIFIED, "Portfolio execution requires scoped price attestation and corporate-action coverage")
            prediction = datasets.load_manifest(request.prediction_set_id)
            market = datasets.load_manifest(request.dataset_id)
            config = TopKPortfolioConfig.model_validate(request.portfolio_config)
            run = run_top_k_backtest(
                prediction, market, store=datasets,
                price_attestation=PriceAttestation.model_validate(request.price_attestation),
                corporate_action_dataset=CorporateActionDataset.model_validate(request.corporate_action_dataset),
                config=config, stress_cost_multiplier=request.stress_cost_multiplier,
            )
            result = {"summary": run.metrics, "portfolio_metrics": [run.metrics], "orders": run.orders,
                      "target_weights": run.target_weights, "equity_curve": run.equity_curve,
                      "positions": run.positions, "fills": run.fills, "risk_events": run.risk_events,
                      "diagnostics": run.diagnostics, "final_account": run.final_account,
                      "cash_ledger": run.cash_ledger, "corporate_actions": run.corporate_actions}
        elif isinstance(request, BacktestOptionsJob):
            prediction = datasets.load_manifest(request.prediction_set_id)
            options = datasets.load_manifest(request.option_dataset_id)
            config = OptionExecutionConfig.model_validate(request.execution_config)
            run = run_option_backtest(prediction, options, store=datasets, config=config)
            result = {"summary": run.metrics, "trades": run.trades, "fills": run.fills, "positions": run.positions,
                      "cash_ledger": run.cash_ledger, "risk_events": run.risk_events, "diagnostics": run.diagnostics}
        else:
            raise AlphaError(AlphaErrorCode.UNSUPPORTED_EXECUTION_MODEL, "Unsupported alpha job")
        if not store.begin_publish(claim, result):
            return False
        staged_freeze_path = None
        try:
            def publish_checkpoint():
                if not store.heartbeat(claim, .99):
                    raise RuntimeError("OWNERSHIP_LOST during publication")
            for manifest in pending:
                datasets.register(manifest, checkpoint=publish_checkpoint, job_path=Path(store.path))
            if staged_model is not None:
                final_registry.commit_from(staging_registry, staged_model.model_id, checkpoint=publish_checkpoint)
            if staged_freeze is not None:
                freeze_store, freeze_record = staged_freeze
                staged_freeze_path = freeze_store.stage(freeze_record, checkpoint=publish_checkpoint)
            completed = store.complete_publish(claim, dataset_ids=[manifest.dataset_id for manifest in pending])
        except Exception as error:
            if staged_freeze_path is not None and staged_freeze_path.exists():
                staged_freeze_path.unlink()
            return store.fail_publish(claim, error, "EXECUTION_FAILED")
        if not completed:
            if staged_freeze_path is not None and staged_freeze_path.exists():
                staged_freeze_path.unlink()
            return False
        if staged_freeze is not None:
            freeze_store, freeze_record = staged_freeze
            try:
                freeze_store.finalize(freeze_record, staged_freeze_path)
            except Exception:
                # The completed job result is authoritative; OOS materializes from it if needed.
                pass
        return True
    except AlphaError as error:
        return store.fail_publish(claim, error, error.code.value)
    except FactorCompileError as error:
        wrapped = AlphaError(AlphaErrorCode.DATASET_MISMATCH, "FactorSpec compilation failed", error.diagnostic)
        return store.fail_publish(claim, wrapped, wrapped.code.value)
    except ValidationError as error:
        wrapped = AlphaError(AlphaErrorCode.DATASET_MISMATCH, "Alpha request or schema validation failed", {"reason": str(error)})
        return store.fail_publish(claim, wrapped, wrapped.code.value)
    except ThetaDataV3ResponseError as error:
        wrapped = AlphaError(AlphaErrorCode.DATA_UNAVAILABLE, "ThetaData response contract failed", {"reason": str(error)})
        return store.fail_publish(claim, wrapped, wrapped.code.value)
    except KeyError as error:
        wrapped = AlphaError(AlphaErrorCode.DATA_UNAVAILABLE, str(error))
        return store.fail_publish(claim, wrapped, wrapped.code.value)
    except Exception as error:
        return store.fail_publish(claim, error)
