"""Explicit time sampling and accounting attribution for the shared event kernel."""
from datetime import datetime, timedelta, timezone
import math
import statistics


def analyze(curve, *, initial_cash, start, fills, actions):
    start = start.astimezone(timezone.utc)
    times = [datetime.fromisoformat(row["time"]).astimezone(timezone.utc) for row in curve]
    if not times or start > times[0] or any(a > b for a, b in zip(times, times[1:])):
        raise ValueError("Analytics requires ordered observations after its initial clock")
    end, final = times[-1], curve[-1]
    duration = (end - start).total_seconds()
    warnings = []
    daily = []
    previous_time, previous_equity = start, initial_cash
    index, mark = 0, initial_cash
    midnight = start.replace(hour=0, minute=0, second=0, microsecond=0)
    boundary = midnight + timedelta(days=1) if midnight.date() < datetime.max.date() else None
    boundaries = []
    if (end - start).days > 36600:
        warnings.append("DAILY_SAMPLING_LIMIT: daily risk metrics omitted above 100 years")
    else:
        while boundary is not None and boundary <= end:
            boundaries.append(boundary)
            boundary = boundary + timedelta(days=1) if boundary.date() < datetime.max.date() else None
        if not boundaries or boundaries[-1] < end:
            if end > start:
                boundaries.append(end)
    for boundary in boundaries:
        while index < len(curve) and times[index] <= boundary:
            mark = curve[index]["equity"]
            index += 1
        value = mark / previous_equity - 1 if previous_equity > 0 else None
        if value is not None and not math.isfinite(value):
            value = None
        daily.append({"start": previous_time.isoformat(), "time": boundary.isoformat(), "equity": mark,
                      "return": value, "full_utc_day": (boundary - previous_time).total_seconds() == 86400})
        previous_time, previous_equity = boundary, mark
    complete = [r["return"] for r in daily if r["full_utc_day"]]
    volatility = sharpe = None
    if len(complete) >= 2 and all(v is not None for v in complete):
        deviation = statistics.stdev(complete)
        volatility = deviation * math.sqrt(365)
        if deviation > 0:
            sharpe = statistics.mean(complete) / deviation * math.sqrt(365)
    cagr = None
    if duration > 0:
        if final["equity"] == 0:
            cagr = -1.
        elif final["equity"] > 0:
            try:
                cagr = math.expm1((math.log(final["equity"]) - math.log(initial_cash)) * (365.25 * 86400 / duration))
            except (ValueError, OverflowError):
                warnings.append("CAGR_UNDEFINED: nonfinite extrapolation")
    peak, drawdown = initial_cash, 0.
    for observation in curve:
        peak = max(peak, observation["equity"])
        drawdown = min(drawdown, observation["equity"] / peak - 1)
    metrics = {"total_return": final["equity"] / initial_cash - 1, "max_drawdown": drawdown,
               "cagr": cagr, "annualized_volatility": volatility, "sharpe": sharpe}
    for name, value in metrics.items():
        if value is not None and not math.isfinite(value):
            metrics[name] = None
            warnings.append(f"{name.upper()}_UNDEFINED: nonfinite metric")
    def finite(name, calculation):
        try:
            value = calculation()
            if math.isfinite(value):
                return value
        except (OverflowError, ValueError, ZeroDivisionError):
            pass
        warnings.append(f"{name.upper()}_UNDEFINED: nonfinite attribution")
        return None
    realized = finite("realized_pnl", lambda: math.fsum(p["realized_pnl"] for p in final["positions"].values()))
    unrealized = finite("unrealized_pnl", lambda: math.fsum(p["quantity"] * p["mark"] - p["basis"] for p in final["positions"].values()))
    dividends = finite("dividend_income", lambda: math.fsum(a["amount"] for a in actions if a["kind"] == "dividend_entitlement"))
    traded = finite("gross_traded_notional", lambda: math.fsum(f["quantity"] * f["price"] for f in fills))
    residual = (finite("accounting_residual", lambda: final["equity"] - initial_cash - math.fsum([realized, unrealized, dividends]))
                if all(value is not None for value in (realized, unrealized, dividends)) else None)
    turnover = finite("turnover_to_initial_equity", lambda: traded / initial_cash) if traded is not None else None
    commissions = finite("commission_paid", lambda: math.fsum(f["commission"] for f in fills))
    return {"analytics": {"version": "1.0", "start": start.isoformat(), "end": end.isoformat(), "elapsed_seconds": duration,
                           "metrics": metrics, "full_daily_return_count": len(complete), "warnings": warnings,
                           "definitions": {"daily_returns": "UTC midnight marks carried forward through missing days; partial boundary intervals reported but excluded from risk metrics",
                                           "annualization": "CAGR uses elapsed seconds / 365.25 days; daily volatility uses sample standard deviation times sqrt(365)",
                                           "sharpe": "mean daily simple return / sample standard deviation times sqrt(365); risk-free rate zero; null for fewer than two full days, undefined returns or zero variance",
                                           "drawdown": "market opens, completed-bar publications, corporate actions and terminal account; latest observed raw quote; includes initial cash baseline; one snapshot per ordered timestamp batch",
                                           "turnover": "sum of absolute fill notionals / initial cash; both buys and sells counted; not annualized",
                                           "attribution": "realized and unrealized position PnL include commissions in cost basis; dividend entitlements include paid and outstanding income; commission is a memo, not deducted twice",
                                           "benchmark": "not supplied"}},
            "daily_returns": daily,
            "attribution": {"realized_pnl": realized, "unrealized_pnl": unrealized, "dividend_income": dividends,
                            "net_pnl": final["equity"] - initial_cash,
                            "accounting_residual": residual,
                            "commission_paid": commissions, "gross_traded_notional": traded,
                            "turnover_to_initial_equity": turnover}}
