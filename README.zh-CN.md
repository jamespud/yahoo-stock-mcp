# yahoo-stock-mcp

[![npm version](https://img.shields.io/npm/v/yahoo-stock-mcp.svg)](https://www.npmjs.com/package/yahoo-stock-mcp)

[English](./README.md) | [中文](./README.zh-CN.md)

> **非官方 Provider / 数据使用提示**
>
> 本项目中的 Yahoo Finance 与 Investing.com 集成均为非官方集成。本项目与 Yahoo、Investing.com / Fusion Media 不存在隶属、赞助、背书或授权关系。
>
> 上游数据的访问、存储、复用或再分发可能受服务条款和数据许可限制。用户需要自行判断其使用方式——包括全量历史同步、`sync --all`、本地留存及下游使用——是否符合适用的服务条款、数据许可和法律要求。本项目不会授予任何上游数据权利。
>
> **Provider 访问规范：** transport compatibility 仅可使用协议层的普通兼容选项，例如标准 TLS 版本选择、正常的 HTTP header 大小写以及用户主动配置的正向代理；允许在有限的受支持 TLS profile 之间做 bounded fallback。项目不得实现 CAPTCHA / JavaScript challenge 求解、IP / 账号 / 身份轮换、凭据伪装，或以突破明确拒绝为目的的无限重试。已识别的 challenge / access-denial 响应必须作为 provider 失败显式暴露。
>
> 服务条款可能变化；请根据实际司法辖区和使用场景查看最新的 [Yahoo Terms of Service](https://legal.yahoo.com/us/en/yahoo/terms/otos/) 与 [Investing.com Terms and Conditions](https://www.investing.com/about-us/terms-and-conditions)。

这是一个股票市场数据 MCP server，通过 Yahoo Finance 与 Investing.com 的非官方集成获取数据，写入外部 MySQL，并通过 MCP tools 查询。

## 核心能力

- 同步行情、基本面、比率、分红、盈利、持有人、新闻、期权、事件和板块数据；
- 将归一化后的数据存入 MySQL，并通过 MCP tools 暴露；
- 基于本地 bar 计算 42 个技术指标；
- K 线使用 Yahoo，基本面由 Yahoo + Investing 互补；
- 支持 scheduled live-provider contract 健康检查。

## 文档

- **[使用说明](./docs/USAGE.zh-CN.md)** — 安装、数据库、同步命令、MCP 客户端配置、环境变量和代理配置。
- **[参考说明](./docs/REFERENCE.zh-CN.md)** — MCP tools、测试、指标、统一 metric 和完整数据覆盖。
- **[English usage](./docs/USAGE.md)** / **[English reference](./docs/REFERENCE.md)**

## 数据源

- **Yahoo Finance（非官方）：** K 线、quoteSummary、期权、新闻、fundamentals-timeseries。
- **Investing.com（非官方）：** GraphQL 行情、三表、比率、分红、预测、盈利、公司资料、高管和持有人。
- **K 线仅使用 Yahoo。** 原 Investing TVC/K-line 通道已删除。

默认以 Yahoo 为优先 provider，Investing 只补缺失数据。设置 `YAHOO_STOCK_MCP_PRIMARY_PROVIDER=investing` 可反转优先级。

## Investing 可用性

Investing 集成使用原生 Node transport，带有限的 TLS compatibility profile，并支持可选的 HTTP/HTTPS 正向代理。它不会求解 challenge 页面。若受支持 profile 仍被拒绝或 Investing 不可达，provider 失败会被显式暴露，sync 可能返回 `partial`。

scheduled live-provider canary 同时 gate Yahoo 和 Investing，并检查三表单位归一化。它是低频健康检查，不会把响应写入项目数据库，也不会发布数据 feed。

## 运行要求

- Node.js >= 20
- 外部 MySQL
- provider 访问受上方提示及上游条款约束

安装和命令请继续阅读 **[docs/USAGE.zh-CN.md](./docs/USAGE.zh-CN.md)**。
