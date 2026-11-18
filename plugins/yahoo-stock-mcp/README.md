# Yahoo Stock MCP plugin

This directory is generated from the repository's canonical `skills/` tree by
`npm run build:plugin`. Do not edit generated files here directly.

The plugin bundles all repository skills and launches the matching published npm package over stdio:

`npx -y yahoo-stock-mcp@0.5.1 server`

Runtime requirements are Node.js >= 22.13 and a local SQLite database, which is created
automatically in a per-user data directory. Keep proxy credentials in the local environment or host
configuration; they are intentionally not stored in this plugin package.

The bundle contains ten independently installable skills, including
`quant-strategy-research`, `quant-research-reliability`, and `quant-alpha-research`. The default MCP command
keeps quant disabled and does not require Python. To opt in, configure the host MCP environment
with `QUANT_ENABLED=1`, `QUANT_PYTHON` pointing to the installed quant runtime, and
`QUANT_REGISTRY_PATH` pointing to writable local storage. Reconnect and inspect the actual
tools/list, then call `quant_get_capabilities`; provider coverage and sandbox availability
remain separate checks. Alpha research preserves immutable datasets, PIT factors, frozen models,
Top-K portfolios and executable option-quote rules. Python strategies additionally require an operator-approved immutable
sandbox image and successful isolation preflight. This plugin embeds no runtime paths, secrets
or image credentials, and never places live broker orders.

Before advertising a repository plugin revision, publish the matching npm package version so the
pinned MCP command can resolve.
