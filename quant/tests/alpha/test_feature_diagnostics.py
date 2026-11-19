import json
import subprocess
import sys
from pathlib import Path

import lightgbm as lgb
import numpy as np

from stock_quant.alpha.models.feature_diagnostics import diagnose_alpha_model
from stock_quant.alpha.models.registry import AlphaModelRegistry
from stock_quant.compiler import digest

FEATURES = ["ret1", "ret5", "mom63", "mom126", "vol20", "volume_ratio", "trend_vol"]


def _train_model(seed: int):
    rng = np.random.default_rng(seed)
    x = rng.normal(size=(240, len(FEATURES)))
    y = 0.8 * x[:, 0] + 0.5 * x[:, 2] - 0.6 * x[:, 4] + 0.2 * x[:, 5]
    dataset = lgb.Dataset(x, label=y, feature_name=FEATURES, free_raw_data=False)
    booster = lgb.train(
        {"objective": "regression", "verbosity": -1, "num_leaves": 7, "max_depth": 3, "seed": seed,
         "min_data_in_leaf": 5, "feature_fraction": 1.0},
        dataset,
        num_boost_round=18,
    )
    return booster


def _save_model(registry, booster, *, training_window, validation_window, stability_by_fold=None):
    gain = booster.feature_importance(importance_type="gain", iteration=booster.current_iteration())
    return registry.save(model_text=booster.model_to_string(), metadata={
        "feature_set_id": "sha256:" + "1" * 64,
        "label_definition_hash": "sha256:" + "2" * 64,
        "feature_schema": FEATURES,
        "feature_schema_hash": digest(FEATURES),
        "selected_iteration": booster.current_iteration(),
        "feature_importance": {name: float(value) for name, value in zip(FEATURES, gain, strict=True)},
        "training_window": training_window,
        "validation_window": validation_window,
        "stability_by_fold": stability_by_fold or [],
    })


def _windows():
    return (
        {"start": "2020-01-01T00:00:00+00:00", "end": "2021-12-31T00:00:00+00:00"},
        {"start": "2022-01-01T00:00:00+00:00", "end": "2023-12-31T00:00:00+00:00"},
    )


def test_model_importance_is_readable_via_mcp(tmp_path):
    registry = AlphaModelRegistry(tmp_path / "registry.models")
    booster = _train_model(7)
    train, validation = _windows()
    saved = _save_model(registry, booster, training_window=train, validation_window=validation)
    request = {"method": "alpha_model_diagnostics", "registry_path": str(tmp_path / "registry.sqlite3"), "model_id": saved["model_id"]}
    completed = subprocess.run(
        [sys.executable, "-m", "stock_quant"],
        cwd=Path(__file__).parents[3],
        env={**__import__("os").environ, "PYTHONPATH": str(Path(__file__).parents[3] / "quant/src")},
        input=json.dumps(request), text=True, capture_output=True, check=True,
    )
    result = json.loads(completed.stdout)["result"]
    assert result["model_id"] == saved["model_id"]
    assert set(result["feature_importance_gain"]) == set(FEATURES)
    assert result["stability_status"] == "NOT_TESTED"


def test_importance_feature_names_match_frozen_schema(tmp_path):
    registry = AlphaModelRegistry(tmp_path / "models")
    booster = _train_model(9)
    train, validation = _windows()
    saved = _save_model(registry, booster, training_window=train, validation_window=validation)
    result = diagnose_alpha_model(saved["model_id"], registry=registry)
    assert list(result["feature_importance_gain"]) == FEATURES
    assert list(result["feature_importance_split"]) == FEATURES
    assert result["feature_schema_hash"] == digest(FEATURES)


def test_importance_matches_lightgbm_oracle(tmp_path):
    registry = AlphaModelRegistry(tmp_path / "models")
    booster = _train_model(11)
    train, validation = _windows()
    saved = _save_model(registry, booster, training_window=train, validation_window=validation)
    result = diagnose_alpha_model(saved["model_id"], registry=registry)
    reopened = lgb.Booster(model_str=registry.load(saved["model_id"]).model_text)
    for importance_type, key in (("gain", "feature_importance_gain"), ("split", "feature_importance_split")):
        oracle = reopened.feature_importance(importance_type=importance_type, iteration=reopened.current_iteration())
        assert result[key] == {name: float(value) for name, value in zip(FEATURES, oracle, strict=True)}


def test_single_model_cannot_claim_stable_importance(tmp_path):
    registry = AlphaModelRegistry(tmp_path / "models")
    booster = _train_model(13)
    train, validation = _windows()
    saved = _save_model(registry, booster, training_window=train, validation_window=validation)
    result = diagnose_alpha_model(saved["model_id"], registry=registry)
    assert result["stability_status"] == "NOT_TESTED"
    assert result["stability_by_fold"] == []


def test_stability_uses_development_windows_only(tmp_path):
    registry = AlphaModelRegistry(tmp_path / "models")
    train, validation = _windows()
    fold_a = _save_model(registry, _train_model(17), training_window=train, validation_window=validation)
    fold_b = _save_model(registry, _train_model(19), training_window=train, validation_window=validation)
    primary_booster = _train_model(23)
    inside = [
        {"fold_id": "fold-a", "model_id": fold_a["model_id"], "window": {"start": "2020-01-01T00:00:00+00:00", "end": "2022-12-31T00:00:00+00:00"}},
        {"fold_id": "fold-b", "model_id": fold_b["model_id"], "window": {"start": "2021-01-01T00:00:00+00:00", "end": "2023-12-31T00:00:00+00:00"}},
    ]
    primary = _save_model(registry, primary_booster, training_window=train, validation_window=validation, stability_by_fold=inside)
    result = diagnose_alpha_model(primary["model_id"], registry=registry)
    assert result["stability_status"] in {"PASS", "UNSTABLE"}
    assert len(result["stability_by_fold"]) == 2
    assert result["stability_spearman"] is not None

    outside = [
        {"fold_id": "fold-a", "model_id": fold_a["model_id"], "window": {"start": "2020-01-01T00:00:00+00:00", "end": "2024-01-01T00:00:00+00:00"}},
        {"fold_id": "fold-b", "model_id": fold_b["model_id"], "window": {"start": "2021-01-01T00:00:00+00:00", "end": "2023-12-31T00:00:00+00:00"}},
    ]
    invalid = _save_model(registry, _train_model(29), training_window=train, validation_window=validation, stability_by_fold=outside)
    invalid_result = diagnose_alpha_model(invalid["model_id"], registry=registry)
    assert invalid_result["stability_status"] == "INVALID_EVIDENCE"


def test_model_diagnostics_does_not_change_model_artifact(tmp_path):
    registry = AlphaModelRegistry(tmp_path / "models")
    booster = _train_model(31)
    train, validation = _windows()
    saved = _save_model(registry, booster, training_window=train, validation_window=validation)
    directory = registry.model_path(saved["model_id"])
    before = ((directory / "model.txt").read_bytes(), (directory / "metadata.json").read_bytes())
    diagnose_alpha_model(saved["model_id"], registry=registry)
    after = ((directory / "model.txt").read_bytes(), (directory / "metadata.json").read_bytes())
    assert after == before
