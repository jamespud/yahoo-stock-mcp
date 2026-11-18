"""Explicit QuantLib pricing boundary with diagnostics instead of invented Greeks."""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from ...contracts import StrictModel
from ..errors import AlphaError, AlphaErrorCode
from .contracts import OptionContract
from .quotes import OptionQuote


class OptionMarketInputs(StrictModel):
    spot: float = Field(gt=0, strict=True)
    observed_at: AwareDatetime


class OptionPricingConfig(StrictModel):
    valuation_at: AwareDatetime
    risk_free_rate: float = Field(default=0.04, ge=-1, le=1, strict=True)
    dividend_yield: float = Field(default=0.0, ge=-1, le=1, strict=True)
    model: Literal["binomial_crr", "black_scholes"] = "binomial_crr"
    steps: int = Field(default=128, ge=8, le=5000, strict=True)
    volatility_source: Literal["quote", "mid_implied"] = "quote"
    day_count: Literal["Actual365Fixed"] = "Actual365Fixed"

    @model_validator(mode="after")
    def valuation(self):
        object.__setattr__(self, "valuation_at", self.valuation_at.astimezone(timezone.utc))
        return self


class OptionAnalytics(StrictModel):
    valid: bool
    price: float | None = None
    implied_volatility: float | None = None
    delta: float | None = None
    gamma: float | None = None
    vega: float | None = None
    theta: float | None = None
    rho: float | None = None
    model: str
    diagnostics: list[str] = Field(default_factory=list)


def _ql_date(value: date):
    import QuantLib as ql
    return ql.Date(value.day, value.month, value.year)


