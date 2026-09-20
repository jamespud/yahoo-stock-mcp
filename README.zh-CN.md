# yahoo-stock-mcp

[![npm version](https://img.shields.io/npm/v/yahoo-stock-mcp.svg)](https://www.npmjs.com/package/yahoo-stock-mcp)

[English](./README.md) | [中文](./README.zh-CN.md)

MCP server（TypeScript / Node.js）通过 **Yahoo Finance** 和 **Investing.com（GraphQL + TVC）** 获取股票全量信息，持久化到 **外部 MySQL**（通过 `YAHOO_STOCK_MCP_DATABASE_URL` 连接串配置，不随 server 内置），按标的代码查询。

架构上 MCP server 保持轻量：它只是一个薄查询层 + 同步触发器，数据库是完全外部的依赖。

## 安装

```bash
npm install -g yahoo-stock-mcp
```

需要 Node.js >= 20 与一个外部 MySQL（见下方 `.env` 配置）。

## 快速开始（npm 全局安装）

包内已自带编译好的 `dist/` 与 Go sidecar `bin/gqlproxy`，无需再构建，直接用 `yahoo-stock-mcp` 命令：

```bash

# 0. CLI 基础（无需数据库）
yahoo-stock-mcp --version        # 打印版本号
yahoo-stock-mcp --help           # 打印使用说明（也可用：yahoo-stock-mcp help sync）

# 1. 配置外部 MySQL 连接（.env）
#    YAHOO_STOCK_MCP_DATABASE_URL=mysql://user:pass@host:3306/yahoo_stock_mcp
#    本地临时开发库可用 deploy/docker-compose.mysql.yml 起一个：
#    docker compose -f deploy/docker-compose.mysql.yml up -d

# 2. 对配置的数据库初始化表结构
yahoo-stock-mcp db:init

# 3. 全量同步一只股票（从 2000-01-01 开始拉历史 + 全部基本面）
yahoo-stock-mcp sync --symbol NVDA --full

# 之后增量同步（只拉新增数据）
yahoo-stock-mcp sync --symbol NVDA

# 增量同步并同时拉取 15 分钟线（1m/5m/15m/30m/60m）
yahoo-stock-mcp sync --symbol NVDA --intraday 15m

# 同步所有已入库标的
yahoo-stock-mcp sync --all --full

# 同步全部 GICS 板块 ETF 行情 + 成分股（板块轮动数据）
yahoo-stock-mcp sync --sectors

# 4. 启动 MCP server（stdio）
yahoo-stock-mcp server
```

## 命令参考

```text
Usage: yahoo-stock-mcp <command> [options]

Commands:
  server                启动 MCP server（stdio，无参数时默认执行）
  db:init               在配置的数据库中初始化表结构
  sync                  从 Yahoo Finance / Investing.com 拉取股票数据到 MySQL
  version               打印版本号
  help [command]        查看总帮助或某个命令的帮助

Options:
  -h, --help            查看帮助
  -v, --version         打印版本号
```

运行 `yahoo-stock-mcp help sync`（或 `yahoo-stock-mcp sync --help`）查看 sync 的选项。
`--version` / `-v` / `version` 都会输出 `yahoo-stock-mcp <版本号>`。

## 从源码运行（开发 / 贡献）

```bash
npm install
npm run build:all   # TypeScript + Go sidecar
npm run server      # stdio；其余命令用 npm run sync -- ... 或 npm run dev
```

## 测试

```bash
# 需要本地 MySQL（默认 127.0.0.1:3306，见 deploy/docker-compose.mysql.yml）且已初始化表结构
npm run test:cli        # CLI 行为：version / help / 未知命令处理（无需数据库）
npm run test:providers  # 数据源优先级与 provider 提取逻辑（无需数据库）
npm run test:indicators # 技术指标 fixtures 与边界情况（无需数据库）
npm run test:db         # 查询层：覆盖全部查询函数、LIMIT 绑定回归、边界参数
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

- **Yahoo Finance**：K 线（v8 chart）、quoteSummary（需 cookie+crumb）、期权（v7）、新闻（v1 search）、财务（fundamentals-timeseries，免认证）
- **Investing.com**：GraphQL `gql.api.investing.com/graphql`（行情/三表/比率/分红/预测/盈利/公司资料/高管/持有人，免认证）、TVC K 线（carrier token）

### 数据源优先级

默认以 **Yahoo 为权威源**：同一行数据两家都返回时（比率、财务字段、分红、前瞻事件），取 Yahoo 的值，
investing 只补 Yahoo 没给的。设 `YAHOO_STOCK_MCP_PRIMARY_PROVIDER=investing` 可把优先级翻过来。
新建标的时若 Yahoo 已经给出标的身份，就不再调用 investing —— 新增一只股票或整轮板块同步都不会再
等 investing 的 403 重试。

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

## 客户端接入示例（Claude Desktop / Cursor / Codex）

```json
{
  "mcpServers": {
    "yahoo-stock-mcp": {
      "command": "yahoo-stock-mcp",
      "args": ["server"],
      "env": {
        "YAHOO_STOCK_MCP_DATABASE_URL": "mysql://user:pass@host:3306/yahoo_stock_mcp",
        "YAHOO_STOCK_MCP_PROXY_URL": "http://127.0.0.1:17890"
      }
    }
  }
}
```

> `command` 依赖 `yahoo-stock-mcp` 在 PATH 上（npm 全局安装后即满足）；若未全局安装，也可改用源码路径 `node /path/to/yahoo-stock-mcp/dist/cli.js server`。所有配置变量都带 `YAHOO_STOCK_MCP_` 前缀，避免与其它应用的 `DATABASE_URL` / `PROXY_URL` / `USER_AGENT` 冲突。

## 说明

- 全量同步：从 `YAHOO_STOCK_MCP_BARS_START_DATE`（默认 2000-01-01）拉全部日 K + 全部基本面 + 期权快照 + 新闻 + 数据清单（事件/内部人/分析师/盈利趋势/空头/基金等）。
- 增量同步：会围绕 `sync_state.last_bar_date` 重拉一小段近期日 K 并幂等 upsert，用于刷新盘中未收盘 K 线和数据源后续修正；同时刷新行情、比率、预测、新闻、期权快照与数据清单。
- 分钟线：`--intraday <1m|5m|15m|30m|60m>` 拉取最近 7 天分钟 K 到 `intraday_bars`（幂等 upsert）。
- 板块：`sync --sectors` 一键同步 11 个 GICS 板块 ETF（XLC..XLU）+ SPY 基准的行情与 `topHoldings` 成分股，`get_sector_performance` 输出板块轮动排名。
- 期权行情：`get_options` 读取同步入库的快照；`get_option_quote` 每次直接从 Yahoo 按需拉取最新报价（含标的现价、可选到期日、行权价、方向过滤），无需先执行同步。
- 所有写入均为幂等 upsert（`INSERT ... ON DUPLICATE KEY UPDATE`），可重复执行。
- Node HTTP 请求使用进程级共享限流（默认请求启动间隔 300ms，Yahoo 与 Investing Node transport 共用；并发调用会预留不同发送时隙），Yahoo crumb 缓存 25 分钟，TVC token 缓存 25 分钟。

## 关于 investing.com 的 TLS 拦截

investing.com 通过 Cloudflare **TLS 指纹**拦截 Node.js 的请求（HTTP 403），Go 客户端可正常访问。因此项目内置了一个极小的 Go 传输代理 `cmd/gqlproxy`（约 200 行，仅标准库）：

```bash
npm run build:sidecar   # 生成 bin/gqlproxy
```

TS 数据源层默认先试 Node fetch，遇到 403 自动切换到该代理（含持久化 cookie 会话，自动处理 Cloudflare challenge）。从不受指纹拦截的网络访问时无需代理，可设置 `YAHOO_STOCK_MCP_INVESTING_TRANSPORT=node` 强制纯 Node。

```bash
# 完整构建（TypeScript + Go sidecar）
npm run build:all
```

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `YAHOO_STOCK_MCP_DATABASE_URL` | 由 `DB_*` 推导 | 完整 MySQL 连接串，例如 `mysql://user:pass@host:3306/yahoo_stock_mcp`；优先于 `DB_*` |
| `YAHOO_STOCK_MCP_DB_HOST/DB_PORT/DB_USER/DB_PASSWORD/DB_NAME` | 127.0.0.1/3306/stock/stock123/yahoo_stock_mcp | MySQL 连接（未设置 `DATABASE_URL` 时使用） |
| `YAHOO_STOCK_MCP_USER_AGENT` | Chrome 148 UA | 请求指纹 |
| `YAHOO_STOCK_MCP_REQUEST_DELAY_MS` | 300 | 请求间隔限流 |
| `YAHOO_STOCK_MCP_PROXY_URL` | 无 | 所有 Node fetch 请求使用的 HTTP(S) 代理，例如 `http://127.0.0.1:17890`；Yahoo 在大陆需配置 |
| `YAHOO_STOCK_MCP_BARS_START_DATE` | 2000-01-01 | 全量同步起点 |
| `YAHOO_STOCK_MCP_BARS_PROVIDER` | yahoo | K 线来源（yahoo/investing） |
| `YAHOO_STOCK_MCP_PRIMARY_PROVIDER` | yahoo | 两家都有值时以谁为准（yahoo/investing），另一家只补主源缺失的数据 |
| `YAHOO_STOCK_MCP_NEWS_COUNT` | 20 | 每次抓取的新闻条数 |
| `YAHOO_STOCK_MCP_INVESTING_TRANSPORT` | auto | node / go / auto |
| `YAHOO_STOCK_MCP_GQLPROXY_COOKIE_FILE` | .cache/gqlproxy_cookies.txt | sidecar cookie 会话文件 |
