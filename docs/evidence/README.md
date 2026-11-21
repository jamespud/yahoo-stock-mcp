# Alpha data-validity evidence

`alpha-data-validity-v1-2.json` is a sanitized, remote-reviewable summary of the 87-symbol Alpha Data & Execution Validity v1.2 audit. It contains dataset/model identities, aggregate counts, gate statuses and test-command evidence, but no raw market prices.

Regenerate it locally with:

```bash
npm run test:quant-data-validity
```

The live report is written to `reports/alpha-data-validity/acceptance.json`; copy the same sanitized structure here when the frozen research inputs or acceptance tests change. Verify the committed attachment with:

```bash
cd docs/evidence
sha256sum -c alpha-data-validity-v1-2.sha256
```

## Alpha scalability v1.3

`alpha-scalability-v1-3.json` records baseline failures, bounded MCP pagination, real development-only engineering runs and historical calendar comparisons. [Historical XNYS reconciliation](alpha-calendar-xnys-v1-3.md) explains the exchange records, corrections and open differences. The 80-stock mixed-venue run uses the legacy calendar; the two exact-NYSE runs use the opt-in `xnys-daily-bounds-v1` policy. These are engineering results, not evidence of tradable returns.

Use `run-alpha-scale-acceptance.mjs` after building, with a read-only Yahoo database, frozen FactorSpec request directory and a new evidence directory. Append `--xnys-only` or `--xnys-only-long` for the exact-NYSE preparation for `[2002-01-01, 2012-01-01)` or `[1990-01-01, 2012-01-01)`, respectively. Run `compare-alpha-calendars.py` with `pandas-market-calendars==5.1.3` available, without and with `--xnys-daily-bounds`, to reproduce the two comparison snapshots.

From this directory verify the sanitized attachment with `sha256sum -c alpha-scalability-v1-3.sha256`.

## Alpha diagnostics v1.3

`alpha-diagnostics-v1-3.json` freezes the independent diagnostics protocol and records deterministic synthetic B1-B5 contract evidence. Its embedded 50-symbol membership and source protocol identity come from the v1.2 frozen protocol; the repository does not read that external path at runtime. The protocol hash covers the symbols, windows, both frozen Validation subperiod gates, D01 `+mom63` / D02 `-ret5` directions, candidate configurations, calendar policy, metric versions, bootstrap settings, environment and budgets.

The attachment distinguishes `SYNTHETIC_CONTRACT_VALIDATION: PASS` from the real-study gate. Real diagnostics were not executed because PR A's historical calendar gate is still `BLOCKED`, so `RESEARCH_DIAGNOSTIC_READINESS` remains `NOT_TESTED`, `REAL_MARKET_TRADING_VALIDITY` remains `BLOCKED`, and Test/OOS execution and candidate promotion remain zero.

Validation independently rehashes and deep-compares the embedded protocol body. It also requires the full `npm test` disclosure to remain `FAILED` with the exact 11 saved baseline-compatible labels, `failure_labels_match_saved_baseline: true`, and `byte_identical_logs: false`; regeneration fails if that evidence is missing or claims the suite is green.

Runtime jobs use an explicit `protocol_mode`. `synthetic_contract` remains explicitly non-v1.3. `frozen_v1_3` requires the exact protocol hash, 50-symbol membership, windows and embargo, D01/D02 signal identity, candidate configuration, mapped calendar, `session-aligned-v2` labels, thresholds and resource budgets. Validation-only physical dataset coverage must remain outside `[2012-01-01, 2015-01-01)`, and frozen folds enforce row, iteration and elapsed-time bounds.

Evidence records the actual clean Git source commit on which acceptance ran plus a deterministic `tracked-source-files-v1` content hash over tracked files outside `docs/evidence/**`, `docs/superpowers/**`, `.superpowers/**` and `reports/**`. Validation always recomputes and requires that content hash. When the recorded commit is available and remains an ancestor, the stronger Git-history check is retained; shallow, squash or rebase checkouts may fall back to the matching content hash. Mismatched source content or evidence still fails. Both Quant CI checkouts use `fetch-depth: 0`.

Run and verify the synthetic acceptance contract with:

```bash
npm run test:quant-alpha-diagnostics
cd docs/evidence
sha256sum -c alpha-diagnostics-v1-3.sha256
```

The runner supports only the frozen synthetic fixture. It does not accept a dataset path or authorize a real diagnostic run.
