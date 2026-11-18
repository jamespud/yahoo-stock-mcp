"""Closed, versioned factor operator registry. Missing values are never filled with zero."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal

import numpy as np
import pandas as pd

from ...compiler import digest


@dataclass(frozen=True)
class OperatorDefinition:
    name: str
    version: str = "1.0"
    arity: int = 1
    scope: Literal["timeseries", "cross_section", "elementwise"] = "timeseries"
    required: tuple[str, ...] = ()
    allowed: tuple[str, ...] = ()


def _period(params):
    return int(params["period"])


def _lag(series, params):
    return series.shift(_period(params))


def _pct_change(series, params):
    return series.pct_change(_period(params), fill_method=None).replace([np.inf, -np.inf], np.nan)


def _log_return(series, params):
    period = _period(params)
    return np.log(series / series.shift(period)).replace([np.inf, -np.inf], np.nan)


def _rolling_std(series, params):
    return series.rolling(_period(params), min_periods=_period(params)).std()


def _rsi(series, params):
    period = _period(params)
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = gain / loss.replace(0, np.nan)
    result = 100 - 100 / (1 + rs)
    result = result.where(loss != 0, 100.0).where(gain != 0, 50.0)
    return result.where(delta.notna(), np.nan)


def _binary(operation: Callable[[pd.Series, pd.Series], pd.Series]):
    return lambda left, right, _params: operation(left, right).replace([np.inf, -np.inf], np.nan)


_TS = {
    "lag": _lag,
    "pct_change": _pct_change,
    "log_return": _log_return,
    "rolling_mean": lambda s, p: s.rolling(_period(p), min_periods=_period(p)).mean(),
    "rolling_std": _rolling_std,
    "rolling_min": lambda s, p: s.rolling(_period(p), min_periods=_period(p)).min(),
    "rolling_max": lambda s, p: s.rolling(_period(p), min_periods=_period(p)).max(),
    "sma": lambda s, p: s.rolling(_period(p), min_periods=_period(p)).mean(),
    "ema": lambda s, p: s.ewm(span=_period(p), adjust=False, min_periods=1).mean(),
    "rsi": _rsi,
}
_ELEMENTWISE = {
    "add": _binary(lambda a, b: a + b),
    "sub": _binary(lambda a, b: a - b),
    "mul": _binary(lambda a, b: a * b),
    "div": _binary(lambda a, b: a / b.replace(0, np.nan)),
}
_CROSS = {"cs_rank_pct", "cs_zscore", "cs_winsorize"}

OPERATORS: dict[str, OperatorDefinition] = {}
for _name in _TS:
    OPERATORS[_name] = OperatorDefinition(_name, required=("period",), allowed=("period",))
for _name in _ELEMENTWISE:
    OPERATORS[_name] = OperatorDefinition(_name, arity=2, scope="elementwise")
for _name in _CROSS:
    OPERATORS[_name] = OperatorDefinition(
        _name,
        scope="cross_section",
        required=("lower", "upper") if _name == "cs_winsorize" else (),
        allowed=("lower", "upper") if _name == "cs_winsorize" else (),
    )


def operator_version(name: str) -> str:
    definition = OPERATORS[name]
    return f"{definition.name}-{definition.version}-{digest({'name': definition.name, 'arity': definition.arity, 'scope': definition.scope, 'required': definition.required})[-12:]}"


def apply_operator(name: str, inputs: list[pd.Series], params: dict) -> pd.Series:
    if name in _TS:
        return _TS[name](inputs[0], params)
    if name in _ELEMENTWISE:
        return _ELEMENTWISE[name](inputs[0], inputs[1], params)
    raise ValueError(f"Operator {name} requires cross-sectional execution")


def apply_cross_section(name: str, series: pd.Series, decision_at: pd.Series, params: dict) -> pd.Series:
    def transform(group: pd.Series) -> pd.Series:
        valid = group.dropna()
        if name == "cs_rank_pct":
            return valid.rank(method="average", pct=True).reindex(group.index)
        if name == "cs_zscore":
            if len(valid) < 2 or float(valid.std(ddof=0)) == 0:
                return pd.Series(np.nan, index=group.index)
            return ((group - valid.mean()) / valid.std(ddof=0)).reindex(group.index)
        if name == "cs_winsorize":
            if valid.empty:
                return group
            lower = valid.quantile(float(params.get("lower", 0.01)))
            upper = valid.quantile(float(params.get("upper", 0.99)))
            return group.clip(lower=lower, upper=upper)
        raise ValueError(f"Unknown cross-sectional operator {name}")

    return series.groupby(decision_at, sort=True, group_keys=False).transform(transform)
