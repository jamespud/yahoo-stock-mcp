import { config } from "../config.js";
import { ProxyAgent } from "undici";

const dispatcher: any = config.proxyUrl ? new ProxyAgent(config.proxyUrl) : undefined;

const SENSITIVE_QUERY_PARAMS = new Set(["crumb"]);

export function redactUrlForError(raw: string): string {
  try {
    const url = new URL(raw);
    for (const key of SENSITIVE_QUERY_PARAMS) {
      if (url.searchParams.has(key)) url.searchParams.set(key, "[REDACTED]");
    }
    return url.toString();
  } catch {
    // Error formatting must never replace the original failure with a URL parse error.
    return raw;
  }
}

/**
 * Process-wide reservation-based rate limiter.
 *
 * Each waiter atomically reserves the next send slot before yielding, so concurrent callers cannot
 * observe the same timestamp and wake together. The limiter spaces request starts; it does not hold
 * a lock while the network request is in flight.
 */
export class RateLimiter {
  private nextAt = 0;

  constructor(private intervalMs: number) {}

  async wait(): Promise<void> {
    const now = Date.now();
    const scheduledAt = Math.max(now, this.nextAt);
    this.nextAt = scheduledAt + Math.max(0, this.intervalMs);
    const delay = scheduledAt - now;
    if (delay > 0) await new Promise((resolve) => setTimeout(resolve, delay));
  }
}

const limiter = new RateLimiter(config.requestDelayMs);

/** Reserve the next process-wide request slot. Shared by every provider transport. */
export async function applyRateLimit(): Promise<void> {
  await limiter.wait();
}

/**
 * Node fetch with the optional HTTP(S) proxy and the process-wide request limiter applied.
 * Yahoo and Investing Node transport therefore share the same request-start spacing.
 */
export async function httpFetch(url: string, init: RequestInit = {}): Promise<Response> {
  await applyRateLimit();
  return fetch(url, { ...init, dispatcher });
}

export interface HttpOptions {
  method?: "GET" | "POST";
  headers?: Record<string, string>;
  body?: string;
  timeoutMs?: number;
  retries?: number;
  /** Response codes that should not be retried. */
  noRetry?: number[];
}

export async function httpJson<T = any>(url: string, opts: HttpOptions = {}): Promise<T> {
  const {
    method = "GET",
    headers = {},
    body,
    timeoutMs = 30000,
    retries = 3,
    noRetry = [400, 401, 403, 404],
  } = opts;

  let lastErr: Error | null = null;
  for (let attempt = 0; attempt <= retries; attempt++) {
    // Back off first, then reserve a rate-limit slot immediately before the actual retry request.
    if (attempt > 0) {
      await new Promise((r) => setTimeout(r, attempt * attempt * 1000));
    }
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
      const res = await httpFetch(url, {
        method,
        headers: {
          "user-agent": config.userAgent,
          accept: "application/json, */*",
          ...headers,
        },
        body: body ?? undefined,
        signal: ctrl.signal,
      });
      const text = await res.text();
      if (res.status === 429 || (res.status >= 500 && !noRetry.includes(res.status))) {
        lastErr = new Error(`HTTP ${res.status} for ${redactUrlForError(url)}: ${text.slice(0, 200)}`);
        continue;
      }
      if (!res.ok) {
        throw new HttpError(res.status, url, text);
      }
      if (!text) return undefined as T;
      return JSON.parse(text) as T;
    } catch (err: any) {
      if (err instanceof HttpError) throw err;
      if (attempt >= retries) throw err;
      lastErr = err;
    } finally {
      clearTimeout(timer);
    }
  }
  throw lastErr ?? new Error(`request failed: ${redactUrlForError(url)}`);
}

export class HttpError extends Error {
  constructor(
    public status: number,
    public url: string,
    public body: string
  ) {
    super(`HTTP ${status} for ${redactUrlForError(url)}: ${body.slice(0, 300)}`);
  }
}

export async function httpText(url: string, opts: HttpOptions = {}): Promise<string> {
  const {
    method = "GET",
    headers = {},
    body,
    timeoutMs = 30000,
    retries = 3,
    noRetry = [400, 401, 403, 404],
  } = opts;

  let lastErr: Error | null = null;
  for (let attempt = 0; attempt <= retries; attempt++) {
    if (attempt > 0) {
      await new Promise((r) => setTimeout(r, attempt * attempt * 1000));
    }

    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
      const res = await httpFetch(url, {
        method,
        headers: { "user-agent": config.userAgent, ...headers },
        body: body ?? undefined,
        signal: ctrl.signal,
      });
      const text = await res.text();

      if (res.status === 429 || (res.status >= 500 && !noRetry.includes(res.status))) {
        lastErr = new HttpError(res.status, url, text);
        continue;
      }
      if (!res.ok) throw new HttpError(res.status, url, text);
      return text;
    } catch (err: any) {
      if (err instanceof HttpError) throw err;
      if (attempt >= retries) throw err;
      lastErr = err;
    } finally {
      clearTimeout(timer);
    }
  }

  throw lastErr ?? new Error(`request failed: ${url}`);
}
