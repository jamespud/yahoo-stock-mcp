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

这是一个 TypeScript / Node.js MCP server，通过 Yahoo Finance 与 Investing.com 的非官方集成获取股票市场数据，写入外部 MySQL，并通过 MCP tools 查询。

## 文档

- **[安装 / 使用 / 配置](./docs/USAGE.zh-CN.md)**
- **[English usage](./docs/USAGE.md)**
- MCP 与 provider 行为摘要见下文。

## 架构

MCP server 保持为薄查询层 + 同步触发器。MySQL 是外部依赖，通过 `YAHOO_STOCK_MCP_DATABASE_URL` 配置，不随 server 内置。

## 测试

```bash
# 需要本地 MySQL（默认 127.0.0.1:3306，见 deploy/docker-compose.mysql.yml）且已初始化表结构
npm run test:cli        # CLI 行为：version / help / 未知命令处理（无需数据库）
npm run test:providers  # 数据源优先级与 provider 提取逻辑（无需数据库）
npm run test:indicators # 技术指标 fixtures 与边界情况（无需数据库）
npm run test:db         # 查询层：覆盖全部查询函数、LIMIT 绑定回归、边界参数
npm run test:db-bootstrap # 可选缺失数据库 bootstrap 集成测试（需 YAHOO_STOCK_MCP_TEST_ADMIN_DATABASE_URL）
npm run test:mcp        # 协议层：initialize/tools/list/tools/call 全工具端到端 + stdin 关闭退出
npm test                # 五组测试全部执行
```

测试使用独立的 `ZZTEST` 标的，跑完自动清理，不会动已有数据。

## MCP 工具

| 工具 | 说明 |
|---|---|
| `sync_stock` | 全量/增量同步一只股票到 MySQL |
| `search_symbol` | 搜索已入库标的 |
| `get_quote` | 最新行情 + 关键指标 |
| `get_bars` | 历史 K 线（1d/1wk/1mo） |
| `get_profile` | 公司资料 |
| `get_financials` | 三张财务报表 |
| `get_ratios` | 估值/财务比率 |
| `get_dividends` | 分红历史与摘要 |
| `get_analyst_forecast` | 分析师共识与目标价 |
| `get_earnings` | 盈利历史与预测 |
| `get_holders` | 机构持有人 |
| `get_news` | 新闻 |
| `get_options` | 期权链快照（同步入库后查询） |
| `get_option_quote` | 实时拉取期权行情（Yahoo 直连、按需、不依赖本地库）：标的报价 + 可选到期日/行权价/方向过滤 |
| `get_company_events` | 前瞻事件日历：下次财报日 / 电话会 / 除息日 / 派息日（Yahoo calendarEvents + Investing next_release_date） |
| `get_insider_transactions` | 内部人交易：高管/董事买卖、股数、金额（Yahoo insiderTransactions） |
| `get_analyst_actions` | 分析师升级/降级与目标价调整（Yahoo upgradeDowngradeHistory） |
| `get_earnings_trend` | 季度盈利预测趋势：EPS/营收预估、增速、近 7/30/60/90 天修正（Yahoo earningsTrend） |
| `get_recommendation_trend` | 分析师评级趋势（月度 strong buy/buy/hold/sell/strong sell） |
| `get_fund_holders` | 基金持有人（mutual fund ownership，Yahoo fundOwnership） |
| `get_short_interest` | 空头持仓快照：做空股数、short ratio、占流通盘比例（Yahoo defaultKeyStatistics） |
| `get_holder_breakdown` | 持股结构：内部人/机构占比、机构占流通盘、机构数（Yahoo majorHoldersBreakdown） |
| `get_intraday_bars` | 分钟级 K 线（1m/5m/15m/30m/60m，同步入库后查询） |
| `get_indicators` | 从库中 bar 计算 42 个技术指标（SMA/EMA/RSI/MACD/KDJ/BBANDS/ATR/ADX/OBV/…）：返回按日期对齐的序列 + 各通道最新值 |
| `list_indicators` | 指标自描述清单：分组、参数（默认值与范围）、输出通道、暖机长度 |
| `list_sectors` | 板块目录：11 个 GICS 板块 + SPY 基准，映射到 SPDR 板块 ETF |
| `get_sector_performance` | 板块轮动视图：各板块最新价 + 1d/5d/20d 涨跌幅排名 + SPY 基准对比 |
| `get_sector_members` | 板块成分股（板块 ETF topHoldings，含权重） |
| `sync_sectors` | 同步全部板块 ETF 行情（约 30 天 K 线）与成分股 |

## 统一比率指标

Yahoo 与 Investing.com 的字段命名不同，部分百分比字段的单位也不同。同步与查询层会先把常用别名归一为稳定的公开 metric ID，再应用数据源优先级。例如：`pe_ttm`、`pe_forward`、`ps_ttm`、`pb_mrq`、`net_margin_pct_ttm`、`gross_margin_pct_ttm`、`operating_margin_pct_ttm`、`roe_pct_ttm`、`roa_pct_ttm`、`dividend_yield_pct_ann`、`payout_ratio_pct_ttm`。

