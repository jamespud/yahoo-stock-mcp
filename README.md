# yahoo-stock-mcp

[![npm version](https://img.shields.io/npm/v/yahoo-stock-mcp.svg)](https://www.npmjs.com/package/yahoo-stock-mcp)

[English](./README.md) | [中文](./README.zh-CN.md)

> **Unofficial provider integrations / data-use notice**
>
> Yahoo Finance and Investing.com integrations in this project are unofficial. This project is not affiliated with, sponsored by, endorsed by, or authorized by Yahoo or Investing.com / Fusion Media.
>
> Access to, storage of, and reuse or redistribution of upstream data may be restricted by provider terms and data licenses. Users are responsible for determining whether their use — including full-history sync, `sync --all`, local retention, and downstream use — complies with applicable terms, licenses, and law. This project grants no rights to upstream data.
>
> **Provider-access policy:** transport compatibility may use ordinary protocol-level options such as standard TLS version selection, normal HTTP header casing, and a user-configured forward proxy. A bounded fallback across supported TLS profiles is allowed. The project must not implement CAPTCHA or JavaScript challenge solving, IP/account/identity rotation, credential spoofing, or unbounded retry behavior intended to defeat an explicit access denial. Recognized challenge/access-denial responses are surfaced as provider failures.
>
> Provider terms can change. Review the current [Yahoo Terms of Service](https://legal.yahoo.com/us/en/yahoo/terms/otos/) and [Investing.com Terms and Conditions](https://www.investing.com/about-us/terms-and-conditions) for the jurisdiction and use case that apply to you.

An MCP server for stock-market data with unofficial Yahoo Finance and Investing.com integrations, backed by an external MySQL database.

## What it does

- syncs quotes, fundamentals, ratios, dividends, earnings, holders, news, options, events, and sector data;
- stores normalized data in MySQL and exposes it through MCP tools;
- computes 42 technical indicators locally from stored bars;
- uses Yahoo for daily/intraday bars and Yahoo + Investing for complementary fundamentals;
- supports scheduled live-provider contract checks.

## Documentation

- **[Usage](./docs/USAGE.md)** — install, database setup, sync commands, MCP client config, environment variables, and proxy setup.
- **[Reference](./docs/REFERENCE.md)** — MCP tools, tests, indicators, canonical metrics, and detailed data coverage.
- **[中文使用说明](./docs/USAGE.zh-CN.md)** / **[中文参考](./docs/REFERENCE.zh-CN.md)**

## Data sources

- **Yahoo Finance (unofficial):** bars, quoteSummary, options, news, fundamentals-timeseries.
- **Investing.com (unofficial):** GraphQL quotes, financial statements, ratios, dividends, estimates, earnings, profile, executives, and holders.
- **Bars are Yahoo-only.** The former Investing TVC/K-line path has been removed.

Yahoo is the primary provider by default; Investing fills gaps. Set `YAHOO_STOCK_MCP_PRIMARY_PROVIDER=investing` to reverse the precedence.

## Investing availability

The Investing integration uses a native Node transport with a bounded TLS compatibility profile and optional HTTP/HTTPS forward proxy support. It does not solve challenge pages. If supported profiles are rejected or Investing is otherwise unreachable, the provider failure is surfaced explicitly and a sync may report `partial`.

The scheduled live-provider canary hard-gates Yahoo and Investing data-contract/unit regressions. Investing network or access availability may report `DEGRADED` instead of failing the job because hosted-runner egress can be blocked independently of the provider contract. The canary is low-frequency and does not populate the project database or publish a data feed.

## Requirements

- Node.js >= 20
- External MySQL
- Provider access subject to the notices and upstream terms above

For setup and commands, continue with **[docs/USAGE.md](./docs/USAGE.md)**.
