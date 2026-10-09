import "dotenv/config";
import { parsePrimaryProvider } from "./providers/priority.js";
import { isValidIsoDate } from "./validation.js";

const ENV_PREFIX = "YAHOO_STOCK_MCP_";

/** Read a project-prefixed env var only (no bare fallback), so generic shell
 *  variables like DATABASE_URL / PROXY_URL / USER_AGENT can never leak in. */
export function env(name: string): string | undefined {
  return process.env[`${ENV_PREFIX}${name}`];
}

export function parseNumericEnv(
  raw: string | undefined | null,
  fallback: number
): number {
  if (raw == null || raw.trim() === "") return fallback;
  const n = Number(raw);
  return Number.isFinite(n) ? n : fallback;
}

export function parseBoundedNumericEnv(
  name: string,
  raw: string | undefined | null,
  fallback: number,
  opts: { min?: number; max?: number; integer?: boolean } = {}
): number {
  const value = parseNumericEnv(raw, fallback);
  const { min, max, integer = false } = opts;
  if (
    (integer && !Number.isInteger(value)) ||
    (min != null && value < min) ||
    (max != null && value > max)
  ) {
    const constraints = [
      integer ? "an integer" : "a number",
      min != null ? `>= ${min}` : null,
      max != null ? `<= ${max}` : null,
    ].filter(Boolean).join(" ");
    throw new Error(
      `Invalid YAHOO_STOCK_MCP_${name}=${JSON.stringify(raw)}; expected ${constraints}`
    );
  }
  return value;
}

export function parseBarsStartDate(
  raw: string | undefined | null,
  fallback = "2000-01-01"
): string {
  const value = (raw ?? "").trim();
  if (value === "") return fallback;
  if (!isValidIsoDate(value)) {
    throw new Error(
      `Invalid YAHOO_STOCK_MCP_BARS_START_DATE=${JSON.stringify(raw)}; expected a valid YYYY-MM-DD calendar date`
    );
  }
  return value;
}

const DEFAULT_DB = "yahoo_stock_mcp";

/**
 * v0.5.0 removed the MySQL backend. Silently ignoring a leftover MySQL connection string would
 * look exactly like "my data disappeared", so any legacy MySQL configuration is a hard error.
 *
 * v0.5.0 does **not** ship a migration tool (deferred); the message points at the upgrade notes
 * rather than at a program that does not exist.
 */
const LEGACY_MYSQL_ENV = [
  "YAHOO_STOCK_MCP_DATABASE_URL",
  "YAHOO_STOCK_MCP_DB_HOST",
  "YAHOO_STOCK_MCP_DB_PORT",
  "YAHOO_STOCK_MCP_DB_USER",
  "YAHOO_STOCK_MCP_DB_PASSWORD",
  "YAHOO_STOCK_MCP_DB_NAME",
];
const legacyMysqlVar = LEGACY_MYSQL_ENV.find((name) => (process.env[name] ?? "").trim() !== "");
if (legacyMysqlVar) {
  throw new Error(
    `${legacyMysqlVar} is set, but yahoo-stock-mcp v0.5.0 no longer uses MySQL: SQLite is the only ` +
      `backend. Unset the YAHOO_STOCK_MCP_DATABASE_URL / YAHOO_STOCK_MCP_DB_* variables to start with ` +
      `a local SQLite database. v0.5.0 cannot migrate existing MySQL data automatically — see the ` +
      `upgrade notes in README.md. You can keep running v0.4.x to read the old database.`
  );
}



export const config = {
  userAgent:
    env("USER_AGENT") ??
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
  requestDelayMs: parseBoundedNumericEnv("REQUEST_DELAY_MS", env("REQUEST_DELAY_MS"), 300, { min: 0 }),
  barsStartDate: parseBarsStartDate(env("BARS_START_DATE")),
  /**
   * Which provider is authoritative when both return a value (the other one only fills what the
   * primary lacks). `YAHOO_STOCK_MCP_PRIMARY_PROVIDER=yahoo|investing`, default yahoo.
   */
  primaryProvider: parsePrimaryProvider(env("PRIMARY_PROVIDER")),
  newsCount: parseBoundedNumericEnv("NEWS_COUNT", env("NEWS_COUNT"), 20, { min: 0, integer: true }),
  /** Optional HTTP(S) proxy for all Node fetch requests, e.g. http://127.0.0.1:17890 */
  proxyUrl: env("PROXY_URL")?.trim() || null,
};
