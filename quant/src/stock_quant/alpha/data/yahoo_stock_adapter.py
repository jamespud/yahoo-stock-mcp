"""Read-only adapter over the existing local Yahoo SQLite tables. No HTTP client."""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

from ...calendar import generate_calendar
from ..contracts import StockDatasetRequest
from ..errors import AlphaError, AlphaErrorCode
from .partitions import StockBarRow
from .stock_dataset import StockHistoryBatch


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
        calendar = generate_calendar(
            calendar_name=self.calendar_name,
            exchange=self.exchange,
            start_date=request.start.date(),
            end_date=request.end.date(),
        )
        sessions = {session.date: session for session in calendar.sessions}
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
            for symbol in request.symbols:
                instrument = instruments.get(symbol)
                if instrument is None:
                    continue
                query = "SELECT * FROM daily_bars WHERE instrument_id=? AND trade_date>=? AND trade_date<? ORDER BY trade_date"
                for bar in connection.execute(query, (instrument["id"], request.start.date().isoformat(), request.end.date().isoformat())):
                    session = sessions.get(__import__("datetime").date.fromisoformat(bar["trade_date"]))
                    session_dates_by_symbol[symbol].append(__import__("datetime").date.fromisoformat(bar["trade_date"]))
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
                            volume=float(bar["volume"] or 0),
                            adjustment=request.adjustment,
                            source=bar["source"] or "yahoo",
                            source_version="yahoo-local-sqlite-v1",
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
            source_version="yahoo-local-sqlite-v1",
            quality={
                "provider": "yahoo_local_sqlite",
                "missing_symbols": missing_symbols,
                "non_session_bars": non_session,
                "missing_ohlcv_rows": missing_ohlcv,
                "price_basis_unverified": request.price_basis == "price_basis_unverified",
                "calendar_provider": calendar.provider,
                "calendar_provider_version": calendar.provider_version,
                "publication_delay_seconds": self.publication_delay_seconds,
            },
        )
