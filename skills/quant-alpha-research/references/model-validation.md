# Alpha model validation

- First version accepts only a predefined LightGBM Regressor. No arbitrary user training code or
  untrusted Pickle loading. New formal training requires explicit `validation_only`, a research
  protocol hash and Train/Validation windows only.
- Historical `legacy_inline_test` or mode-less models remain readable and may use compatibility
  prediction. They cannot be selected by a new validation-only Freeze and are not formal OOS evidence.
- Ordinary prediction of a validation-only research model is rejected; OOS must use the completed
  Freeze grant path.
- Freeze train, validation and test windows before training. Labels must be matured by the split end.
  Purge overlapping windows and apply the configured embargo.
- Fit preprocessing on train only. Select iterations/hyperparameters on validation. Read test once
  after freezing the model; repeated test-driven selection is leakage.
- Persist feature names/order/types, model and environment hashes, seed, hyperparameters, selected
  iteration, fold/model-set identity, artifact integrity and the exact label version.
- Freeze the model's label version and replay that same version in OOS. A `session-aligned-v2` OOS
  without verifiable calendar evidence fails closed; a legacy v1 model remains v1.
- Report Rank IC per decision time, mean, standard deviation, ICIR, prediction coverage, valid-label
  coverage, matched coverage, valid/invalid/expected IC date counts, quantile returns, top-bottom
  spread, feature gain/split importance, sample counts and metric definitions.
- Preserve null IC dates in coverage denominators. Report sample-level and date-level OOS exclusions
  separately and require expected = valid + excluded.
- A single frozen model can expose importance but cannot establish stability. Stability remains
  NOT_TESTED until independently trained development-window folds are present; OOS may never select
  the stability window. `quant_run_alpha_walk_forward` supplies descriptive Fold stability, but its
  nested-tuned Fold models remain report-only and are never registry/Freeze identities.
- Validation may gate selection. Test/OOS measurements are report-only and must not trigger automatic
  model certification or later hyperparameter selection. `is_alpha_certified` stays false in v1;
  `validation_gate_passed` is a separate pre-test signal.
- Verify predictions use a frozen model/fold, start no earlier than the validation end, and never let a
  future model predict an earlier period. Corrupt or missing model artifacts fail closed.

## Development baseline comparison

- `baseline_comparison` is a closed development-only comparison of short reversal, RSI, Ridge and the
  same Job's LightGBM model. It is not a registry of selectable production models.
- Supply the complete FactorSpec that generated the feature panel. Its compiled hash must equal the
  feature panel `source_version`; composite panels are rejected.
- Reversal is exactly `-pct_change(close, 5)` and RSI is exactly `50 - RSI(close, 5)`. Directions are
  fixed before fitting. A Validation IC never changes the sign.
- Ridge uses Train-only median imputation, Train-only standard scaling and a predeclared bounded alpha.
  Validation performance never searches alpha. A feature missing for every Train sample is an error.
- LightGBM and Ridge use the same matured Train identity. Primary metrics use the same all-candidate
  Validation common cohort; individual coverage remains a diagnostic, not a different primary denominator.
- Pairwise IC uses only dates where both models have finite IC on identical samples. The report is
  descriptive; it does not claim statistical significance or trading validity.
- Quality assessment is allowed only when every feature/label/parent artifact both declares and proves
  via Parquet footer min/max statistics that all required time columns are isolated. The preflight runs
  before label or feature partition reads. Missing/unusable metadata, null time values, unsupported
  lineage kinds, row-count mismatch or any out-of-range row group fail with
  `QUALITY_SCOPE_NOT_ISOLATED` before any `read_partition()` call. `end_at` and `decision_at` are
  strict at the half-open Validation boundary; availability timestamps may equal the cutoff. Never scan
  a dataset range that may include reserved Test data merely to populate a comparison report.
- Candidate records distinguish `candidate_definition_hash` from `fitted_model_hash`. Ridge `model_id`
  binds the Train cohort/label identity and fitted imputer/scaler/Ridge state, so different training
  labels cannot reuse an identity that implies the same fitted result.

## Development walk-forward

- The request carries a half-open `development_window`, never Test/Freeze/OOS fields. Fold plans use
  unique decision sessions; incomplete tail windows are excluded before labels are scored.
- Outer-train eligibility uses actual label `exit_at` and `label_available_at` against
  `outer_validation_start - embargo_days` with strict `<`. Inner fit uses the first inner-tuning
  decision as its availability cutoff.
- LightGBM early stopping must see only the inner tuning set. After an iteration is selected, refit
  on eligible outer Train with `num_boost_round=selected_iteration` and no `valid_sets`; then score
  Outer Validation once.
- Ridge refits median imputation, scaling and the fixed-alpha model separately in every Fold. Factor
  directions and bounded hyperparameters are fixed before any outer result.
- Primary comparison uses the same-date, same-instrument four-candidate common cohort across
  non-overlapping OOF samples. Date-level RankIC is averaged by date, not by sample count or unconditional
  Fold means. Missing/invalid dates remain in coverage denominators.
- Moving-block bootstrap resamples ordered decision sessions while preserving all four candidate values.
  Paired p-values use a centered null. Holm retains the fixed family of four even when a test is
  unestimable. Report scope is `CURRENT_PREREGISTERED_FAMILY_ONLY` and global trial history is incomplete.
- Read `summary`, every page of `walk_forward`, `diagnostics`, `trials` and `reproducibility`.
  `WF_COMPLETED` or `WF_EXPLORATORY` does not mean frozen OOS, Alpha confirmation or trading validity.
