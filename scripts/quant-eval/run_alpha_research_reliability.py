"""Controlled 99-symbol Alpha Research Reliability acceptance."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from stock_quant.alpha.data.catalog import DatasetStore
from stock_quant.alpha.factors.ast import FactorSpec
from stock_quant.alpha.factors.compiler import compile_factor_spec
from stock_quant.alpha.factors.engine import compute_stock_factors
from stock_quant.alpha.jobs.executor import train_development_labels
from stock_quant.alpha.models.labels import stock_forward_labels
from stock_quant.alpha.models.lightgbm_trainer import (
    AlphaModelRegistry, ModelResourceBudget, ModelSetManifest, TrainAlphaRequest, train_alpha,
)
from stock_quant.alpha.models.research_freeze import FreezeRequest, FrozenResearchStore, freeze_alpha_selection
from stock_quant.alpha.models.oos_evaluation import evaluate_alpha_oos
from stock_quant.alpha.models.temporal_split import ModelDevelopmentSplit

DATASET = "sha256:ded14a7ae40ed989fe0b205b4bc531d3ab82ea7352080fe80ef5772486a237cc"
PROTOCOL = "sha256:9a1103dcc225013a4d6045ed8682a08d6cdf653fab7747166aebaa2ee9349804"
TRAIN_START = datetime(2017, 7, 3, tzinfo=timezone.utc)
TRAIN_END = datetime(2022, 1, 1, tzinfo=timezone.utc)
VALIDATION_START = datetime(2022, 1, 14, tzinfo=timezone.utc)
VALIDATION_END = datetime(2024, 1, 1, tzinfo=timezone.utc)
TEST_START = datetime(2024, 1, 15, tzinfo=timezone.utc)
TEST_END = datetime(2026, 11, 18, tzinfo=timezone.utc)
HORIZON = 5
SEED = 20261119


def _request(compiled, dataset, seed=SEED):
    return TrainAlphaRequest(
        feature_set_id=dataset.dataset_id,
        label_name="stock_forward_return",
        target_type="stock_return",
        evaluation_mode="validation_only",
        split=ModelDevelopmentSplit(
            train_start=TRAIN_START, train_end=TRAIN_END,
            validation_start=VALIDATION_START, validation_end=VALIDATION_END,
            embargo_days=0,
        ),
        hyperparameters={
            "learning_rate": 0.05, "num_leaves": 31, "min_child_samples": 20,
            "n_estimators": 200,
        },
        random_seed=seed,
        resource_budget=ModelResourceBudget(max_seconds=600, max_rows=1000000),
        min_rank_ic=0.0,
        research_protocol_hash=PROTOCOL,
    )


def run(*, registry_root: str, dataset_hash: str = DATASET, protocol_hash: str = PROTOCOL, report_dir: str | None = None) -> dict:
    if dataset_hash != DATASET or protocol_hash != PROTOCOL:
        return {"status": "FAIL", "reason": "frozen dataset or protocol hash changed"}
    store = DatasetStore(registry_root)
    dataset = store.load_manifest(dataset_hash)
    if dataset.dataset_kind != "stock_bars":
        return {"status": "FAIL", "reason": "dataset is not stock_bars"}
    if dataset.data_quality.get("observed_symbols") != 99:
        return {"status": "FAIL", "reason": "universe must cover exactly 99 symbols", "observed_symbols": dataset.data_quality.get("observed_symbols")}
    source = store.read_rows(dataset.dataset_id, start_at=TRAIN_START, end_at=VALIDATION_END)
    full_source = store.read_rows(dataset.dataset_id)
    if not source:
        return {"status": "FAIL", "reason": "no development rows"}
    label_source = store.read_rows(dataset.dataset_id, start_at=TRAIN_START, end_at=VALIDATION_END + timedelta(days=1))
    label_rows = stock_forward_labels(label_source, horizon=HORIZON)
    feature_ids, factor_ids, model_ids, model_hashes, diagnostics = [], [], [], {}, []
    registry_root = Path(report_dir or ".") / "models"
    models = AlphaModelRegistry(registry_root)
    for index, spec in enumerate(_specs(), start=1):
        compiled = compile_factor_spec(spec)
        factor_ids.append(compiled.spec_hash)
        dataset_manifest = compute_stock_factors(dataset, compiled, store=store, rows=full_source)
        feature_ids.append(dataset_manifest.dataset_id)
        rows = train_development_labels(label_rows, _request(compiled, dataset_manifest).split)
        if not rows:
            return {"status": "FAIL", "reason": f"H{index:02d} has no matured development labels"}
        request = _request(compiled, dataset_manifest)
        request = request.model_copy(update={"feature_set_id": dataset_manifest.dataset_id})
        model = train_alpha(request, store=store, registry=models, label_rows=label_rows)
        model_ids.append(model.model_id)
        model_hashes[model.model_id] = model.model_hash
        diagnostics.append({
            "candidate": f"H{index:02d}",
            "feature_set_id": dataset_manifest.dataset_id,
            "factor_spec_hash": compiled.spec_hash,
            "model_id": model.model_id,
            "validation_metrics": model.metrics["validation"],
        })
    # Select highest validation rank IC mean when a threshold is met.
    eligible = [item for item in diagnostics if item["validation_metrics"].get("rank_ic_mean") is not None and item["validation_metrics"]["prediction_coverage"] >= 0.8]
    if not eligible:
        return {"status": "PASS", "outcome": "NO_CANDIDATE_SELECTED", "diagnostics": diagnostics}
    winner = max(eligible, key=lambda item: item["validation_metrics"]["rank_ic_mean"])
    freeze_store = FrozenResearchStore(Path(report_dir or ".") / "freezes")
    record = freeze_alpha_selection(FreezeRequest(
        research_protocol_hash=PROTOCOL,
        candidate_ledger_hash=_digest(diagnostics),
        selected_model_ids=[winner["model_id"]],
        test_feature_set_id=winner["feature_set_id"],
        test_label_source_dataset_id=dataset.dataset_id,
        test_start=TEST_START,
        test_end=TEST_END,
        selection_reason="highest validation rank_ic_mean",
        label_name="stock_forward_return",
        horizon_days=HORIZON,
    ), store=store, model_registry=models, freeze_store=freeze_store)
    oos = evaluate_alpha_oos(record, store=store, model_registry=models)
    return {
        "status": "PASS",
        "dataset_hash": dataset_hash,
        "protocol_hash": protocol_hash,
        "feature_set_ids": feature_ids,
        "factor_spec_hashes": factor_ids,
        "model_ids": model_ids,
        "model_hashes": model_hashes,
        "diagnostics": diagnostics,
        "selected_model_id": winner["model_id"],
        "freeze_id": record.freeze_id,
        "oos_summary": oos["summary"],
    }


def _digest(value) -> str:
    import hashlib
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _specs():
    def node(id, operator, inputs, period=None):
        return {"id": id, "operator": operator, "inputs": inputs, "params": {"period": period} if period else {}}
    return [
        FactorSpec.model_validate({"schema_version": "1.0", "timeframe": "1d",
            "nodes": [node("mom63", "pct_change", ["close"], 63), node("mom126", "pct_change", ["close"], 126), node("skip5mom", "lag", ["mom63"], 5)],
            "outputs": ["mom63", "mom126", "skip5mom"]}),
        FactorSpec.model_validate({"schema_version": "1.0", "timeframe": "1d",
            "nodes": [node("ret1", "pct_change", ["close"], 1), node("ret5", "pct_change", ["close"], 5), node("rsi5", "rsi", ["close"], 5)],
            "outputs": ["ret1", "ret5", "rsi5"]}),
        FactorSpec.model_validate({"schema_version": "1.0", "timeframe": "1d",
            "nodes": [node("ret1", "pct_change", ["close"], 1), node("vol20", "rolling_std", ["ret1"], 20), node("vol60", "rolling_std", ["ret1"], 60)],
            "outputs": ["vol20", "vol60"]}),
        FactorSpec.model_validate({"schema_version": "1.0", "timeframe": "1d",
            "nodes": [node("ret1", "pct_change", ["close"], 1), node("vmean20", "rolling_mean", ["volume"], 20), node("volume_ratio", "div", ["volume", "vmean20"]), node("price_volume", "mul", ["ret1", "volume_ratio"])],
            "outputs": ["ret1", "volume_ratio", "price_volume"]}),
        FactorSpec.model_validate({"schema_version": "1.0", "timeframe": "1d",
            "nodes": [node("ret1", "pct_change", ["close"], 1), node("mom63", "pct_change", ["close"], 63), node("vol20", "rolling_std", ["ret1"], 20), node("trend_vol", "div", ["mom63", "vol20"]), node("interaction", "mul", ["mom63", "vol20"])],
            "outputs": ["mom63", "vol20", "trend_vol", "interaction"]}),
        FactorSpec.model_validate({"schema_version": "1.0", "timeframe": "1d",
            "nodes": [node("ret1", "pct_change", ["close"], 1), node("mom63", "pct_change", ["close"], 63), node("mom126", "pct_change", ["close"], 126), node("ret5", "pct_change", ["close"], 5), node("vol20", "rolling_std", ["ret1"], 20), node("vmean20", "rolling_mean", ["volume"], 20), node("volume_ratio", "div", ["volume", "vmean20"]), node("trend_vol", "div", ["mom63", "vol20"])],
            "outputs": ["ret1", "ret5", "mom63", "mom126", "vol20", "volume_ratio", "trend_vol"]}),
    ]
