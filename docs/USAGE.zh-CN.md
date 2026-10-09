# 使用说明

[返回 README](../README.zh-CN.md) | [English usage](./USAGE.md)

本文档集中说明安装、数据库初始化、同步命令、MCP 客户端配置、环境变量和代理配置。

## 安装

```bash
npm install -g yahoo-stock-mcp
```

需要 Node.js >= 22.13。无需任何数据库服务：首次使用时自动创建本地 SQLite 文件。

### Codex 仓库插件

先把仓库加入 marketplace：

```bash
codex plugin marketplace add jamespud/yahoo-stock-mcp
```

然后在支持的 Plugins Directory 中安装 **Yahoo Stock MCP**。Plugin 会包含全部 Skills，并通过 portable stdio MCP 配置启动与 `package.json` 相同版本的 npm 包。数据库与代理凭据仍由本地环境配置，插件文件不会保存 secrets。

如果只需要 Skills、不需要随插件加载 MCP server，请按 [`skills/README.md`](../skills/README.md) 使用 Codex 内置 `$skill-installer`。

## 快速开始（npm 全局安装）

包内已自带编译好的 `dist/`，无需本地构建，直接用 `yahoo-stock-mcp` 命令：

```bash

# 0. CLI 基础（无需数据库）
yahoo-stock-mcp --version        # 打印版本号
yahoo-stock-mcp --help           # 打印使用说明（也可用：yahoo-stock-mcp help sync）

# 1.（可选）指定 SQLite 文件位置。
#    不设置时 db:init 使用用户数据目录：
#      Linux   ${XDG_DATA_HOME:-~/.local/share}/yahoo-stock-mcp/stocks.db
#      macOS   ~/Library/Application Support/yahoo-stock-mcp/stocks.db
#      Windows %APPDATA%\yahoo-stock-mcp\stocks.db
#    YAHOO_STOCK_MCP_SQLITE_PATH=/path/to/stocks.db

# 2. 初始化数据库（创建文件并应用全部迁移）。
yahoo-stock-mcp db:init

# 已有数据库升级：只执行尚未应用的迁移
yahoo-stock-mcp db:migrate

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
  db:init               创建本地 SQLite 数据库并应用全部迁移
  db:migrate            对已有数据库执行尚未应用的迁移
  sync                  从 Yahoo Finance / Investing.com 拉取股票数据到 SQLite
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
npm run build       # TypeScript
npm run server      # stdio；其余命令用 npm run sync -- ... 或 npm run dev
npm run db:migrate  # 执行尚未应用的数据库迁移
```

## 数据库迁移

`db/sqlite/migrations/0001_initial.sql` 是唯一 canonical baseline。之后发布的结构变更放在
`db/sqlite/migrations/` 下，按编号顺序执行且发布后不可修改；已应用版本和 SHA-256 checksum 记录在
`schema_migrations`。新数据库使用 `db:init`，已有安装升级使用 `db:migrate`。

SQLite 的 DDL 是事务性的：迁移要么整体生效，要么完全不生效。修改已发布的迁移文件会在下次运行时
因 checksum 不匹配而被拒绝。

## 客户端接入示例（Claude Desktop / Cursor / Codex）

```json
{
  "mcpServers": {
    "yahoo-stock-mcp": {
      "command": "yahoo-stock-mcp",
      "args": ["server"],
      "env": {
        "YAHOO_STOCK_MCP_SQLITE_PATH": "/path/to/stocks.db",
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
- 新闻采用规范化存储：共享文章元数据写入 `news_articles`，标的关联写入 `instrument_news`；同一篇 Yahoo 文章可以同时关联多个已同步标的，而不会重复保存文章内容。
- Yahoo 历史分红只有在数据源同时提供分红金额和除息日时才写入；缺失日期不会用本机当前日期补造。
- 所有写入均为幂等 upsert（`INSERT ... ON DUPLICATE KEY UPDATE`），可重复执行。
- Node HTTP 请求使用进程级共享限流（默认请求启动间隔 300ms，Yahoo 与 Investing Node transport 共用；并发调用会预留不同发送时隙），Yahoo crumb 缓存 25 分钟。

## 代理配置

`YAHOO_STOCK_MCP_PROXY_URL` 用于配置网络请求的正向代理。

支持：

```text
http://proxy.example:8080
https://proxy.example:443
http://user:password@proxy.example:3128
https://user:password@proxy.example:443
```

Investing GraphQL transport 对 HTTP / HTTPS 正向代理都使用 CONNECT 隧道。URL 中的用户名和密码会作为 HTTP Basic `Proxy-Authorization` 发送到 CONNECT 请求；用户名或密码中含保留字符时应进行 percent-encoding。

代理只是网络连通性配置，不是 challenge solver。若上游在有限的 transport compatibility profile 尝试后仍返回已识别的 challenge / access-denial 响应，provider 调用会失败并显式暴露该失败。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `YAHOO_STOCK_MCP_SQLITE_PATH` | 用户数据目录 | SQLite 数据库文件路径；目录不存在时自动创建 |
| `YAHOO_STOCK_MCP_USER_AGENT` | Chrome 148 UA | 请求指纹 |
| `YAHOO_STOCK_MCP_REQUEST_DELAY_MS` | 300 | 请求间隔限流 |
| `YAHOO_STOCK_MCP_PROXY_URL` | 无 | 所有 Node fetch 请求使用的 HTTP(S) 代理，例如 `http://127.0.0.1:17890`；Yahoo 在大陆需配置 |
| `YAHOO_STOCK_MCP_BARS_START_DATE` | 2000-01-01 | 全量同步起点 |
| `YAHOO_STOCK_MCP_PRIMARY_PROVIDER` | yahoo | 两家都有值时以谁为准（yahoo/investing），另一家只补主源缺失的数据 |
| `YAHOO_STOCK_MCP_NEWS_COUNT` | 20 | 每次抓取的新闻条数 |


## 从 v0.4.0 升级

v0.5.0 已移除 MySQL 支持，且**不提供**自动数据迁移。升级前请先备份 MySQL 数据库；想继续使用本项目，
就重新同步到 SQLite（重新同步不保证恢复全部历史数据），或留在 v0.4.x 继续读取旧数据。设置任何
`YAHOO_STOCK_MCP_DATABASE_URL` / `YAHOO_STOCK_MCP_DB_*` 变量都会让服务**拒绝启动**，而不是静默创建
一个空数据库。
