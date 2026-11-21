"""Compare development-period XNYS sessions with an optional independent oracle."""

import argparse
import json
from datetime import date, timedelta
from importlib.metadata import version

import exchange_calendars as xc
import pandas_market_calendars as pmc

from stock_quant.calendar import generate_long_calendar


def compare(start: date, end: date, *, xnys_daily_bounds: bool = False) -> dict:
    # Match the Yahoo adapter's mapped coverage, then apply the data window.
    runtime = generate_long_calendar(calendar_name="XNYS", exchange="XNYS", start_date=start, end_date=end,
                                     xnys_daily_bounds=xnys_daily_bounds)
    runtime_times = {
        s.date: (s.intervals[0].start.strftime("%H:%M:%S"), s.intervals[-1].end.strftime("%H:%M:%S"))
        for s in runtime.sessions if start <= s.date < end
    }
    provider = xc.get_calendar("XNYS", start=start - timedelta(days=7), end=end + timedelta(days=7))
    oracle = pmc.get_calendar("NYSE").schedule(start_date=start, end_date=end - timedelta(days=1))

    def bounds(frame, open_column, close_column):
        return {
            label.date(): (row[open_column].strftime("%H:%M:%S"), row[close_column].strftime("%H:%M:%S"))
            for label, row in frame.iterrows() if start <= label.date() < end
        }

    provider_times = bounds(provider.schedule, "open", "close")
    oracle_times = bounds(oracle, "market_open", "market_close")
    return {
        "start": start.isoformat(),
        "end_exclusive": end.isoformat(),
        "version": runtime.version,
        "oracle_version": version("pandas-market-calendars"),
        "shards": len(runtime.long_calendar_shards),
        "runtime_sessions": len(runtime_times),
        "oracle_sessions": len(oracle_times),
        "same_provider_match": runtime_times == provider_times,
        "date_difference": [d.isoformat() for d in sorted(runtime_times.keys() ^ oracle_times.keys())],
        "time_differences": [
            {"date": d.isoformat(), "runtime_utc": runtime_times[d], "oracle_utc": oracle_times[d]}
            for d in sorted(runtime_times.keys() & oracle_times.keys()) if runtime_times[d] != oracle_times[d]
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xnys-daily-bounds", action="store_true")
    args = parser.parse_args()
    print(json.dumps([compare(date(year, 1, 1), date(2012, 1, 1), xnys_daily_bounds=args.xnys_daily_bounds)
                      for year in (1990, 2002)], indent=2))
