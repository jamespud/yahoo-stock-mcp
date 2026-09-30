# Yahoo Stock MCP plugin

This directory is generated from the repository's canonical `skills/` tree by
`npm run build:plugin`. Do not edit generated files here directly.

The plugin bundles all repository skills and launches the matching published npm package over stdio:

`npx -y yahoo-stock-mcp@0.4.0 server`

Runtime requirements remain Node.js >= 20 and an external MySQL database. Keep database credentials
and proxy credentials in the local environment or host configuration; they are intentionally not
stored in this plugin package.

Before advertising a repository plugin revision, publish the matching npm package version so the
pinned MCP command can resolve.
