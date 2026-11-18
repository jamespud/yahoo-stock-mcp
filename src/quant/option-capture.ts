import { fetchYahooOptionChain } from "../providers/yahoo.js";

export interface CapturedYahooOptionRows {
  observedAt: string;
  rows: Record<string, unknown>[];
}

export async function captureYahooOptionRows(underlying: string, expirations: string[]): Promise<CapturedYahooOptionRows> {
  const observedAt = new Date().toISOString();
  const initial = await fetchYahooOptionChain(underlying);
  const available = new Set(initial.expirations);
  const missing = expirations.filter(expiration => !available.has(expiration));
  if (missing.length) throw new Error(`DATA_UNAVAILABLE: Yahoo does not list expirations ${missing.join(", ")}`);
  const rows: Record<string, unknown>[] = [];
  for (const expiration of expirations) {
    const dateUnix = Math.floor(Date.parse(`${expiration}T00:00:00Z`) / 1000);
    const chain = await fetchYahooOptionChain(underlying, dateUnix);
    for (const leg of chain.legs.filter(leg => leg.expiration === expiration)) {
      rows.push({
        expiration: leg.expiration,
        optionType: leg.optionType,
        strike: leg.strike,
        bid: leg.bid,
        ask: leg.ask,
        bidSize: leg.bidSize,
        askSize: leg.askSize,
        lastPrice: leg.lastPrice,
        lastTradeDate: leg.lastTradeDate,
        volume: leg.volume,
        openInterest: leg.openInterest,
        impliedVol: leg.impliedVol,
      });
    }
  }
  if (!rows.length) throw new Error("DATA_UNAVAILABLE: Yahoo returned no option rows for the requested expirations");
  return { observedAt, rows };
}
