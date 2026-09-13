import "dotenv/config";
import { parsePrimaryProvider } from "./providers/priority.js";

const ENV_PREFIX = "YAHOO_STOCK_MCP_";

/** Read a project-prefixed env var only (no bare fallback), so generic shell
 *  variables like DATABASE_URL / PROXY_URL / USER_AGENT can never leak in. */
export function env(name: string): string | undefined {
  return process.env[`${ENV_PREFIX}${name}`];
}

function num(v: string | undefined, d: number): number {
  const n = Number(v);
  return Number.isFinite(n) ? n : d;
}

const DEFAULT_DB = "yahoo_stock_mcp";

interface DbTarget {
  host: string;
  port: number;
  user: string;
  password: string;
  database: string;
  url?: string;
}

function parseDatabaseUrl(url: string): DbTarget {
  const u = new URL(url);
  return {
    host: u.hostname,
    port: u.port ? Number(u.port) : 3306,
    user: decodeURIComponent(u.username),
    password: decodeURIComponent(u.password),
    database: decodeURIComponent(u.pathname.replace(/^\/+/, "")) || DEFAULT_DB,
    url,
  };
}

function buildDatabaseConfig(): DbTarget {
  const url = env("DATABASE_URL");
  if (url) return parseDatabaseUrl(url);
  return {
    host: env("DB_HOST") ?? "127.0.0.1",
    port: num(env("DB_PORT"), 3306),
    user: env("DB_USER") ?? "stock",
    password: env("DB_PASSWORD") ?? "stock123",
    database: env("DB_NAME") ?? DEFAULT_DB,
  };
}

const db = buildDatabaseConfig();

export const config = {
  /** External MySQL connection string, e.g. mysql://user:pass@host:3306/yahoo_stock_mcp */
  databaseUrl: db.url ?? `mysql://${encodeURIComponent(db.user)}:${encodeURIComponent(db.password)}@${db.host}:${db.port}/${db.database}`,
  db,
  userAgent:
    env("USER_AGENT") ??
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
  requestDelayMs: num(env("REQUEST_DELAY_MS"), 300),
  barsStartDate: env("BARS_START_DATE") ?? "2000-01-01",
  barsProvider: (env("BARS_PROVIDER") ?? "yahoo") as "yahoo" | "investing",
  /**
   * Which provider is authoritative when both return a value (the other one only fills what the
   * primary lacks). `YAHOO_STOCK_MCP_PRIMARY_PROVIDER=yahoo|investing`, default yahoo.
   */
  primaryProvider: parsePrimaryProvider(env("PRIMARY_PROVIDER")),
  newsCount: num(env("NEWS_COUNT"), 20),
  /** Optional HTTP(S) proxy for all Node fetch requests, e.g. http://127.0.0.1:17890 */
  proxyUrl: env("PROXY_URL")?.trim() || null,
};
