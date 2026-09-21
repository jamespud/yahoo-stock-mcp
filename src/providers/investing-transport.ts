import http from "node:http";
import net from "node:net";
import tls from "node:tls";
import zlib from "node:zlib";
import { config } from "../config.js";
import { applyRateLimit } from "./http.js";

/**
 * Native Node transport for `https://gql.api.investing.com/graphql`.
 *
 * investing.com sits behind Cloudflare bot management. Node's default TLS ClientHello (undici,
 * https.Agent, plain fetch) is answered with a bare `403`; a version-constrained handshake is
 * accepted. Verified live: `minVersion: "TLSv1.3"` and `maxVersion: "TLSv1.2"` both return 200,
 * while the default 1.2-1.3 range does not. The profile list below is an internal fallback chain —
 * it is deliberately not user-configurable.
 *
 * Only the proxy CONNECT + TLS handshake is hand-rolled; application framing is delegated to
 * Node's own HTTP parser so chunked encoding, Content-Length and header handling stay correct.
 */

export const INVESTING_GQL_HOST = "gql.api.investing.com";

const DEFAULT_TIMEOUT_MS = 45_000;

export interface InvestingTlsProfile {
  readonly name: string;
  readonly options: tls.ConnectionOptions;
}

export const INVESTING_TLS_PROFILES: readonly InvestingTlsProfile[] = [
  { name: "tls1.3", options: { minVersion: "TLSv1.3" } },
  { name: "tls1.2", options: { maxVersion: "TLSv1.2" } },
];

export interface InvestingFetchInit {
  method?: "GET" | "POST";
  headers?: Record<string, string>;
  body?: string;
  timeoutMs?: number;
}

export interface InvestingFetchResult {
  status: number;
  text: string;
  tlsProfile: string;
}

/** `CONNECT host:port HTTP/1.1` request head for a plain-HTTP forward proxy. */
export function buildConnectRequest(
  host: string,
  port: number,
  authorization: string | null = null
): string {
  const lines = [
    `CONNECT ${host}:${port} HTTP/1.1`,
    `Host: ${host}:${port}`,
    "Proxy-Connection: keep-alive",
  ];
  if (authorization) lines.push(`Proxy-Authorization: ${authorization}`);
  return `${lines.join("\r\n")}\r\n\r\n`;
}

/** Status code from a CONNECT response head. Returns 0 when the head is malformed. */
export function parseConnectResponseHead(head: string): number {
  const match = /^HTTP\/1\.[01]\s+(\d{3})/.exec(head);
  return match ? Number(match[1]) : 0;
}

/**
 * Cloudflare answers a blocked request with a bare `403` body or a "Just a moment..." challenge
 * page. Both mean "this handshake was rejected" for our purposes.
 */
export function isCloudflareBlock(status: number, body: string): boolean {
  if (status !== 403) return false;
  const text = body.trim().toLowerCase();
  if (text === "403" || text === "") return true;
  return (
    text.includes("just a moment") ||
    text.includes("challenge-platform") ||
    text.includes("cf-chl-") ||
    text.includes("__cf_chl")
  );
}

/**
 * Cloudflare fingerprints the *case* of HTTP/1.1 header names: lowercase names are answered with a
 * challenge page, browser-cased names are accepted. Node's HTTP client preserves the case it is
 * given, so every outgoing header is normalised here before it reaches the wire.
 */
const CANONICAL_HEADER_CASE: Record<string, string> = {
  host: "Host",
  "user-agent": "User-Agent",
  accept: "Accept",
  "accept-encoding": "Accept-Encoding",
  "accept-language": "Accept-Language",
  "content-type": "Content-Type",
  "content-length": "Content-Length",
  connection: "Connection",
  origin: "Origin",
  referer: "Referer",
  cookie: "Cookie",
};

export function canonicalHeaderName(name: string): string {
  const lower = name.toLowerCase();
  const known = CANONICAL_HEADER_CASE[lower];
  if (known) return known;
  return lower.replace(/(^|-)([a-z])/g, (_match, prefix: string, ch: string) => prefix + ch.toUpperCase());
}

