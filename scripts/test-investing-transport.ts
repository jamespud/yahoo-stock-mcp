import assert from "node:assert/strict";
import net from "node:net";
import { once } from "node:events";
import zlib from "node:zlib";
import {
  buildConnectRequest,
  canonicalHeaderName,
  canonicalizeHeaders,
  decodeBody,
  isCloudflareBlock,
  openProxyTunnel,
  openTlsSocket,
  parseConnectResponseHead,
  resolveProxyEndpoint,
  sendOverSocket,
  INVESTING_TLS_PROFILES,
  type ProxyEndpoint,
} from "../src/providers/investing-transport.js";
import {
  INVESTING_FIELD_UNITS,
  investingStatementUnit,
  normalizeInvestingStatementValue,
} from "../src/providers/financial-units.js";
import { parseGqlResponse } from "../src/providers/investing.js";

// ── pure: proxy CONNECT framing ─────────────────────────────────

assert.equal(
  buildConnectRequest("gql.api.investing.com", 443),
  "CONNECT gql.api.investing.com:443 HTTP/1.1\r\nHost: gql.api.investing.com:443\r\nProxy-Connection: keep-alive\r\n\r\n"
);
assert.equal(parseConnectResponseHead("HTTP/1.1 200 Connection established\r\n\r\n"), 200);
assert.equal(parseConnectResponseHead("HTTP/1.0 200 OK\r\n\r\n"), 200);
assert.equal(parseConnectResponseHead("HTTP/1.1 403 Forbidden\r\n\r\n"), 403);
assert.equal(parseConnectResponseHead("garbage"), 0, "malformed CONNECT heads must not parse as success");

assert.equal(
  buildConnectRequest("gql.api.investing.com", 443, "Basic dXNlcjpwYXNz"),
  "CONNECT gql.api.investing.com:443 HTTP/1.1\r\n" +
    "Host: gql.api.investing.com:443\r\n" +
    "Proxy-Connection: keep-alive\r\n" +
    "Proxy-Authorization: Basic dXNlcjpwYXNz\r\n\r\n",
  "authenticated proxy CONNECT must carry Proxy-Authorization"
);

assert.deepEqual(resolveProxyEndpoint("http://proxy.example:8080"), {
  host: "proxy.example",
  port: 8080,
  secure: false,
  authorization: null,
});
assert.deepEqual(resolveProxyEndpoint("https://proxy.example"), {
  host: "proxy.example",
  port: 443,
  secure: true,
  authorization: null,
});
assert.deepEqual(resolveProxyEndpoint("http://alice:p%40ss@proxy.example:3128"), {
  host: "proxy.example",
  port: 3128,
  secure: false,
  authorization: `Basic ${Buffer.from("alice:p@ss").toString("base64")}`,
});
assert.throws(
  () => resolveProxyEndpoint("socks5://proxy.example:1080"),
  /Unsupported proxy protocol/,
  "unsupported proxy schemes must fail fast"
);

// ── pure: Cloudflare block detection ────────────────────────────

assert.equal(isCloudflareBlock(403, "403"), true, "bare 403 body is a block");
assert.equal(isCloudflareBlock(403, ""), true, "empty 403 body is a block");
assert.equal(
  isCloudflareBlock(403, "<html><head><title>Just a moment...</title></head></html>"),
  true,
  "challenge page is a block"
);
assert.equal(isCloudflareBlock(403, '{"errors":[{"message":"nope"}]}'), false, "a real 403 payload is not a block");
assert.equal(isCloudflareBlock(200, "ok"), false);
assert.equal(isCloudflareBlock(500, "403"), false);

// ── pure: header casing (Cloudflare fingerprints HTTP/1.1 header case) ──

assert.equal(canonicalHeaderName("user-agent"), "User-Agent");
assert.equal(canonicalHeaderName("CONTENT-TYPE"), "Content-Type");
assert.equal(canonicalHeaderName("accept-encoding"), "Accept-Encoding");
assert.equal(canonicalHeaderName("x-custom-header"), "X-Custom-Header");
assert.deepEqual(
  canonicalizeHeaders({ host: "h", "user-agent": "ua", "content-type": "application/json" }),
  { Host: "h", "User-Agent": "ua", "Content-Type": "application/json" },
  "header casing must be browser-style and order-preserving"
);

// ── pure: body decoding ─────────────────────────────────────────

