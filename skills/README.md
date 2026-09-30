# Skills

Agent-facing workflows built on this server's MCP tools. `skills/` is the single source of truth:
there is no second copy of this content, and each client is pointed at this directory rather than
given its own fork of the text.

The division of responsibility is deliberate:

- **MCP tools are the capability layer.** They fetch and normalise quotes, statements, analysts,
  options, news, sectors, and technical indicators.
- **Skills are the workflow layer.** They decide which data a task needs, in what order to call the
  tools, how to treat missing or partially synced data, and how to present the result.

## Contents

| Skill | Purpose |
| --- | --- |
| [`stock-research`](stock-research/SKILL.md) | Standardized single-company report: profile, snapshot, statements, ratios, news. Entry point for broad company questions. |
| [`technical-analysis`](technical-analysis/SKILL.md) | Trend, momentum, volume, and volatility from stored bars, with window selection and warm-up handling. |
| [`earnings-event-research`](earnings-event-research/SKILL.md) | Next report, reported results versus estimates, estimate revisions, and analyst actions. |
| [`stock-data-setup`](stock-data-setup/SKILL.md) | Database init/migration, symbol and sector sync, and diagnosis of provider or database failures. |
| [`sector-rotation`](sector-rotation/SKILL.md) | Benchmark-relative performance across the 11 GICS sector ETFs, plus constituent weights for a sector. |
| [`options-analysis`](options-analysis/SKILL.md) | Option chain inspection: expirations and strikes, per-contract quotes, liquidity screening, and term structure. |
| [`dividend-research`](dividend-research/SKILL.md) | Dividend history, yield and payout ratios, forward ex-dividend/payment dates, and coverage against cash flow. |

The first four are the core set for single-company work; `stock-research` is the entry point and
routes to the focused skills. `sector-rotation`, `options-analysis`, and `dividend-research` answer
the narrower market-structure questions and are not meant to re-derive a company overview.

Shared data discipline lives in [`references/data-policy.md`](references/data-policy.md) and is
referenced by every skill: no fabricated values, explicit as-of dates, disclosure of partial syncs,
and no circumvention of provider access denial.

Screening, backtesting, and trade execution are explicitly out of scope - the server exposes no such
capability.

## Format

Each skill is a directory containing a `SKILL.md` with `name` and `description` YAML frontmatter,
following the open [Agent Skills](https://code.claude.com/docs/en/skills) layout. Longer procedures
live in each skill's `references/` directory and are read only when the task needs them.

Skills are written in English to match the rest of the repository's primary documentation; there is
intentionally no second translated copy to keep in sync.

## Installing into an agent

Each skill is self-contained: its `SKILL.md` and every referenced file live inside the skill
directory. The shared policy is authored once at `skills/references/data-policy.md` and synchronized
into each skill by `npm run sync:skill-references`; CI rejects drift.

**Codex** - use the built-in `$skill-installer` with a GitHub skill path. For example, ask Codex:

```text
Use $skill-installer to install
https://github.com/jamespud/yahoo-stock-mcp/tree/main/skills/stock-research
```

To install all skills in one operation, ask `$skill-installer` to install these repository paths:

```text
skills/stock-research
skills/technical-analysis
skills/earnings-event-research
skills/stock-data-setup
skills/sector-rotation
skills/options-analysis
skills/dividend-research
```

The installer copies each selected directory into `$CODEX_HOME/skills/<skill-name>` (normally
`~/.codex/skills`). Restarting or opening a new Codex turn makes newly installed skills available.

**Claude Code** - personal skills live in `~/.claude/skills/`, project skills in
`.claude/skills/`. Copy or link the individual self-contained skill directories there.

**Other clients** - any agent that implements the Agent Skills convention can read a directory of
`<name>/SKILL.md`. Point it at this folder, or at
`node_modules/yahoo-stock-mcp/skills` after an npm install.

Client discovery rules change over time; check the client's own documentation when a skill does not
appear.

## Verifying the skills

`npm run test:skills` runs in CI and checks the parts that can be verified without a model:

- each skill has `SKILL.md` with valid frontmatter, and `name` matches its directory;
- every `references/...` link in a skill resolves to a real file;
- every skill links the shared data policy;
- every tool named in a skill's `## MCP tools used` section exists in the server's live
  `tools/list` response - a skill cannot reference a tool the server does not expose.

Behavioural verification needs a model. Each skill is expected to hold up in at least these three
situations, with a synced database and an MCP client attached:

| Situation | Expectation |
| --- | --- |
| **Normal data** | The skill calls its listed tools and reports values with as-of dates; it does not answer from memory. |
| **Missing data** | A symbol that was never synced and an empty result set are both disclosed; the skill routes to `stock-data-setup` or states the gap instead of substituting numbers. |
| **Provider failure** | A `partial` sync or an Investing access denial is reported with the failing component named; the analysis continues only on data that synced, and no workaround is proposed. |

Spot-check the transcript for the failure mode this project cares about most: a skill producing a
plausible number that no tool returned.