export function decodeBody(body: Buffer, contentEncoding?: string | string[]): string {
  const encoding = (Array.isArray(contentEncoding) ? contentEncoding.join(",") : contentEncoding ?? "")
    .toLowerCase();
  if (!encoding) return body.toString("utf8");
  try {
    if (encoding.includes("br")) return zlib.brotliDecompressSync(body).toString("utf8");
    if (encoding.includes("gzip")) return zlib.gunzipSync(body).toString("utf8");
    if (encoding.includes("deflate")) return zlib.inflateSync(body).toString("utf8");
  } catch {
    // Fall through: an unreadable body is better surfaced than thrown away.
  }
  return body.toString("utf8");
}

export interface ProxyEndpoint {
  host: string;
  port: number;
  secure: boolean;
  /** Precomputed Proxy-Authorization value. Never include it in error messages. */
  authorization: string | null;
}

export function resolveProxyEndpoint(proxyUrl: string | null | undefined): ProxyEndpoint | null {
  if (!proxyUrl) return null;
  const proxy = new URL(proxyUrl);
  if (proxy.protocol !== "http:" && proxy.protocol !== "https:") {
    throw new Error(
      `Unsupported proxy protocol for investing transport: ${proxy.protocol} (expected http: or https:)`
    );
  }
  let authorization: string | null = null;
  if (proxy.username || proxy.password) {
    let username: string;
    let password: string;
    try {
      username = decodeURIComponent(proxy.username);
      password = decodeURIComponent(proxy.password);
    } catch {
      throw new Error("Invalid percent-encoding in proxy credentials");
    }
    authorization = `Basic ${Buffer.from(`${username}:${password}`, "utf8").toString("base64")}`;
  }
  return {
    host: proxy.hostname,
    port: proxy.port ? Number(proxy.port) : proxy.protocol === "https:" ? 443 : 80,
    secure: proxy.protocol === "https:",
    authorization,
  };
}

export function connectRaw(host: string, port: number, timeoutMs: number): Promise<net.Socket> {
  return new Promise((resolve, reject) => {
    const socket = net.connect({ host, port });
    const timer = setTimeout(() => {
      socket.destroy();
      reject(new Error(`investing transport: TCP connect to ${host}:${port} timed out`));
    }, timeoutMs);
    socket.once("connect", () => {
      clearTimeout(timer);
      resolve(socket);
    });
    socket.once("error", (err) => {
      clearTimeout(timer);
      reject(err);
    });
  });
}

/** Open an HTTP or HTTPS forward-proxy tunnel and read the CONNECT response head. */
export function openProxyTunnel(
  proxy: ProxyEndpoint,
  targetHost: string,
  targetPort: number,
  timeoutMs: number
): Promise<net.Socket> {
  return new Promise((resolve, reject) => {
    const raw = net.connect({ host: proxy.host, port: proxy.port });
    let channel: net.Socket = raw;
    let settled = false;

    const timer = setTimeout(() => {
      fail(new Error(`investing transport: proxy CONNECT to ${proxy.host}:${proxy.port} timed out`));
    }, timeoutMs);

    const fail = (err: Error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      channel.destroy();
      if (channel !== raw) raw.destroy();
      reject(err);
    };

    const beginConnect = (socket: net.Socket) => {
      channel = socket;
      let buffered = Buffer.alloc(0);

      const onData = (chunk: Buffer) => {
        buffered = Buffer.concat([buffered, chunk]);
        const end = buffered.indexOf("\r\n\r\n");
        if (end < 0) return;

        socket.removeListener("data", onData);
        const head = buffered.subarray(0, end + 4).toString("latin1");
        const status = parseConnectResponseHead(head);
        if (status !== 200) {
          fail(new Error(`investing transport: proxy CONNECT rejected with HTTP ${status || "?"}`));
          return;
        }

        if (settled) return;
        settled = true;
        clearTimeout(timer);
        resolve(socket);
      };

      socket.on("data", onData);
      socket.once("error", fail);
      socket.write(buildConnectRequest(targetHost, targetPort, proxy.authorization));
    };

    raw.once("connect", () => {
      if (!proxy.secure) {
        beginConnect(raw);
        return;
      }

      // For an HTTPS forward proxy, CONNECT itself travels inside the TLS session to the proxy.
      // Return that wrapped channel so the caller can layer the target-host TLS session over it.
      const wrapped = tls.connect({ socket: raw, servername: proxy.host });
      channel = wrapped;
      wrapped.once("secureConnect", () => beginConnect(wrapped));
      wrapped.once("error", fail);
    });
    raw.once("error", fail);
  });
}