ID 中带 `_pct_` 的指标统一按“百分点”存储，例如 `25.3` 表示 25.3%。已有数据库中的旧 provider 字段名会在读取时兼容归一；在配置的主数据源内部选择最新观测，副数据源仅用于填补缺失的 canonical metric。

## 技术指标

`get_indicators` 完全用 `daily_bars` / `intraday_bars` 本地计算，不依赖额外数据源，也不需要改表结构。

共 42 个指标，分七组：

- **趋势 / 均线（9）**：SMA、EMA、WMA、DEMA、TEMA、HMA、KAMA、BBANDS、SAR
- **动量（8）**：RSI、MACD、STOCH、KDJ、STOCHRSI、WILLR、CCI、MFI
- **振荡 / 趋势强度（9）**：ADX、ROC、MOM、CMO、TRIX、ULTOSC、AROON、AO、KST
- **量能（6）**：VWAP、OBV、ADL、ADOSC、CMF、FI
- **波动率（5）**：TRANGE、ATR、NATR、STDDEV、ANNVOL
- **价格变换（4）**：TYPPRICE、MEDPRICE、WCLPRICE、AVGPRICE
- **回归（1）**：LINEARREG（回归值、斜率、截距、外推、±k 标准误通道）

约定：

- 默认 `basis=adjusted`，用 `adjClose/close` 等比缩放 OHLC；日内 bar 恒为原始价。
- 公式对齐 TA-Lib 惯例；有意偏离的部分（RSI/STOCH/KDJ/WILLR 在退化窗口的取值、滚动 VWAP、首根 bar 的 TR）在对应实现的注释与指标说明里标注。
- 想知道每个指标的参数与取值范围，直接调 `list_indicators`。

## 数据源

- **Yahoo Finance（非官方集成）：** 日线/分钟线、quoteSummary、期权、新闻、fundamentals-timeseries。
- **Investing.com（非官方集成）：** GraphQL 行情、三表、比率、分红、预测、盈利、公司资料、高管和持有人。
- **K 线仅使用 Yahoo。** 原 Investing TVC/K-line 通道已删除。

### 数据源优先级

默认以 Yahoo 为优先源。同一条 canonical 数据两家都有值时取 Yahoo，Investing 只补 Yahoo 缺失的数据。设置 `YAHOO_STOCK_MCP_PRIMARY_PROVIDER=investing` 可反转优先级。

## 数据清单（Data Checklist）

面向"关注行情、提前布局"场景，在原有个股基本面基础上新增以下数据维度，全部由 **Yahoo quoteSummary / Investing GraphQL** 现有接口获取：

| 维度 | 表 | 数据源 |
|---|---|---|
| 前瞻事件日历 | `company_events` | Yahoo `calendarEvents` + Investing `next_release_date`（下次财报/除息/派息） |
| 内部人交易 | `insider_transactions` | Yahoo `insiderTransactions` |
| 分析师动作 | `analyst_actions` | Yahoo `upgradeDowngradeHistory`（升级/降级/目标价调整） |
| 盈利预测趋势 | `earnings_trend` | Yahoo `earningsTrend`（季度 EPS/营收预估 + 近 7/30/60/90 天修正） |
| 评级趋势 | `recommendation_trend` | Yahoo `recommendationTrend`（月度评级分布） |
| 基金持有人 | `fund_holders` | Yahoo `fundOwnership` |
| 空头持仓 | `short_interest` | Yahoo `defaultKeyStatistics`（sharesShort/shortRatio/占流通盘） |
| 持股结构 | `holder_breakdown` | Yahoo `majorHoldersBreakdown`（内部人/机构占比） |
| 分钟线 | `intraday_bars` | Yahoo chart v8（1m/5m/15m/30m/60m） |
| 板块目录与轮动 | `sectors` / `sector_members` | GICS 11 板块 + SPY 基准，板块 ETF（XLC..XLU/SPY）行情 + `topHoldings` 成分股权重 |

> 指数 / ETF / 跨资产（如 `^GSPC`、`^VIX`、`SPY`、`TLT`）可直接当作标的同步：Yahoo 原生支持指数行情，Investing 侧失败会被自动跳过，不影响 Yahoo 数据落库。

## Investing.com transport 与可用性

`src/providers/investing-transport.ts` 只服务 `https://gql.api.investing.com/graphql`。

transport 使用 Node `net` / `tls`，支持：

- 直连，或 HTTP / HTTPS 正向代理 CONNECT 隧道；
- 从 `YAHOO_STOCK_MCP_PROXY_URL` 读取可选的 Basic proxy credentials；
- 有界的 TLS compatibility profile（先 TLS 1.3，失败后回退 TLS 1.2）；
- 浏览器风格的 HTTP/1.1 header 大小写；
- 使用 Node 自带 HTTP parser 处理 Content-Length/chunked，以及 gzip/deflate/brotli。

这些行为属于上方 Provider 访问规范定义的协议层兼容选项。transport 不求解 challenge 页面。如果全部受支持 profile 均收到已识别的 Cloudflare challenge / access-denial 响应，请求会失败，sync 会把 Investing 组件报告为不可用。

Investing 的可用性仍取决于运行环境。scheduled live-provider canary 现在同时 gate Yahoo 和 Investing，并检查三表单位归一化；它只是低频 contract 健康检查，不会把响应写入项目数据库，也不会发布成数据 feed。

