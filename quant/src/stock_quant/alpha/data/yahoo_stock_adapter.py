"""Read-only adapter over the existing local Yahoo SQLite tables. No HTTP client."""
from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from datetime import timedelta, timezone
from pathlib import Path

from ...calendar import XNYS_DAILY_BOUNDS_VERSION, CalendarShardManifest, generate_long_calendar
from ..contracts import StockDatasetRequest
from ..errors import AlphaError, AlphaErrorCode
from .exchange_mapping import build_exchange_mapping
from .partitions import StockBarRow
from .stock_dataset import StockHistoryBatch


SOURCE_VERSION = "yahoo-local-sqlite-v2-null-volume"


class YahooStockAdapter:
    def __init__(self, database_path: str | Path, *, calendar_name: str = "XNYS", exchange: str = "XNYS", publication_delay_seconds: int = 300):
        self.database_path = Path(database_path)
        self.calendar_name = calendar_name
        self.exchange = exchange
        self.publication_delay_seconds = publication_delay_seconds

    def read(self, request: StockDatasetRequest) -> StockHistoryBatch:
        if request.source != "yahoo":
            raise AlphaError(AlphaErrorCode.DATA_UNAVAILABLE, "YahooStockAdapter only serves yahoo requests")
        if not self.database_path.is_file():
            raise AlphaError(AlphaErrorCode.DATA_UNAVAILABLE, "Local Yahoo database is unavailable")
        xnys_daily_bounds = request.calendar_version == XNYS_DAILY_BOUNDS_VERSION
        if xnys_daily_bounds and (self.calendar_name != "XNYS" or self.exchange != "XNYS"):
            raise AlphaError(AlphaErrorCode.DATA_UNAVAILABLE, f"{XNYS_DAILY_BOUNDS_VERSION} requires an exact XNYS mapping")
        calendar = generate_long_calendar(
            calendar_name=self.calendar_name,
            exchange=self.exchange,
            start_date=request.start.date(),
            end_date=request.end.date(),
            xnys_daily_bounds=xnys_daily_bounds,
        )
        # The catalog normalizes timestamps to UTC. Keep the original calendar
        # inputs only when they cannot be recovered from the stored period and mapping.
        save_shards = (isinstance(calendar, CalendarShardManifest) or calendar.exchange != calendar.calendar_name
                       or any(value.date() != value.astimezone(timezone.utc).date()
                              for value in (request.start, request.end)))
        sessions = {session.date: session for session in calendar.sessions}
        session_dates = [day.isoformat() for day in sessions]
        connection = sqlite3.connect(f"file:{self.database_path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            instruments = {}
            for row in connection.execute(
                f"SELECT id, symbol, exchange, currency FROM instruments WHERE symbol IN ({','.join('?' for _ in request.symbols)})",
                request.symbols,
            ):
                instruments[row["symbol"].upper()] = row
            missing_symbols = [symbol for symbol in request.symbols if symbol not in instruments]
            bars, missing_ohlcv, non_session = [], 0, []
            session_dates_by_symbol: dict[str, list] = defaultdict(list)
            exchange_mappings = {}
            volume_null_count = volume_zero_count = volume_positive_count = volume_invalid_count = 0
            for symbol in request.symbols:
                instrument = instruments.get(symbol)
                if instrument is None:
                    continue
                mapping = build_exchange_mapping(
                    symbol,
                    instrument["exchange"],
                    start_date=request.start.date(),
                    end_date=request.end.date(),
                    xnys_daily_bounds=xnys_daily_bounds,
                )
                if xnys_daily_bounds and mapping["calendar_version"] != calendar.version:
                    raise AlphaError(
                        AlphaErrorCode.DATA_UNAVAILABLE,
                        f"{XNYS_DAILY_BOUNDS_VERSION} requires an exact XNYS mapping",
                        {"symbol": symbol, "source_exchange": instrument["exchange"]},
                    )
                if mapping["calendar_mapping_status"] not in {"VERIFIED", "PROXY_ONLY"}:
                    raise AlphaError(
                        AlphaErrorCode.DATA_UNAVAILABLE,
                        "Source exchange cannot be mapped to a trading calendar",
                        {"symbol": symbol, "source_exchange": instrument["exchange"], "mapping_status": mapping["calendar_mapping_status"]},
                    )
                if mapping["calendar_name"] != calendar.calendar_name or mapping["session_dates"] != session_dates:
                    raise AlphaError(
                        AlphaErrorCode.DATA_UNAVAILABLE,
                        "Per-instrument calendar differs from requested dataset calendar",
                        {"symbol": symbol, "calendar_name": mapping["calendar_name"], "dataset_calendar_name": calendar.calendar_name},
                    )
                exchange_mappings[symbol] = mapping
                query = "SELECT * FROM daily_bars WHERE instrument_id=? AND trade_date>=? AND trade_date<? ORDER BY trade_date"
                for bar in connection.execute(query, (instrument["id"], request.start.date().isoformat(), request.end.date().isoformat())):
                    session = sessions.get(__import__("datetime").date.fromisoformat(bar["trade_date"]))
                    session_dates_by_symbol[symbol].append(__import__("datetime").date.fromisoformat(bar["trade_date"]))
                    raw_volume = bar["volume"]
                    if raw_volume is None:
                        volume_null_count += 1
                        volume = None
                    else:
                        parsed_volume = float(raw_volume)
                        if not math.isfinite(parsed_volume) or parsed_volume < 0:
                            volume_invalid_count += 1
                            volume = None
                        elif parsed_volume == 0:
                            volume_zero_count += 1
                            volume = 0.0
                        else:
                            volume_positive_count += 1
                            volume = parsed_volume
                    if session is None:
                        non_session.append(f"{symbol}:{bar['trade_date']}")
                        continue
                    values = [bar["open"], bar["high"], bar["low"], bar["close"]]
                    if any(value is None for value in values):
                        missing_ohlcv += 1
                        continue
                    end_at = session.intervals[-1].end
                    try:
                        bars.append(StockBarRow(
                            instrument_id=symbol,
                            start_at=session.intervals[0].start,
                            end_at=end_at,
                            available_at=end_at + timedelta(seconds=self.publication_delay_seconds),
                            open=float(bar["open"]), high=float(bar["high"]), low=float(bar["low"]), close=float(bar["close"]),
                            volume=volume,
                            adjustment=request.adjustment,
                            source=bar["source"] or "yahoo",
                            source_version=SOURCE_VERSION,
                            currency=(instrument["currency"] or "USD").upper(),
                        ))
                    except (TypeError, ValueError) as error:
                        raise AlphaError(
                            AlphaErrorCode.DATA_UNAVAILABLE,
                            "Invalid local Yahoo OHLC row",
                            {"symbol": symbol, "trade_date": bar["trade_date"], "reason": str(error)},
                        ) from error
        except sqlite3.Error as error:
            raise AlphaError(AlphaErrorCode.DATA_UNAVAILABLE, "Cannot read local Yahoo database", {"reason": str(error)}) from error
        finally:
            connection.close()
        return StockHistoryBatch(
            rows=bars,
            calendar_version=calendar.version,
            calendar_id=calendar.id,
            calendar_session_dates=[session.date for session in calendar.sessions],
            session_dates_by_symbol=dict(session_dates_by_symbol),
            exchange_mappings=exchange_mappings,
            source_version=SOURCE_VERSION,
            quality={
                "provider": "yahoo_local_sqlite",
                "missing_symbols": missing_symbols,
                "non_session_bars": non_session,
                "missing_ohlcv_rows": missing_ohlcv,
                "volume_null_count": volume_null_count,
                "volume_zero_count": volume_zero_count,
                "volume_positive_count": volume_positive_count,
                "volume_invalid_count": volume_invalid_count,
                "volume_valid_coverage": (
                    (volume_zero_count + volume_positive_count)
                    / max(1, volume_null_count + volume_zero_count + volume_positive_count + volume_invalid_count)
                ),
                "price_basis_declared": request.price_basis,
                "price_basis_unverified": True,
                "price_attestation_status": "BLOCKED",
                "execution_validity_status": "BLOCKED",
                "corporate_action_coverage_status": "UNVERIFIED",
                "calendar_provider": calendar.provider,
                "calendar_provider_version": calendar.provider_version,
                **({"calendar_shards": list(calendar.long_calendar_shards)} if save_shards else {}),
                "calendar_mapping_status_by_symbol": {
                    symbol: mapping["calendar_mapping_status"] for symbol, mapping in sorted(exchange_mappings.items())
                },
                "publication_delay_seconds": self.publication_delay_seconds,
            },
        )
