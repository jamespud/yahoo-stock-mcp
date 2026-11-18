# Alpha factor policy

- FactorSpec v1 is separate from the strategy entry/exit DSL.
- Node order is not execution order; compilation uses the dependency DAG and rejects duplicates,
  missing references, cycles, wrong arity/params, invalid windows and excessive complexity.
- Time-series operators run per instrument. Cross-sectional operators run only over assets with a
  row at the same decision time; later-listed assets do not enter earlier ranks.
- Missing values remain missing. No implicit zero fill, forward fill or cross-asset substitution.
- `feature_available_at`, instrument identity, factor version, eligibility and quality flags are part
  of the feature panel. Feature-set identity binds dataset, FactorSpec, operator versions and files.
- Future raw-data mutations must not alter stored past factor rows. Recompute only by creating a new
  immutable feature set.
