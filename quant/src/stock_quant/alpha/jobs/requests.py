"""Versioned Alpha job requests. No caller-controlled filesystem paths."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, TypeAdapter, model_validator

from ...contracts import StrictModel
from ..contracts import Hash, Identifier, ModelResourceBudget, PriceBasis, QuotePolicy
from ..models.temporal_split import ModelDevelopmentSplit, TemporalSplit


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
    feature_set_id: Hash
    label_name: str = Field(min_length=1, max_length=128)
    target_type: str
    stock_dataset_id: Hash | None = None
    option_dataset_id: Hash | None = None
    horizon_days: int = Field(default=5, ge=1, le=252)
    evaluation_mode: str = Field(default="legacy_inline_test", pattern=r"^(validation_only|legacy_inline_test)$")
    research_protocol_hash: Hash | None = None
    split: TemporalSplit | ModelDevelopmentSplit
    hyperparameters: dict = Field(default_factory=dict)
    random_seed: int = 0
    min_rank_ic: float = 0.03
    resource_budget: ModelResourceBudget = Field(default_factory=ModelResourceBudget)

    @model_validator(mode="after")
    def split_mode(self):
        if self.evaluation_mode == "validation_only" and not isinstance(self.split, ModelDevelopmentSplit):
            raise ValueError("validation_only training requires ModelDevelopmentSplit")
        if self.evaluation_mode == "legacy_inline_test" and not isinstance(self.split, TemporalSplit):
            raise ValueError("legacy_inline_test training requires TemporalSplit")
        return self


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

    @model_validator(mode="after")
    def ordered(self):
        if self.test_start >= self.test_end:
            raise ValueError("Expected test_start < test_end")
        return self


class EvaluateAlphaOOSJob(AlphaJobBase):
    kind: Literal["evaluate_alpha_oos"]
    freeze_id: Hash

    @model_validator(mode="after")
    def canonical_idempotency(self):
        expected = f"alpha_oos:{self.freeze_id}"
        if self.idempotency_key is None:
            self.idempotency_key = expected
        elif self.idempotency_key != expected:
            raise ValueError("evaluate_alpha_oos must use the canonical freeze idempotency key")
        return self


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
    portfolio_config: dict = Field(default_factory=dict)


class BacktestOptionsJob(AlphaJobBase):
    kind: Literal["backtest_options"]
    prediction_set_id: Hash
    option_dataset_id: Hash
    execution_config: dict = Field(default_factory=dict)


AlphaJobRequest = Annotated[
    PrepareStockDatasetJob | CaptureOptionChainJob | PrepareOptionsDatasetJob | ComputeFactorsJob |
    ComputeOptionFactorsJob | TrainAlphaModelJob | FreezeAlphaSelectionJob | EvaluateAlphaOOSJob |
    PredictAlphaJob | BacktestAlphaPortfolioJob | BacktestOptionsJob,
    Field(discriminator="kind"),
]
ALPHA_REQUEST_ADAPTER = TypeAdapter(AlphaJobRequest)
ALPHA_KINDS = {
    "prepare_dataset", "capture_option_chain", "prepare_options_dataset", "compute_factors",
    "compute_option_factors", "train_alpha_model", "freeze_alpha_selection", "evaluate_alpha_oos",
    "predict_alpha", "backtest_alpha_portfolio", "backtest_options",
}