assert.equal(decodeBody(Buffer.from("plain"), undefined), "plain");
assert.equal(decodeBody(zlib.gzipSync(Buffer.from("gzipped")), "gzip"), "gzipped");
assert.equal(decodeBody(zlib.deflateSync(Buffer.from("deflated")), "deflate"), "deflated");
assert.equal(decodeBody(zlib.brotliCompressSync(Buffer.from("brotli")), "br"), "brotli");
assert.equal(decodeBody(Buffer.from("raw"), "identity"), "raw");

// ── pure: statement unit normalisation ──────────────────────────

assert.equal(
  normalizeInvestingStatementValue("total_revenues_standard", 416161),
  416161000000,
  "money series are reported in millions"
);
assert.equal(
  normalizeInvestingStatementValue("total_assets", 359241),
  359241000000,
  "balance-sheet money series are reported in millions"
);
assert.equal(
  normalizeInvestingStatementValue("capital_expenditure", -12715),
  -12715000000,
  "negative money series keep their sign"
);
assert.equal(
  normalizeInvestingStatementValue("basic_weighted_average_shares_outstanding", 14948.5),
  14948500000,
  "share counts are reported in millions of shares"
);
assert.equal(
  normalizeInvestingStatementValue("basic_eps_continuing_operations", 7.46),
  7.46,
  "per-share amounts are already absolute"
);
assert.equal(
  normalizeInvestingStatementValue("gross_profit_margin", 46.9051),
  46.9051,
  "percentages are already percentage points"
);
assert.equal(normalizeInvestingStatementValue("total_assets", null), null);
assert.equal(normalizeInvestingStatementValue("total_assets", Number.NaN), null);

assert.equal(investingStatementUnit("unknown_metric", "Free Cash Flow Yield %"), "percent");
assert.equal(investingStatementUnit("unknown_metric", "Diluted EPS"), "perShare");
assert.equal(investingStatementUnit("unknown_metric", "Shares Outstanding"), "shares");
assert.equal(investingStatementUnit("unknown_metric", "Something Else"), "money");
assert.equal(INVESTING_FIELD_UNITS.total_revenues_standard, "money");
assert.equal(INVESTING_FIELD_UNITS.gross_profit_margin, "percent");
assert.equal(INVESTING_TLS_PROFILES.length >= 1, true, "transport needs at least one TLS profile");

// ── pure: GraphQL response handling ─────────────────────────────

assert.deepEqual(parseGqlResponse(200, '{"data":{"ok":1}}'), { ok: 1 });
assert.throws(
  () => parseGqlResponse(200, '{"errors":[{"message":"boom"}]}'),
  /investing gql: boom/,
  "a 200 response carrying GraphQL errors must still fail"
);
assert.throws(() => parseGqlResponse(403, "403"), /HTTP 403/);
assert.throws(
  () => parseGqlResponse(403, "<html><title>Just a moment...</title></html>"),
  /Cloudflare challenge/,
  "challenge responses get a dedicated error message"
);
assert.throws(() => parseGqlResponse(200, "<html>not json</html>"), /was not JSON/);

// ── mock sockets ────────────────────────────────────────────────

type Handler = (socket: net.Socket) => void;

