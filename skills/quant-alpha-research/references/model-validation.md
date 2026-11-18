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
- Noise must not be certified merely because a favorable random OOS sample appeared. Require the
  configured validation and OOS evidence together, plus coverage and non-flat spread checks.
- Verify predictions reference the frozen model/fold and never let a future model predict an earlier
  period. Corrupt or missing model artifacts fail closed.
