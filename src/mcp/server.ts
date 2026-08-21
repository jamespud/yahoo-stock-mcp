import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { syncOne, syncSectors } from "../services/sync.service.js";
import { fetchYahooOptionChain } from "../providers/yahoo.js";
import * as q from "../services/query.service.js";
import { closeDb } from "../db.js";

const server = new McpServer({
  name: "yahoo-stock-mcp",
  version: "0.2.0",
});

function text(data: unknown): any {
  return { content: [{ type: "text" as const, text: JSON.stringify(data, null, 2) }] };
}

async function guard(fn: () => Promise<unknown>): Promise<any> {
  try {
    return text(await fn());
  } catch (e: any) {
    return { isError: true, content: [{ type: "text" as const, text: `ERROR: ${e.message}` }] };
  }
}

server.tool(
  "search_symbol",
  "Search locally stored instruments by symbol or name.",
  { query: z.string().describe("Symbol or name fragment, e.g. NVDA or NVIDIA") },
  async ({ query: qry }) => guard(() => q.searchSymbols(qry))
);

server.tool(
  "get_quote",
  "Get latest quote for a stock: price, 52-week range, key ratios, dividend summary.",
  { symbol: z.string().describe("Ticker, e.g. NVDA") },
  async ({ symbol }) =>
    guard(async () => {
      const r = await q.getQuote(symbol);
      if (!r) throw new Error(`instrument not found in DB: ${symbol} (run sync_stock first)`);
      return r;
    })
);

server.tool(
  "get_bars",
  "Get historical OHLCV bars from the database (1d/1wk/1mo).",
  {
    symbol: z.string().describe("Ticker, e.g. NVDA"),
    interval: z.enum(["1d", "1wk", "1mo"]).default("1d"),
    from: z.string().optional().describe("Start date YYYY-MM-DD"),
    to: z.string().optional().describe("End date YYYY-MM-DD"),
    limit: z.number().int().min(1).max(10000).optional().default(1000),
  },
  async ({ symbol, interval, from, to, limit }) =>
    guard(async () => {
      const r = await q.getBars(symbol, interval, from, to, limit);
      if (!r) throw new Error(`instrument not found in DB: ${symbol}`);
      return r;
    })
);

server.tool(
  "get_profile",
  "Get company profile: business summary, sector, industry, employees, address, contact info.",
  { symbol: z.string().describe("Ticker, e.g. NVDA") },
  async ({ symbol }) =>
    guard(async () => {
      const r = await q.getProfile(symbol);
      if (!r) throw new Error(`instrument not found in DB: ${symbol}`);
      return r;
    })
);

server.tool(
  "get_financials",
  "Get financial statements (income statement, balance sheet, cash flow) from the database.",
  {
    symbol: z.string().describe("Ticker, e.g. NVDA"),
    statement: z.enum(["INCOME", "BALANCE", "CASHFLOW"]).optional(),
    period: z.enum(["ANNUAL", "QUARTERLY"]).optional(),
  },
  async ({ symbol, statement, period }) =>
    guard(async () => {
      const r = await q.getFinancials(symbol, statement, period);
      if (!r) throw new Error(`instrument not found in DB: ${symbol}`);
      return r;
    })
);

server.tool(
  "get_ratios",
  "Get valuation and financial ratios (PE, PS, PB, margins, ROE, beta, etc.).",
  { symbol: z.string().describe("Ticker, e.g. NVDA") },
  async ({ symbol }) =>
    guard(async () => {
      const r = await q.getRatios(symbol);
      if (!r) throw new Error(`instrument not found in DB: ${symbol}`);
      return r;
    })
);

server.tool(
  "get_dividends",
  "Get dividend history and dividend summary for a stock.",
  { symbol: z.string().describe("Ticker, e.g. NVDA") },
  async ({ symbol }) =>
    guard(async () => {
      const r = await q.getDividends(symbol);
      if (!r) throw new Error(`instrument not found in DB: ${symbol}`);
      return r;
    })
);

server.tool(
  "get_analyst_forecast",
  "Get analyst consensus, buy/hold/sell counts and price targets.",
  { symbol: z.string().describe("Ticker, e.g. NVDA") },
  async ({ symbol }) =>
    guard(async () => {
      const r = await q.getForecast(symbol);
      if (!r) throw new Error(`instrument not found in DB: ${symbol}`);
      return r;
    })
);

server.tool(
  "get_earnings",
  "Get earnings history and forecasts (EPS and revenue, actual vs estimate).",
  { symbol: z.string().describe("Ticker, e.g. NVDA") },
  async ({ symbol }) =>
    guard(async () => {
      const r = await q.getEarnings(symbol);
      if (!r) throw new Error(`instrument not found in DB: ${symbol}`);
      return r;
    })
);

server.tool(
  "get_holders",
  "Get institutional holders and ownership data.",
  {
    symbol: z.string().describe("Ticker, e.g. NVDA"),
    limit: z.number().int().min(1).max(100).optional().default(20),
  },
  async ({ symbol, limit }) =>
    guard(async () => {
      const r = await q.getHolders(symbol, limit);
      if (!r) throw new Error(`instrument not found in DB: ${symbol}`);
      return r;
    })
);