async function withServer(handler: Handler, run: (port: number) => Promise<void>): Promise<void> {
  const server = net.createServer(handler);
  server.on("error", () => {});
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", () => resolve()));
  const address = server.address() as net.AddressInfo;
  try {
    await run(address.port);
  } finally {
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
}

async function connectedSocket(port: number): Promise<net.Socket> {
  const socket = net.connect(port, "127.0.0.1");
  await once(socket, "connect");
  return socket;
}

const GQL_URL = "https://gql.api.investing.com/graphql";
const BODY = '{"query":"{x}"}';

// Content-Length framing
await withServer(
  (socket) => {
    socket.on("data", () => {
      const payload = '{"data":{"framing":"content-length"}}';
      socket.end(
        `HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: ${Buffer.byteLength(payload)}\r\n\r\n${payload}`
      );
    });
  },
  async (port) => {
    const res = await sendOverSocket(await connectedSocket(port), GQL_URL, { method: "POST", body: BODY }, 5000);
    assert.equal(res.status, 200);
    assert.deepEqual(parseGqlResponse(res.status, decodeBody(res.body, res.headers["content-encoding"])), {
      framing: "content-length",
    });
  }
);

// chunked framing
await withServer(
  (socket) => {
    socket.on("data", () => {
      const payload = '{"data":{"framing":"chunked"}}';
      const mid = Math.floor(payload.length / 2);
      const chunks = [payload.slice(0, mid), payload.slice(mid)];
      let out = "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n";
      for (const chunk of chunks) out += `${Buffer.byteLength(chunk).toString(16)}\r\n${chunk}\r\n`;
      socket.end(`${out}0\r\n\r\n`);
    });
  },
  async (port) => {
    const res = await sendOverSocket(await connectedSocket(port), GQL_URL, { method: "POST", body: BODY }, 5000);
    assert.equal(res.status, 200);
    assert.deepEqual(parseGqlResponse(res.status, decodeBody(res.body, res.headers["content-encoding"])), {
      framing: "chunked",
    });
  }
);

// gzip compression
await withServer(
  (socket) => {
    socket.on("data", () => {
      const payload = zlib.gzipSync(Buffer.from('{"data":{"framing":"gzip"}}'));
      const head =
        `HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Encoding: gzip\r\n` +
        `Content-Length: ${payload.length}\r\nConnection: close\r\n\r\n`;
      socket.end(Buffer.concat([Buffer.from(head, "latin1"), payload]));
    });
  },
  async (port) => {
    const res = await sendOverSocket(await connectedSocket(port), GQL_URL, { method: "POST", body: BODY }, 5000);
    assert.equal(res.headers["content-encoding"], "gzip");
    assert.deepEqual(parseGqlResponse(res.status, decodeBody(res.body, res.headers["content-encoding"])), {
      framing: "gzip",
    });
  }
);

// 403 with a bare body
await withServer(
  (socket) => {
    socket.on("data", () =>
      socket.end("HTTP/1.1 403 Forbidden\r\nContent-Length: 3\r\nConnection: close\r\n\r\n403")
    );
  },
  async (port) => {
    const res = await sendOverSocket(await connectedSocket(port), GQL_URL, { method: "POST", body: BODY }, 5000);
    assert.equal(res.status, 403);
    assert.equal(isCloudflareBlock(res.status, decodeBody(res.body)), true);
    assert.throws(() => parseGqlResponse(res.status, decodeBody(res.body)), /HTTP 403/);
  }
);

// socket timeout: server accepts but never answers
await withServer(
  (socket) => {
    socket.on("data", () => {
      /* intentionally silent */
    });
  },
  async (port) => {
    const socket = await connectedSocket(port);
    await assert.rejects(
      () => sendOverSocket(socket, GQL_URL, { method: "POST", body: BODY }, 400),
      /timed out/,
      "a stalled response must fail fast instead of hanging"
    );
  }
);

// CONNECT tunnel accepted
await withServer(
  (socket) => {
    socket.on("data", () => socket.write("HTTP/1.1 200 Connection established\r\n\r\n"));
  },
  async (port) => {
    const proxy: ProxyEndpoint = { host: "127.0.0.1", port, secure: false, authorization: null };
    const tunnel = await openProxyTunnel(proxy, "gql.api.investing.com", 443, 5000);
    assert.ok(tunnel, "an accepted CONNECT must yield a usable socket");
    tunnel.destroy();
  }
);

// CONNECT rejected
await withServer(
  (socket) => {
    socket.on("data", () => socket.end("HTTP/1.1 403 Forbidden\r\n\r\n"));
  },
  async (port) => {
    const proxy: ProxyEndpoint = { host: "127.0.0.1", port, secure: false, authorization: null };
    await assert.rejects(
      () => openProxyTunnel(proxy, "gql.api.investing.com", 443, 5000),
      /proxy CONNECT rejected with HTTP 403/,
      "a rejected CONNECT must surface the proxy status"
    );
  }
);

// TLS handshake against a non-TLS peer
await withServer(
  (socket) => {
    socket.on("data", () => socket.end("this is not a TLS server\r\n"));
  },
  async (port) => {
    await assert.rejects(
      () =>
        openTlsSocket(
          "127.0.0.1",
          port,
          INVESTING_TLS_PROFILES[0],
          5000,
          { host: "127.0.0.1", port, secure: false, authorization: null }
        ),
      "a failed TLS handshake must reject"
    );
  }
);

// CONNECT to an unreachable proxy
await assert.rejects(
  () =>
    openProxyTunnel({ host: "127.0.0.1", port: 1, secure: false, authorization: null }, "gql.api.investing.com", 443, 3000),
  "an unreachable proxy must reject"
);

console.log("investing transport tests OK");
