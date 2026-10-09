# Yahoo Stock MCP plugin

This directory is generated from the repository's canonical `skills/` tree by
`npm run build:plugin`. Do not edit generated files here directly.

The plugin bundles all repository skills and launches the matching published npm package over stdio:

`npx -y yahoo-stock-mcp@0.5.0 server`

Runtime requirements are Node.js >= 22.13 and a local SQLite database, which is created
automatically in a per-user data directory. Keep proxy credentials in the local environment or host
configuration; they are intentionally not stored in this plugin package.

Before advertising a repository plugin revision, publish the matching npm package version so the
pinned MCP command can resolve.