server.tool(
  "get_news",
  "Get recent news for a stock.",
  {
    symbol: z.string().describe("Ticker, e.g. NVDA"),
    limit: z.number().int().min(1).max(100).optional().default(20),
  },
  async ({ symbol, limit }) =>
    guard(async () => {
      const r = await q.getNews(symbol, limit);
      if (!r) throw new Error(`instrument not found in DB: ${symbol}`);
      return r;
    })
);

server.tool(
  "get_options",
  "Get the latest options chain snapshot for a stock (from Yahoo).",
  {
    symbol: z.string().describe("Ticker, e.g. NVDA"),
    expiration: z.string().optional().describe("Expiration date YYYY-MM-DD"),
  },
  async ({ symbol, expiration }) =>
    guard(async () => {
      const r = await q.getOptions(symbol, expiration);
      if (!r) throw new Error(`instrument not found in DB: ${symbol}`);
      return r;
    })
);

server.tool(
  "get_option_quote",
  "Fetch live options quotes for a stock directly from Yahoo (on-demand, no DB sync needed): underlying quote, available expirations/strikes, and per-contract bid/ask/last/volume/open interest/IV.",
  {
    symbol: z.string().describe("Ticker, e.g. NVDA"),
    expiration: z.string().optional().describe("Expiration date YYYY-MM-DD (default: nearest listed)"),
    type: z.enum(["CALL", "PUT"]).optional().describe("Only return CALL or PUT legs"),
    strike: z.number().optional().describe("Only return legs at this exact strike"),
    limit: z.number().int().min(1).max(2000).optional().default(500).describe("Max legs to return"),
  },
  async ({ symbol, expiration, type, strike, limit }) =>
    guard(async () => {
      let dateUnix: number | undefined;
      if (expiration) {
        dateUnix = Math.floor(new Date(expiration + "T00:00:00Z").getTime() / 1000);
        if (Number.isNaN(dateUnix)) throw new Error(`invalid expiration date: ${expiration}`);
      }
      const chain = await fetchYahooOptionChain(symbol, dateUnix);
      let legs = chain.legs;
      if (type) legs = legs.filter((l) => l.optionType === type);
      if (strike != null) legs = legs.filter((l) => l.strike === strike);
      legs = legs.slice(0, limit);
      return { ...chain, legs };
    })
);

server.tool(
  "get_company_events",
  "Get forward-looking company events: next earnings date, earnings call, ex-dividend and dividend payment dates.",
  {
    symbol: z.string().describe("Ticker, e.g. NVDA"),
    limit: z.number().int().min(1).max(100).optional().default(20),
  },
  async ({ symbol, limit }) =>
    guard(async () => {
      const r = await q.getCompanyEvents(symbol, limit);
      if (!r) throw new Error(`instrument not found in DB: ${symbol} (run sync_stock first)`);
      return r;
    })
);

server.tool(
  "get_insider_transactions",
  "Get insider (officer/director) buy/sell transactions.",
  {
    symbol: z.string().describe("Ticker, e.g. NVDA"),
    limit: z.number().int().min(1).max(100).optional().default(20),
  },
  async ({ symbol, limit }) =>
    guard(async () => {
      const r = await q.getInsiderTransactions(symbol, limit);
      if (!r) throw new Error(`instrument not found in DB: ${symbol}`);
      return r;
    })
);

server.tool(
  "get_analyst_actions",
  "Get analyst upgrades, downgrades and price-target changes.",
  {
    symbol: z.string().describe("Ticker, e.g. NVDA"),
    limit: z.number().int().min(1).max(100).optional().default(20),
  },
  async ({ symbol, limit }) =>
    guard(async () => {
      const r = await q.getAnalystActions(symbol, limit);
      if (!r) throw new Error(`instrument not found in DB: ${symbol}`);
      return r;
    })
);

server.tool(
  "get_earnings_trend",
  "Get quarterly earnings estimate trend: EPS/revenue estimates, growth and recent revisions.",
  { symbol: z.string().describe("Ticker, e.g. NVDA") },
  async ({ symbol }) =>
    guard(async () => {
      const r = await q.getEarningsTrend(symbol);
      if (!r) throw new Error(`instrument not found in DB: ${symbol}`);
      return r;
    })
);

server.tool(
  "get_recommendation_trend",
  "Get analyst recommendation trend (strong buy/buy/hold/sell/strong sell) by period.",
  { symbol: z.string().describe("Ticker, e.g. NVDA") },
  async ({ symbol }) =>
    guard(async () => {
      const r = await q.getRecommendationTrend(symbol);
      if (!r) throw new Error(`instrument not found in DB: ${symbol}`);
      return r;
    })
);