def calculate_option_analytics(
    contract: OptionContract,
    quote: OptionQuote,
    market: OptionMarketInputs,
    config: OptionPricingConfig,
) -> OptionAnalytics:
    try:
        import QuantLib as ql
    except ImportError as error:
        raise AlphaError(AlphaErrorCode.DATA_UNAVAILABLE, "QuantLib option pricing is unavailable") from error
    diagnostics: list[str] = []
    valuation_date = config.valuation_at.date()
    if contract.expiration < valuation_date:
        return OptionAnalytics(valid=False, model=config.model, diagnostics=["EXPIRED"])
    if market.observed_at > config.valuation_at:
        return OptionAnalytics(valid=False, model=config.model, diagnostics=["FUTURE_SPOT"])
    if quote.bid is not None and quote.ask is not None and quote.bid > 0 and quote.ask > 0 and quote.bid > quote.ask:
        return OptionAnalytics(valid=False, model=config.model, diagnostics=["INVALID_OPTION_QUOTE"])
    mid = None
    if quote.bid is not None and quote.ask is not None and quote.bid > 0 and quote.ask > 0:
        mid = (quote.bid + quote.ask) / 2
    volatility = None if config.volatility_source == "mid_implied" else quote.implied_volatility
    ql.Settings.instance().evaluationDate = _ql_date(valuation_date)
    day_count = ql.Actual365Fixed()
    spot = ql.QuoteHandle(ql.SimpleQuote(market.spot))
    dividend = ql.YieldTermStructureHandle(ql.FlatForward(ql.Settings.instance().evaluationDate, config.dividend_yield, day_count))
    riskfree = ql.YieldTermStructureHandle(ql.FlatForward(ql.Settings.instance().evaluationDate, config.risk_free_rate, day_count))
    expiry = _ql_date(contract.expiration)
    payoff = ql.PlainVanillaPayoff(ql.Option.Call if contract.option_type == "CALL" else ql.Option.Put, contract.strike)
    exercise = ql.AmericanExercise(ql.Settings.instance().evaluationDate, expiry) if contract.exercise_style == "american" else ql.EuropeanExercise(expiry)
    vanilla = ql.VanillaOption(payoff, exercise)
    if volatility is None and mid is not None and mid > 0:
        try:
            initial_vol = 0.25
            process = ql.BlackScholesMertonProcess(spot, dividend, riskfree, ql.BlackVolTermStructureHandle(ql.BlackConstantVol(ql.Settings.instance().evaluationDate, ql.NullCalendar(), initial_vol, day_count)))
            volatility = vanilla.impliedVolatility(mid, process, 1e-6, 200, 1e-4, 4.0)
        except Exception:
            return OptionAnalytics(valid=False, model=config.model, diagnostics=["NO_SOLUTION_IV"])
    if volatility is None or not 0 < volatility < 4:
        return OptionAnalytics(valid=False, model=config.model, diagnostics=["NO_SOLUTION_IV"])
    process = ql.BlackScholesMertonProcess(spot, dividend, riskfree, ql.BlackVolTermStructureHandle(ql.BlackConstantVol(ql.Settings.instance().evaluationDate, ql.NullCalendar(), volatility, day_count)))
    if config.model == "black_scholes" and contract.exercise_style == "european":
        vanilla.setPricingEngine(ql.AnalyticEuropeanEngine(process))
    else:
        vanilla.setPricingEngine(ql.BinomialVanillaEngine(process, "crr", config.steps))
    def price_with(*, vol_value: float, rate_value: float, valuation: date) -> float:
        ql.Settings.instance().evaluationDate = _ql_date(valuation)
        bumped_spot = ql.QuoteHandle(ql.SimpleQuote(market.spot))
        bumped_dividend = ql.YieldTermStructureHandle(ql.FlatForward(ql.Settings.instance().evaluationDate, config.dividend_yield, day_count))
        bumped_riskfree = ql.YieldTermStructureHandle(ql.FlatForward(ql.Settings.instance().evaluationDate, rate_value, day_count))
        bumped_process = ql.BlackScholesMertonProcess(bumped_spot, bumped_dividend, bumped_riskfree, ql.BlackVolTermStructureHandle(ql.BlackConstantVol(ql.Settings.instance().evaluationDate, ql.NullCalendar(), vol_value, day_count)))
        bumped = ql.VanillaOption(payoff, ql.AmericanExercise(ql.Settings.instance().evaluationDate, expiry) if contract.exercise_style == "american" else ql.EuropeanExercise(expiry))
        if config.model == "black_scholes" and contract.exercise_style == "european":
            bumped.setPricingEngine(ql.AnalyticEuropeanEngine(bumped_process))
        else:
            bumped.setPricingEngine(ql.BinomialVanillaEngine(bumped_process, "crr", config.steps))
        return float(bumped.NPV())

    try:
        price = float(vanilla.NPV())
        delta = float(vanilla.delta())
        gamma = float(vanilla.gamma())
        try:
            vega = float(vanilla.vega()) / 100.0
        except Exception:
            vega = (price_with(vol_value=volatility + 0.01, rate_value=config.risk_free_rate, valuation=valuation_date)
                    - price_with(vol_value=volatility - 0.01, rate_value=config.risk_free_rate, valuation=valuation_date)) / 2.0
        try:
            theta = float(vanilla.thetaPerDay())
        except Exception:
            from datetime import timedelta
            next_day = valuation_date + timedelta(days=1)
            theta = price_with(vol_value=volatility, rate_value=config.risk_free_rate, valuation=next_day) - price if next_day <= contract.expiration else 0.0
        try:
            rho = float(vanilla.rho()) / 100.0
        except Exception:
            rho = (price_with(vol_value=volatility, rate_value=config.risk_free_rate + 0.0001, valuation=valuation_date)
                   - price_with(vol_value=volatility, rate_value=config.risk_free_rate - 0.0001, valuation=valuation_date)) / 200.0
    except Exception as error:
        return OptionAnalytics(valid=False, model=config.model, diagnostics=["MODEL_NON_CONVERGENCE", str(error)])
    finally:
        ql.Settings.instance().evaluationDate = _ql_date(valuation_date)
    if not price > 0 or not all(value == value for value in (delta, gamma, vega, theta, rho)):
        return OptionAnalytics(valid=False, model=config.model, diagnostics=["MODEL_NON_CONVERGENCE"])
    return OptionAnalytics(
        valid=True, price=price, implied_volatility=float(volatility), delta=delta, gamma=gamma,
        vega=vega, theta=theta, rho=rho, model=config.model, diagnostics=diagnostics,
    )
