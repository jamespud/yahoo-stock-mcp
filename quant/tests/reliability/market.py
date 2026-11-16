"""Seeded artificial prices on a frozen, declared US session schedule.

No provider data or inferred holiday rules. Generation deliberately does not use
production calendar enumeration, so interval tests have an independent reference.
"""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
from random import Random
from zoneinfo import ZoneInfo

from stock_quant.data import MarketBar, Snapshot

VERSION = "phase2-market-v1"
SEED = 80421
SYMBOLS = ("AAPL", "MSFT", "NVDA")
DATES = ("2026-03-04", "2026-03-05", "2026-03-06", "2026-03-09",
         "2026-03-10", "2026-03-11", "2026-10-29", "2026-10-30",
         "2026-11-02", "2026-11-03", "2026-11-25", "2026-11-27")
REGIMES = ("rising", "downtrend", "range", "high_vol_reversal")
MANIFEST = Path(__file__).parents[1] / "fixtures" / "phase2_market_v1.json"


def canonical_hash(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return "sha256:" + sha256(encoded).hexdigest()


def payload(seed=SEED):
    rng = Random(seed)
    zone = ZoneInfo("America/New_York")
    sessions, rows, missing, delayed = [], [], [], []
    for day_index, day in enumerate(DATES):
        start = datetime.fromisoformat(day + "T09:30:00").replace(tzinfo=zone).astimezone(timezone.utc)
        finish = datetime.fromisoformat(day + ("T13:00:00" if day == "2026-11-27" else "T16:00:00")).replace(tzinfo=zone).astimezone(timezone.utc)
        sessions.append({"date": day, "intervals": [{"start": start.isoformat(), "end": finish.isoformat()}]})
        for symbol_index, symbol in enumerate(SYMBOLS):
            for tf, minutes in (("1d", 390), ("1h", 60), ("15m", 15)):
                at, step = start, 0
                while at < finish:
                    end = finish if tf == "1d" else min(at + timedelta(minutes=minutes), finish)
                    identity = {"symbol": symbol, "timeframe": tf, "start_time": at.isoformat()}
                    # One daily and two intraday gaps; no forward fill.
                    gap = (symbol_index, tf, day_index, step) in {(1, "1d", 4, 0), (0, "1h", 3, 2), (2, "15m", 8, 4)}
                    if gap:
                        missing.append(identity)
                    else:
                        regime = day_index // 3
                        base = 90 + symbol_index * 23
                        movement = (day_index * 5, 18 - (day_index - 3) * 6, (1 if day_index % 2 else -1), (-20, 22, -15)[day_index % 3])[regime]
                        # Alternating increments and seeded noise rule out monotonic-only data.
                        opening = round(base + movement + (step % 5 - 2) * .7 + rng.uniform(-.8, .8), 6)
                        closing = round(opening + rng.uniform(-2.5, 2.5), 6)
                        delay = timedelta(minutes=95 if tf != "1d" else 1000) if (symbol_index, tf, day_index, step) in {(0, "15m", 3, 1), (1, "1h", 8, 0), (2, "1d", 2, 0)} else timedelta()
                        if delay:
                            delayed.append(identity)
                        rows.append({**identity, "exchange": "XNAS", "end_time": end.isoformat(),
                                     "available_at": (end + delay).isoformat(), "open": opening,
                                     "high": round(max(opening, closing) + .5, 6), "low": round(min(opening, closing) - .5, 6),
                                     "close": closing, "volume": float(1000 + rng.randrange(4000)),
                                     "source": "artificial_seeded", "adjustment": "raw", "dataset_version": VERSION})
                    at, step = end, step + 1
    calendar = {"exchange": "XNAS", "timezone": "America/New_York", "version": VERSION,
                "start_date": DATES[0], "end_date": DATES[-1], "sessions": sessions}
    return {"version": VERSION, "seed": seed, "bars": rows, "calendars": [calendar],
            "missing": missing, "delayed": delayed,
            "regimes": [{"name": name, "dates": list(DATES[i * 3:i * 3 + 3])} for i, name in enumerate(REGIMES)]}


def snapshot(raw=None):
    raw = payload() if raw is None else raw
    return Snapshot([MarketBar.model_validate(row) for row in raw["bars"]],
                    calendar_version=raw["version"], calendars=raw["calendars"])


def daily_payload(seed=SEED):
    """Compact 180+ session market for EMA30 and four expanding-window folds.

    Independent weekday/explicit-holiday enumeration. Tests compare trading dates
    to the production exchange provider; synthetic prices remain unverified data.
    """
    from datetime import date
    days = []
    day = date(2026, 1, 2)
    holidays = {date.fromisoformat(d) for d in ("2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07")}
    while day <= date(2026, 10, 1):
        if day.weekday() < 5 and day not in holidays:
            days.append(day)
        day += timedelta(days=1)
    rng, zone = Random(seed), ZoneInfo("America/New_York")
    sessions, rows = [], []
    for i, day in enumerate(days):
        start = datetime.combine(day, datetime.min.time()).replace(hour=9, minute=30, tzinfo=zone).astimezone(timezone.utc)
        end = start + timedelta(hours=6, minutes=30)
        sessions.append({"date": day.isoformat(), "intervals": [{"start": start.isoformat(), "end": end.isoformat()}]})
        regime = min(3, i // 45)
        local = i % 45
        for j, symbol in enumerate(SYMBOLS):
            movement = (local * .8, 36 - local * .8, (local % 6 - 3) * .4, (-1 if local < 22 else 1) * (local % 22) * 1.3)[regime]
            opening = round(100 + j * 30 + movement + rng.uniform(-.1, .1), 6)
            close = round(opening + rng.uniform(-.2, .2), 6)
            rows.append({"symbol": symbol, "timeframe": "1d", "exchange": "XNAS", "start_time": start.isoformat(), "end_time": end.isoformat(), "available_at": end.isoformat(), "open": opening, "high": max(opening, close) + .5, "low": min(opening, close) - .5, "close": close, "volume": float(10000 + rng.randrange(1000)), "source": "artificial_seeded", "adjustment": "raw", "dataset_version": "phase2-daily-v1"})
    return {"version": "phase2-daily-v1", "seed": seed, "bars": rows,
            "calendars": [{"exchange": "XNAS", "timezone": "America/New_York", "version": "declared-daily-v1", "start_date": days[0].isoformat(), "end_date": days[-1].isoformat(), "sessions": sessions}],
            "regimes": [{"name": name, "start_index": i * 45, "end_index": len(days) if i == 3 else (i + 1) * 45} for i, name in enumerate(REGIMES)]}