server.tool(
  "get_fund_holders",
  "Get mutual fund / fund ownership positions.",
  {
    symbol: z.string().describe("Ticker, e.g. NVDA"),
    limit: z.number().int().min(1).max(100).optional().default(20),
  },
  async ({ symbol, limit }) =>
    guard(async () => {
      const r = await q.getFundHolders(symbol, limit);
      if (!r) throw new Error(`instrument not found in DB: ${symbol}`);
      return r;
    })
);

server.tool(
  "get_short_interest",
  "Get short interest snapshot: shares short, short ratio, % of float, days to cover.",
  { symbol: z.string().describe("Ticker, e.g. NVDA") },
  async ({ symbol }) =>
    guard(async () => {
      const r = await q.getShortInterest(symbol);
      if (!r) throw new Error(`instrument not found in DB: ${symbol}`);
      return r;
    })
);

server.tool(
  "get_holder_breakdown",
  "Get ownership structure: insider % and institutional % held, float % and count.",
  { symbol: z.string().describe("Ticker, e.g. NVDA") },
  async ({ symbol }) =>
    guard(async () => {
      const r = await q.getHolderBreakdown(symbol);
      if (!r) throw new Error(`instrument not found in DB: ${symbol}`);
      return r;
    })
);

server.tool(
  "get_intraday_bars",
  "Get intraday OHLCV bars (1m/5m/15m/30m/60m) stored in the database. Run sync_stock with mode=intraday_15m etc. to populate.",
  {
    symbol: z.string().describe("Ticker, e.g. NVDA"),
    interval: z.enum(["1m", "5m", "15m", "30m", "60m"]).default("15m"),
    from: z.string().optional().describe("Start datetime YYYY-MM-DD"),
    to: z.string().optional().describe("End datetime YYYY-MM-DD"),
    limit: z.number().int().min(1).max(20000).optional().default(5000),
  },
  async ({ symbol, interval, from, to, limit }) =>
    guard(async () => {
      const r = await q.getIntradayBars(symbol, interval, from, to, limit);
      if (!r) throw new Error(`instrument not found in DB: ${symbol}`);
      return r;
    })
);

server.tool(
  "list_sectors",
  "List the GICS sector catalog (11 sectors + S&P 500 benchmark), each mapped to its SPDR sector ETF.",
  {},
  async () => guard(async () => {
    const r = await q.listSectors();
    if (!r) throw new Error("sector catalog not found (run db:init / sync_sectors)");
    return r;
  })
);

server.tool(
  "get_sector_performance",
  "Sector rotation view: latest price and 1d/5d/20d returns for every GICS sector ETF, ranked by 1-day change, plus the SPY benchmark.",
  {},
  async () => guard(async () => {
    const r = await q.getSectorPerformance();
    if (!r?.sectors.length) throw new Error("no sector data synced yet (run sync_sectors first)");
    return r;
  })
);

server.tool(
  "get_sector_members",
  "Get a sector's top constituents with weights (from the sector ETF's topHoldings).",
  {
    sector: z.string().describe("Sector code, e.g. XLK (Technology) or ETF symbol"),
    limit: z.number().int().min(1).max(200).optional().default(20),
  },
  async ({ sector, limit }) =>
    guard(async () => {
      const code = sector.toUpperCase();
      const r = await q.getSectorMembers(code, limit);
      if (!r) throw new Error(`no members for sector ${code} (run sync_sectors first)`);
      return r;
    })
);

server.tool(
  "sync_sectors",
  "Sync all GICS sector ETFs (quote, ~30d bars) and their top-holding constituents into the database. members=false skips constituent refresh.",
  {
    members: z.boolean().optional().default(true).describe("Also refresh sector_members from ETF topHoldings"),
  },
  async ({ members }) =>
    guard(async () => {
      const r = await syncSectors({ members });
      return { synced: r.length, sectors: r };
    })
);

server.tool(
  "sync_stock",
  "Sync a stock from providers into the local MySQL database. full = complete history from 2000; incremental = only new data.",
  {
    symbol: z.string().describe("Ticker, e.g. NVDA"),
    mode: z.enum(["incremental", "full"]).default("incremental"),
    intraday: z.enum(["1m", "5m", "15m", "30m", "60m"]).optional().describe("Also sync intraday bars at this interval"),
  },
  async ({ symbol, mode, intraday }) =>
    guard(async () => {
      const r = await syncOne(symbol, { full: mode === "full", intraday: intraday ?? null });
      return { synced: symbol, mode, syncedIntraday: intraday ?? null, ...r };
    })
);

export async function startMcpServer(): Promise<void> {
  const transport = new StdioServerTransport();
  // Exit when the client closes stdin. The SDK's stdio transport does not 
  // wire stdin 'end'/'close' to onclose, so the open MySQL pool would
  // otherwise keep the process alive and Close() would hang.
  const shutdown = async () => {
    await closeDb();
    process.exit(0);
  };
  process.stdin.on("end", shutdown);
  process.stdin.on("close", shutdown);
  await server.connect(transport);
  process.on("SIGINT", async () => {
    await closeDb();
    process.exit(0);
  });
  process.on("SIGTERM", async () => {
    await closeDb();
    process.exit(0);
  });
}
