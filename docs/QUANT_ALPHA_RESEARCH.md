# Quant Alpha Research v1

Alpha research is an opt-in extension of the Quant Runtime:

```text
stock/option dataset
  -> FactorSpec DAG / option factors
  -> matured executable labels
  -> deterministic LightGBM model set
  -> frozen predictions
  -> Top-K stock portfolio or long-option backtest
```

Set `QUANT_ENABLED=1`, `QUANT_PYTHON`, `QUANT_REGISTRY_PATH`, and, for stock preparation,
`STOCK_MCP_DB_PATH`. The Alpha catalog uses a sibling `<registry-stem>.alpha` directory; models use
`<registry-stem>.models`. No caller-supplied filesystem paths are accepted by MCP tools.

## Install optional dependencies

```bash
uv sync --project quant
```

`quant/pyproject.toml` declares optional groups:

- `alpha`: Polars, DuckDB, LightGBM, scikit-learn, SciPy
- `options`: QuantLib
- `theta`: HTTPX

The base runtime remains usable without these groups. `quant_get_capabilities` reports which
features and verified providers are actually available.

## Main workflow

1. `quant_prepare_alpha_dataset` creates an immutable, content-addressed stock dataset.
2. `quant_compute_factors` compiles `FactorSpec v1` and publishes a feature panel.
3. `quant_capture_options_snapshot` appends a Yahoo current-observation option snapshot. It cannot be
   used as historical option history.
4. `quant_prepare_options_dataset` is reserved for licensed historical Bid/Ask data.
5. `quant_compute_option_factors` calculates ATM IV, skew, term structure, OI/volume ratios,
   realized volatility, IV-RV spread and Greeks where valid inputs exist.
6. `quant_train_alpha_model` trains only the predefined deterministic LightGBM regressor. It requires
   matured labels, chronological windows, purge/embargo and a frozen test period.
7. `quant_predict_alpha` applies a frozen model only after its training window.
8. `quant_backtest_alpha_portfolio` simulates a constrained Top-K portfolio with next-open stock fills.
9. `quant_backtest_options` simulates long calls/puts at next valid Ask/Bid and leaves missing exits unresolved.

Use `quant_get_job`, `quant_cancel_job` and `quant_get_result` for terminal evidence. List sections
are paginated and must be read with the returned cursor.

## Dataset and model invariants

- Dataset IDs bind normalized manifests and every partition hash.
- Raw partitions are immutable; corruption fails validation.
- Missing bars, labels, quotes and factors are never zero-filled.
- Cross-sectional values use only same-decision eligible rows.
- Model feature name/order/type is frozen in the registry; corrupt models fail closed.
- Rank IC, ICIR, coverage, quantile returns and top-bottom spread retain sample counts and definitions.
- Target weights are separate from actual fills and positions; cash + positions must reconcile.

## Provider boundaries

Yahoo stock data is local and normally `price_basis_unverified`. Yahoo option snapshots preserve
`observed_at`, but `quote_at` is null because Yahoo does not prove quote-event time. Historical option
training/backtests require licensed historical contracts and Bid/Ask. ThetaData v3 is implemented
behind validated fixtures, but real coverage requires configured subscription credentials.

No CAPTCHA, challenge solving, credential spoofing, identity rotation or license bypass is implemented.

## Verification

```bash
npm run typecheck
npm run build
npm run test:quant-alpha
npm run test:quant-alpha-mcp
npm run test:quant-alpha-e2e
npm run test:quant-mcp
npm run test:quant-regression
npm run test:skills
npm run test:skills:quant
npm run test:plugin
npm run verify:pack
```

See [the current research status](QUANT_ALPHA_STATUS.md) for exact evidence and limitations.
