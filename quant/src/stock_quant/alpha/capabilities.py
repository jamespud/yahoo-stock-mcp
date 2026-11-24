"""Honest, dependency-aware Alpha capability discovery."""
from __future__ import annotations

from collections.abc import Mapping
from importlib.util import find_spec

from ..contracts import StrictModel
from .contracts import FACTOR_OPERATORS, LABEL_TYPES, ProviderCoverage

_MODULES = {
    "polars": "polars",
    "duckdb": "duckdb",
    "lightgbm": "lightgbm",
    "sklearn": "sklearn",
    "scipy": "scipy",
    "QuantLib": "QuantLib",
    "httpx": "httpx",
}


class AlphaCapabilities(StrictModel):
    schema_version: str = "1.0"
    stock_alpha_enabled: bool
    option_snapshot_enabled: bool
    historical_options_enabled: bool
    option_pricing_enabled: bool
    option_backtesting_enabled: bool
    lightgbm_enabled: bool
    walk_forward_validation_enabled: bool
    walk_forward_job_kind: str
    research_experiment_registry_enabled: bool = True
    managed_research_admission_enabled: bool = True
    research_ledger_snapshot_enabled: bool = True
    research_evidence_report_enabled: bool = True
    research_global_trial_history_complete: bool = False
    research_exposure_tracking_complete: bool = False
    supported_model_types: list[str]
    supported_factor_operators: list[str]
    supported_label_types: list[str]
    provider_coverage: dict[str, dict]
    resource_limits: dict[str, int | float]

    @classmethod
    def detect(
        cls,
        *,
        modules: Mapping[str, bool] | None = None,
        provider_coverage: Mapping[str, Mapping] | None = None,
    ) -> AlphaCapabilities:
        installed = dict(modules) if modules is not None else {
            name: find_spec(module) is not None for name, module in _MODULES.items()
        }
        coverage: dict[str, dict] = {
            "yahoo_stock": {"status": "UNAVAILABLE", "reason": "local database coverage has not been verified"},
            "yahoo_options": {"status": "UNAVAILABLE", "reason": "current-snapshot only; not historical option history"},
            "thetadata": {"status": "UNAVAILABLE", "reason": "subscription credentials were not configured"},
        }
        if provider_coverage is not None:
            for provider, raw in provider_coverage.items():
                model = ProviderCoverage.model_validate({"provider": provider, **dict(raw)})
                coverage[provider] = model.model_dump(mode="json", exclude_none=True)
        historical = coverage.get("thetadata", {}).get("status") == "AVAILABLE"
        pricing = bool(installed.get("QuantLib"))
        models = bool(installed.get("lightgbm") and installed.get("sklearn") and installed.get("scipy"))
        return cls(
            stock_alpha_enabled=True,
            option_snapshot_enabled=True,
            historical_options_enabled=historical and bool(installed.get("httpx")),
            option_pricing_enabled=pricing,
            option_backtesting_enabled=historical and pricing and bool(installed.get("httpx")),
            lightgbm_enabled=models,
            walk_forward_validation_enabled=models,
            walk_forward_job_kind="walk_forward_alpha",
            supported_model_types=["lightgbm_regressor"] if models else [],
            supported_factor_operators=list(FACTOR_OPERATORS),
            supported_label_types=list(LABEL_TYPES),
            provider_coverage=coverage,
            resource_limits={
                "max_symbols": 1000,
                "max_factor_nodes": 128,
                "max_training_seconds": 1800,
                "max_walk_forward_folds": 12,
                "max_walk_forward_bootstrap_replicates": 2000,
                "max_memory_bytes": 8 * 1024 * 1024 * 1024,
                "max_artifact_bytes": 16 * 1024 * 1024 * 1024,
                "max_request_bytes": 1048576,
            },
        )
