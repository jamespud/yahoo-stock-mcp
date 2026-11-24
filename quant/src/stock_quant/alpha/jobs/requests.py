"""Versioned Alpha job requests. No caller-controlled filesystem paths."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, TypeAdapter, field_validator, model_validator

from ...contracts import StrictModel
from ..contracts import Hash, Identifier, ModelResourceBudget, PriceBasis, QuotePolicy
from ..models.baseline_comparison import BaselineComparisonConfig
from ..models.hyperparameters import INT32_MAX, validate_hyperparameters
from ..models.temporal_split import ModelDevelopmentSplit
from ..models.walk_forward import WalkForwardAlphaRequest


class AlphaJobBase(StrictModel):
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)
    max_seconds: int = Field(default=120, ge=1, le=86400, strict=True)


class PrepareStockDatasetJob(AlphaJobBase):
    kind: Literal["prepare_dataset"]
    source: Identifier = "yahoo"
    symbols: list[Identifier] = Field(min_length=1, max_length=1000)
    start: AwareDatetime
    end: AwareDatetime
    price_basis: PriceBasis = "price_basis_unverified"
    calendar_version: Identifier = "caller-supplied"


class CaptureOptionChainJob(AlphaJobBase):
    kind: Literal["capture_option_chain"]
    source: Literal["yahoo"] = "yahoo"
    underlying: Identifier
    expirations: list[str] = Field(min_length=1, max_length=100)
    observed_at: AwareDatetime
    rows: list[dict] | None = Field(default=None, max_length=5000)
    rows_artifact: Hash | None = None

    @model_validator(mode="after")
    def row_source(self):
        if (self.rows is None) == (self.rows_artifact is None):
            raise ValueError("Provide exactly one of rows or rows_artifact")
        return self


class PrepareOptionsDatasetJob(AlphaJobBase):
    kind: Literal["prepare_options_dataset"]
    source: Literal["yahoo", "thetadata"]
    underlyings: list[Identifier] = Field(min_length=1, max_length=500)
    start: AwareDatetime
    end: AwareDatetime
    quote_policy: QuotePolicy
    provider_dataset: str | None = Field(default=None, max_length=128)
    rows: list[dict] | None = Field(default=None, max_length=10000)
    rows_artifact: Hash | None = None

    @model_validator(mode="after")
    def row_source(self):
        if self.rows is not None and self.rows_artifact is not None:
            raise ValueError("Provide only one of rows or rows_artifact")
        return self


class ComputeFactorsJob(AlphaJobBase):
    kind: Literal["compute_factors"]
    dataset_id: Hash
    factor_spec: dict
    auxiliary_feature_set_ids: list[Hash] = Field(default_factory=list, max_length=8)


class ComputeOptionFactorsJob(AlphaJobBase):
    kind: Literal["compute_option_factors"]
    option_dataset_id: Hash
    factor_spec: dict = Field(default_factory=dict)
    pricing_config: dict = Field(default_factory=dict)
    underlying_price_dataset_id: Hash | None = None


class TrainAlphaModelJob(AlphaJobBase):
    kind: Literal["train_alpha_model"]
    research_trial_id: Hash | None = None
    feature_set_id: Hash
    label_name: str = Field(min_length=1, max_length=128)
    target_type: str
    stock_dataset_id: Hash
    horizon_days: int = Field(default=5, ge=1, le=252)
    evaluation_mode: Literal["validation_only"]
    research_protocol_hash: Hash
    split: ModelDevelopmentSplit
    hyperparameters: dict = Field(default_factory=dict)
    random_seed: int = Field(0, strict=True, ge=-INT32_MAX - 1, le=INT32_MAX)
    min_rank_ic: float = 0.03
    resource_budget: ModelResourceBudget = Field(default_factory=ModelResourceBudget)
    baseline_comparison: BaselineComparisonConfig | None = None

    _validate_hyperparameters = field_validator("hyperparameters")(validate_hyperparameters)


class FreezeAlphaSelectionJob(AlphaJobBase):
    kind: Literal["freeze_alpha_selection"]
    research_protocol_hash: Hash
    candidate_ledger_hash: Hash
    selected_model_ids: list[Hash] = Field(min_length=1, max_length=1)
    test_feature_set_id: Hash
    test_label_source_dataset_id: Hash
    test_start: AwareDatetime
    test_end: AwareDatetime
    selection_reason: str = Field(min_length=1, max_length=1024)
    label_name: str = Field(min_length=1, max_length=128)
    target_type: str = Field(default="stock_return", pattern=r"^(stock_return|option_return|option_iv_change|stock_return_with_option_features)$")
    horizon_days: int = Field(default=5, ge=1, le=252, strict=True)
    label_version: str | None = Field(default=None, pattern=r"^(observed-record-v1|session-aligned-v2)$")

    @model_validator(mode="after")
    def ordered(self):
        if self.test_start >= self.test_end:
            raise ValueError("Expected test_start < test_end")
        return self


class EvaluateAlphaOOSJob(AlphaJobBase):
    kind: Literal["evaluate_alpha_oos"]
    freeze_id: Hash


class PredictAlphaJob(AlphaJobBase):
    kind: Literal["predict_alpha"]
    model_set_id: Hash
    feature_set_id: Hash
    start: AwareDatetime
    end: AwareDatetime


class BacktestAlphaPortfolioJob(AlphaJobBase):
    kind: Literal["backtest_alpha_portfolio"]
    prediction_set_id: Hash
    dataset_id: Hash
    price_attestation: dict | None = None
    corporate_action_dataset: dict | None = None
    stress_cost_multiplier: float = Field(default=1.0, gt=0, strict=True)
    portfolio_config: dict = Field(default_factory=dict)


class BacktestOptionsJob(AlphaJobBase):
    kind: Literal["backtest_options"]
    prediction_set_id: Hash
    option_dataset_id: Hash
    execution_config: dict = Field(default_factory=dict)


AlphaJobRequest = Annotated[
    PrepareStockDatasetJob | CaptureOptionChainJob | PrepareOptionsDatasetJob | ComputeFactorsJob |
    ComputeOptionFactorsJob | TrainAlphaModelJob | WalkForwardAlphaRequest | FreezeAlphaSelectionJob |
    EvaluateAlphaOOSJob | PredictAlphaJob | BacktestAlphaPortfolioJob | BacktestOptionsJob,
    Field(discriminator="kind"),
]
ALPHA_REQUEST_ADAPTER = TypeAdapter(AlphaJobRequest)
ALPHA_KINDS = {
    "prepare_dataset", "capture_option_chain", "prepare_options_dataset", "compute_factors",
    "compute_option_factors", "train_alpha_model", "walk_forward_alpha", "freeze_alpha_selection",
    "evaluate_alpha_oos", "predict_alpha", "backtest_alpha_portfolio", "backtest_options",
}
