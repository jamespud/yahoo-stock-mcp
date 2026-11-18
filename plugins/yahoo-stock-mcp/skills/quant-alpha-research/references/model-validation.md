# Alpha model validation

- First version accepts only a predefined LightGBM Regressor. No arbitrary user training code or
  untrusted Pickle loading.
- Freeze train, validation and test windows before training. Labels must be matured by the split end.
  Purge overlapping windows and apply the configured embargo.
- Fit preprocessing on train only. Select iterations/hyperparameters on validation. Read test once
  after freezing the model; repeated test-driven selection is leakage.
- Persist feature names/order/types, model and environment hashes, seed, hyperparameters, selected
  iteration, fold/model-set identity and artifact integrity.
- Report Rank IC per decision time, mean, standard deviation, ICIR, prediction coverage, quantile
  returns, top-bottom spread, feature importance, sample counts and metric definitions.
- Validation may gate selection. Test/OOS measurements are report-only and must not trigger automatic
  model certification or later hyperparameter selection. `is_alpha_certified` stays false in v1;
  `validation_gate_passed` is a separate pre-test signal.
- Verify predictions use a frozen model/fold, start no earlier than the validation end, and never let a
  future model predict an earlier period. Corrupt or missing model artifacts fail closed.