export function openTlsSocket(
  targetHost: string,
  targetPort: number,
  profile: InvestingTlsProfile,
  timeoutMs: number,
  proxy: ProxyEndpoint | null = resolveProxyEndpoint(config.proxyUrl)
): Promise<tls.TLSSocket> {
  const rawPromise = proxy
    ? openProxyTunnel(proxy, targetHost, targetPort, timeoutMs)
    : connectRaw(targetHost, targetPort, timeoutMs);

  return rawPromise.then(
    (raw) =>
      new Promise<tls.TLSSocket>((resolve, reject) => {
        const socket = tls.connect({ socket: raw, servername: targetHost, ...profile.options });
        const timer = setTimeout(() => {
          socket.destroy();
          reject(new Error(`investing transport: TLS handshake (${profile.name}) timed out`));
        }, timeoutMs);
        socket.once("secureConnect", () => {
          clearTimeout(timer);
          resolve(socket);
        });
        socket.once("error", (err) => {
          clearTimeout(timer);
          reject(err);
        });
      })
  );
}

/** Rewrite header names into browser casing while preserving insertion order. */
export function canonicalizeHeaders(headers: Record<string, string>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [name, value] of Object.entries(headers)) {
    out[canonicalHeaderName(name)] = value;
  }
  return out;
}

/** Hand the already-negotiated socket to Node's HTTP parser instead of re-implementing framing. */
class PreconnectedAgent extends http.Agent {
  constructor(private readonly preconnected: net.Socket) {
    super({ keepAlive: false, maxSockets: 1 });
  }
  override createConnection(): net.Socket {
    return this.preconnected;
  }
}

export interface RawResponse {
  status: number;
  headers: http.IncomingHttpHeaders;
  body: Buffer;
}

export function sendOverSocket(
  socket: net.Socket,
  url: string,
  init: InvestingFetchInit,
  timeoutMs: number
): Promise<RawResponse> {
  const target = new URL(url);
  return new Promise((resolve, reject) => {
    const agent = new PreconnectedAgent(socket);
    const req = http.request(
      {
        host: target.hostname,
        port: target.port ? Number(target.port) : 443,
        path: `${target.pathname}${target.search}`,
        method: init.method ?? "GET",
        headers: canonicalizeHeaders({
          host: target.hostname,
          "user-agent": config.userAgent,
          accept: "*/*",
          "accept-encoding": "gzip, deflate, br",
          connection: "close",
          ...init.headers,
        }),
        agent,
      },
      (res) => {
        const chunks: Buffer[] = [];
        res.on("data", (chunk: Buffer) => chunks.push(chunk));
        res.on("end", () =>
          resolve({ status: res.statusCode ?? 0, headers: res.headers, body: Buffer.concat(chunks) })
        );
        res.on("error", reject);
      }
    );
    req.setTimeout(timeoutMs, () => req.destroy(new Error("investing transport: request timed out")));
    req.on("error", reject);
    if (init.body !== undefined) req.write(init.body);
    req.end();
  });
}

/**
 * POST/GET against the Investing GraphQL endpoint, walking the internal TLS profile chain until a
 * handshake is accepted. Throws when every profile fails before producing a response; when all
 * profiles are blocked it returns the blocked response so the caller can format the error.
 */
export async function investingFetch(
  url: string,
  init: InvestingFetchInit = {}
): Promise<InvestingFetchResult> {
  const target = new URL(url);
  if (target.protocol !== "https:") {
    throw new Error(`investing transport requires https; got ${target.protocol}`);
  }
  if (target.hostname !== INVESTING_GQL_HOST) {
    throw new Error(
      `investing transport only serves ${INVESTING_GQL_HOST}; got ${target.hostname}`
    );
  }

  const timeoutMs = init.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  let blocked: InvestingFetchResult | null = null;
  let lastError: unknown = null;

  for (const profile of INVESTING_TLS_PROFILES) {
    await applyRateLimit();
    let socket: tls.TLSSocket | null = null;
    try {
      socket = await openTlsSocket(target.hostname, 443, profile, timeoutMs);
      const res = await sendOverSocket(socket, url, init, timeoutMs);
      const text = decodeBody(res.body, res.headers["content-encoding"]);
      const result: InvestingFetchResult = { status: res.status, text, tlsProfile: profile.name };
      if (!isCloudflareBlock(res.status, text)) return result;
      blocked = result;
    } catch (err) {
      lastError = err;
    } finally {
      socket?.destroy();
    }
  }

  if (blocked) return blocked;
  throw lastError ?? new Error("investing transport: no TLS profile produced a response");
}
