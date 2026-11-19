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
