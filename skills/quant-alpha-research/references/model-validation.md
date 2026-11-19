# Alpha model validation

- First version accepts only a predefined LightGBM Regressor. No arbitrary user training code or
  untrusted Pickle loading.
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
  the stability window.
- Validation may gate selection. Test/OOS measurements are report-only and must not trigger automatic
  model certification or later hyperparameter selection. `is_alpha_certified` stays false in v1;
  `validation_gate_passed` is a separate pre-test signal.
- Verify predictions use a frozen model/fold, start no earlier than the validation end, and never let a
  future model predict an earlier period. Corrupt or missing model artifacts fail closed.
