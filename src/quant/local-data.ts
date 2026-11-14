/** Copy local Yahoo observations into explicit, versioned quant snapshots.
 * The market database does not retain historical publication timestamps. The
 * caller supplies a versioned session schedule and a modeled publication delay;
 * the resulting availability policy is recorded rather than presented as observed.
 */
import { createHash } from "node:crypto";
import { sqliteDatabase } from "../storage/sqlite.js";

export interface Session {
  date: string;
  open: string;
  close: string;
  intervals?: {start: string; end: string}[];
}
export interface ExplicitCalendar {
  exchange: string; timezone: string; version: string; start_date: string; end_date: string;
  sessions: {date: string; intervals: {start: string; end: string}[]}[];
}
export interface LocalSnapshotRequest {
  symbol: string;
  timeframe: "1m" | "5m" | "15m" | "1h" | "1d";
  sessions?: Session[];
  calendar?: ExplicitCalendar;
  calendar_version?: string;
  publication_delay_seconds: number;
  price_basis?: "vendor_ohlc_unverified" | "raw_attested";
  actions?: Record<string, unknown>[];
}

export async function localSnapshot(request: LocalSnapshotRequest) {
  const duration = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1d": 0}[request.timeframe];
  if (Boolean(request.calendar) === Boolean(request.sessions)) throw new Error("SCHEMA_INVALID: supply calendar OR sessions");
  const calendarVersion = request.calendar_version || request.calendar?.version;
  if (!calendarVersion) throw new Error("SCHEMA_INVALID: calendar_version is required with sessions");
  const supplied = request.calendar ? request.calendar.sessions.map(s => ({date: s.date, open: s.intervals[0].start, close: s.intervals[s.intervals.length-1].end, intervals: s.intervals})) : request.sessions!;
  const sessions = [...supplied].sort((a, b) => Date.parse(a.open) - Date.parse(b.open));
  const seen = new Set<string>();
  for (let i = 0; i < sessions.length; i++) {
    const s = sessions[i];
    if (seen.has(s.date) || !Number.isFinite(Date.parse(s.open)) || !Number.isFinite(Date.parse(s.close)) || Date.parse(s.open) >= Date.parse(s.close) || (i > 0 && Date.parse(s.open) < Date.parse(sessions[i-1].close))) throw new Error("SCHEMA_INVALID: duplicate, overlapping or invalid sessions");
    seen.add(s.date);
  }
  if (!sessions.length) throw new Error("SCHEMA_INVALID: session schedule is required");
  const db = await sqliteDatabase();
  const instrument = db.prepare("SELECT id,symbol,exchange,currency FROM instruments WHERE symbol=?").get(request.symbol);
  if (!instrument || !instrument.exchange || !instrument.currency) throw new Error("DATA_UNAVAILABLE: missing instrument/exchange/currency");
  if (request.calendar && request.calendar.exchange !== instrument.exchange) throw new Error("DATA_UNAVAILABLE: calendar exchange differs from instrument");
  let rows: any[];
  if (request.timeframe === "1d") {
    const dates = sessions.map(s => s.date).sort();
    rows = db.prepare("SELECT trade_date,open,high,low,close,volume,source FROM daily_bars WHERE instrument_id=? AND source='yahoo' AND trade_date>=? AND trade_date<=? ORDER BY trade_date LIMIT 10001").all(instrument.id, dates[0], dates[dates.length-1]);
  } else {
    const interval = request.timeframe === "1h" ? "60m" : request.timeframe;
    const asStored = (s: string) => new Date(s).toISOString().slice(0, 19).replace("T", " ");
    rows = db.prepare("SELECT ts,open,high,low,close,volume,source FROM intraday_bars WHERE instrument_id=? AND source='yahoo' AND bar_interval=? AND ts>=? AND ts<? ORDER BY ts LIMIT 10001").all(instrument.id, interval, asStored(sessions[0].open), asStored(sessions[sessions.length-1].close));
  }
  if (!rows.length) throw new Error("DATA_UNAVAILABLE: no Yahoo bars at requested timeframe; no substitution performed");
  if (rows.length > 10000) throw new Error("RESOURCE_LIMIT_EXCEEDED: split the snapshot into a smaller period");
  const version = "sha256:" + createHash("sha256").update(JSON.stringify(rows)).digest("hex");
  const bars = rows.map(row => {
    const start = request.timeframe === "1d" ? null : Date.parse(row.ts.replace(" ", "T") + "Z");
    const session = request.timeframe === "1d" ? sessions.find(s => s.date === row.trade_date) : sessions.find(s => (s.intervals || [{start: s.open, end: s.close}]).some(i => Date.parse(i.start) <= start! && start! < Date.parse(i.end)));
    if (!session) throw new Error("DATA_UNAVAILABLE: bar is outside the supplied session schedule");
    const startMs = start ?? Date.parse(session.open);
    const segment = (session.intervals || [{start: session.open, end: session.close}]).find(i => Date.parse(i.start) <= startMs && startMs < Date.parse(i.end));
    const endMs = request.timeframe === "1d" ? Date.parse(session.close) : Math.min(startMs + duration * 1000, Date.parse(segment!.end));
    const values = Object.fromEntries(["open", "high", "low", "close", "volume"].map(field => {
      const raw = row[field];
      const value = raw === null || raw === undefined || raw === "" ? NaN : Number(raw);
      if (!Number.isFinite(value)) throw new Error(`DATA_UNAVAILABLE: missing/nonfinite ${field}`);
      return [field, value];
    }));
    return {symbol: instrument.symbol, currency: instrument.currency, exchange: instrument.exchange, timeframe: request.timeframe,
      start_time: new Date(startMs).toISOString(), end_time: new Date(endMs).toISOString(),
      available_at: new Date(endMs + request.publication_delay_seconds * 1000).toISOString(),
      ...values, source: "yahoo", adjustment: request.price_basis === "raw_attested" ? "raw" : "unknown", dataset_version: version,
      availability_policy: "modeled_bar_end_plus_delay"};
  });
  return {bars, calendar_version: calendarVersion, calendars: request.calendar ? [request.calendar] : undefined, actions: request.actions};
}
