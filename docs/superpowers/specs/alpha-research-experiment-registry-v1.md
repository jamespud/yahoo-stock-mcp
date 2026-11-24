# Alpha Research Experiment Registry v1 Design

## Status and safety boundary

- Branch: `codex/alpha-research-experiment-registry-v1`
- Baseline: `3643d645699da9d6842ba2b200f0a821a561f4af`
- This design is read-only with respect to all historical Dataset, Parquet, Model, Freeze and OOS artifacts.
- No real Alpha v1.3 Test/OOS rows are read, regenerated or evaluated.
- Verification uses synthetic datasets, Jobs and artifacts only.

PR5 is evidence governance, not Alpha certification. Registry integrity and model performance are
independent outcomes. No report status in this release can become `ALPHA_CONFIRMED`,
`PRODUCTION_READY` or `TRADING_VALIDATED`.

## Current-state review

### Job lifecycle

`JobStore.submit_alpha()` validates a discriminated Pydantic request, checks idempotency, inserts a
queued Job and writes the optional global `job_requests` row in one SQLite transaction. `claim()` owns
a durable lease and increments `attempts`; `begin_publish()` moves a fenced owner into `publishing`;
`complete_publish()` is the CAS that makes result data visible as `completed`. `finish()` is the
non-publication terminal path. `cancel()` is a terminal CAS for queued, running or publishing work.

The existing `alpha_research_audit` table is an OOS/Freeze/rejection audit. It is not a trial ledger:
it has no campaign event sequence, hash chain, trial identity, request binding, budget reservation or
candidate snapshot.

### Research identity today

`research_protocol_hash` is currently a caller-supplied hash. `candidate_ledger_hash` is stored in a
Freeze identity but is not derived from a system-verified ledger. `job_config_hash` is the canonical
hash of a queued request and has no scientific-candidate meaning. PR3 Baseline Reports and PR4
Walk-forward Reports have independent deterministic report hashes, but neither is a cross-Job trial
accounting system.

PR4's `trial_ledger` is the fixed statistical family inside one walk-forward Job. It must never be
counted as the number of independently trained model candidates.

## Architecture

```text
Campaign (immutable definition, append-only status events)
  └── Protocol revision (server hash, immutable)
       └── Hypothesis definition
            └── Trial registration (canonical request, scientific identity)
                 └── Job
                      ├── Execution attempt events
                      ├── PR3 Baseline evidence / PR4 reconstructed report
                      └── Published result hash
Campaign Ledger Snapshot (watermark, event root, all trials)
  └── Research Selection (predeclared policy)
       └── Existing Freeze / OOS workflow
```

The registry tables live in the same SQLite database and connection as `JobStore`. This permits Job
creation, Trial-Job binding and the submit event to share one transaction. No new queue or worker
system is introduced.

## Public contracts

### Campaign

`ResearchCampaignDefinition` records name, objective, scope, development data scope, related campaign
IDs and a hard/reporting budget policy. `campaign_id` is server-generated from the canonical body.
Status is represented by `CAMPAIGN_REGISTERED`, `CAMPAIGN_SEALED` and `CAMPAIGN_CLOSED` events rather
than mutable history.

### Protocol and hypothesis

`ResearchProtocolDefinition` records revision, parent hash, reason, hypotheses, candidates, allowed Job
kinds, data/window/label/factor/hyperparameter/seed/evaluation/selection/inference/stopping/resource
policies and trial budget. The server computes `protocol_hash`; callers cannot supply it.

A revision must name its immediately preceding parent, increment revision by one, remain in the same
Campaign and include an exposure context (prior result exposure count and latest ledger watermark).
Budget limits cannot be increased through a revision.

Each hypothesis has a deterministic definition hash made from its protocol-bound definition. Its
status changes are append-only events. A walk-forward fold is not a hypothesis. A statistical family
member is not a new model fit.

### Trial

A trial stores:

- Trial ID, definition hash, Campaign/Protocol/Hypothesis IDs and optional parent trial;
- purpose and preregistration evidence grade;
- canonical request and request hash;
- evidence-scope hash;
- the Job binding and lifecycle terminal state.

The scientific trial-definition hash expands Pydantic defaults, excludes idempotency keys, Job IDs,
display-only values and runtime timestamps, rejects NaN/Infinity and canonicalizes unordered sets.
`job_config_hash` remains unchanged for historical Jobs.

Trial identity changes for feature/label data, FactorSpec, model parameters, seed, horizon, windows,
fold configuration, bootstrap inference, primary metric and thresholds. Re-running the same scientific
definition under a new trial preserves the definition hash but consumes a new registration/submission
slot and a new execution attempt.

## Managed and unmanaged admission

The existing call path remains compatible:

- A protocol hash that is not registered is `UNMANAGED_DEVELOPMENT` and retains legacy behavior.
- A registered protocol hash requires a registered trial and an exact request match.
- A caller cannot omit `research_trial_id` to downgrade a registered protocol.
- Supplying a trial with a mismatched protocol, hypothesis, Job kind or canonical request fails closed.
- Arbitrary new hashes cannot inherit a managed Campaign's evidence.
- Direct Python model functions do not receive a trusted registry flag and therefore do not become
  managed evidence merely by calling the registry.

MCP, Pydantic request binding, `JobStore.submit_alpha()`, Worker execution and the direct Python
boundary are covered by tests.

## Ledger schema and event chain

New tables:

```text
alpha_research_campaigns
alpha_research_protocols
alpha_research_hypotheses
alpha_research_trials
alpha_research_trial_jobs
alpha_research_events
alpha_research_ledger_snapshots
alpha_research_selections
alpha_research_exposures
```

Primary identities are hashes or server-minted IDs. Trial-to-Job binding is unique for a managed Job.
Event rows are append-only and ordered by `(campaign_id, event_sequence)`. Every event includes the
previous hash and a server-computed event hash. Integrity verification checks sequence continuity,
Campaign identity, previous-hash links and event-hash recomputation.

The chain detects ordinary accidental or unauthorized edits through business interfaces. It does not
provide an external trusted timestamp and cannot resist an attacker who can rewrite the SQLite file and
all hashes.

Lifecycle events are automatic:

```text
TRIAL_REGISTERED, TRIAL_SUBMITTED, TRIAL_EXECUTION_STARTED, TRIAL_RETRY_STARTED,
TRIAL_PUBLISHING, TRIAL_COMPLETED, TRIAL_FAILED, TRIAL_CANCELLED,
TRIAL_WORKER_LOST, TRIAL_REJECTED, TRIAL_RESULT_ACCESSED
```

Campaign/selection events are `CAMPAIGN_REGISTERED`, `PROTOCOL_REGISTERED`, `PROTOCOL_REVISED`,
`LEDGER_SEALED` and `SELECTION_REGISTERED`.

## Budgets

`max_registered_trials`, `max_submitted_trials`, `max_distinct_candidate_definitions` and
`max_evaluation_jobs` are hard limits enforced under `BEGIN IMMEDIATE`. Protocol revisions and
execution attempts are also hard-bounded. `max_execution_attempts` is reserved when a Job is claimed.

Idempotent replay does not consume a slot. Failed, cancelled and rejected submitted experiments do not
release the scientific trial slot. Model-fit and CPU-time counters are reporting-only until they can be
derived reliably from trusted execution telemetry; no report claims they were hard-enforced.

Trial registration and managed Job submission each use one immediate transaction. An idempotency
conflict is audited as a rejected submission without creating a Job or Trial.

## Exposure and adaptive research

At publication, the completed evaluation is considered generated. Reading an accessible Job result
through `quant_get_result`, the result MCP Resource, or the new research report records a
`TRIAL_RESULT_ACCESSED` event. The exposure table stores Trial, Job, result hash, section, first/last
access and request count. Multiple pages of one result increase request count but not distinct
evaluation-result exposure count.

A subsequent trial is marked `REGISTERED_AFTER_PRIOR_EXPOSURE`; an explicitly post-hoc trial is marked
`POST_HOC_EXPLORATORY`. The report always exposes:

```text
global_trial_history_complete = false
```

and separate completeness dimensions for registry scope, managed campaign history, Job lifecycle,
historical import, external history, exposure tracking and ledger integrity. `exposure_tracking_complete`
is false until every public result path is instrumented.

## Data reuse

The report compares Campaign development scopes and emits:

```text
EXACT_EVIDENCE_REUSE
POTENTIAL_OVERLAPPING_EVIDENCE
DISTINCT_DECLARED_SCOPE
INDEPENDENCE_UNVERIFIED
```

Exact reuse covers Stock Dataset ID, Feature Dataset ID, parent identity, source and overlapping time,
Universe and label definition. Different physical Dataset IDs with the same source and overlapping
windows produce a potential-overlap warning. A new Campaign name never creates new evidence.

## Evidence binding

### PR3 Baseline

The report reads `training_metrics[0].baseline_comparison`, validates
`BaselineComparisonReport.report_hash`, binds Job/config/result hashes, candidate/model/cohort/data
quality identities and preserves invalid/insufficient statuses. Caller-supplied RankIC is never used.

### PR4 Walk-forward

The report reads every page of `walk_forward`, the full `reproducibility.report_reconstruction` object,
the Job result hash and artifact catalog, merges all fold rows and validates `WalkForwardReport`.
Missing pages, altered fold hashes, altered report hashes and inconsistent data identities fail closed.
Fold hashes never become formal `model_id` values.

## Snapshot and selection

A snapshot is generated only after ledger integrity verification and Job reconciliation. It includes
all hypotheses, trials, terminal states, evidence references, exclusions, the watermark, event root,
selection-policy hash and scope completeness. A registered but unsubmitted trial, or any queued/running/
publishing Job, blocks sealing.

Selection is computed from the Protocol's predeclared rules. The default valid-candidate rules require:

- a completed training Job with valid evidence;
- a Model Registry model identity distinct from a fold model hash;
- finite primary metric at or above the declared minimum;
- coverage and valid-sample thresholds from the policy;
- a permitted research status.

The system records all considered, eligible and excluded candidates and permits
`NO_ELIGIBLE_CANDIDATE`. No manual override can create a valid managed Freeze selection.

## Managed Freeze integration

For a registered Protocol, a Freeze submission must provide an existing Snapshot with the same Campaign
and Protocol, a valid Selection Record and a selected model that exists in the original Model Registry.
All existing PR1 checks remain in force. The Registry adds evidence checks; it never relaxes Test timing,
model identity, Dataset identity or Freeze authorization. Unregistered protocols continue through the
legacy path and cannot claim Registry verification.

No PR5 path reads Test rows or issues an OOS grant. `quant_evaluate_alpha_oos` remains the only OOS Job
entry point and is never invoked automatically.

## Research report

`alpha-research-evidence-report-v1` reports Campaign/Protocol identities, watermark, chain integrity,
scope completeness, six primary trial counts plus failed/cancelled/rejected/model-fit/statistical counts,
resource use, hypothesis history, data quality/reuse, exposure, PR3/PR4 evidence, statistical scope,
selection, unresolved risks and a deterministic report hash. Report generation time and random request
IDs are excluded from identity. The report never includes protected OOS rows.

`multiple_testing_scope` preserves PR4's `CURRENT_PREREGISTERED_FAMILY_ONLY`. Campaign correction is
reported as `CAMPAIGN_CORRECTION_NOT_ESTIMABLE` or `CAMPAIGN_FAMILY_NOT_PREREGISTERED` unless a complete
pre-registered family supplies compatible raw valid p-values. Trial count is never multiplied into a
p-value.

## Pagination

Ledger and report list sections use the existing `result_page` cursor format. Cursor identity binds
Campaign, section, watermark, snapshot hash and offset. Reading a snapshot during concurrent appends
cannot move its rows or hash.

## Rollback and compatibility

Existing Model IDs, Dataset IDs, Freeze IDs, OOS grants, result hashes and object sections are not
rewritten. Stopping new managed submissions leaves historical Registry data readable. A registered
Protocol cannot silently become unmanaged. Historical Jobs may be indexed only from existing persisted
metadata and are marked `HISTORICAL_IMPORT_UNVERIFIED`; missing history remains unknown.

## Deferred statistical methods

Deflated Sharpe Ratio, PBO, White Reality Check, Hansen SPA, sequential alpha spending and online FDR
are not implemented. They require trustworthy trial and selection inputs, which this release establishes
first.

## First-review hardening

### Managed Freeze TOCTOU

A managed Freeze is verified at three boundaries:

1. `JobStore.submit_alpha()` before a Job is created.
2. the trusted Worker entry point before Dataset reads.
3. the final `complete_publish()` CAS transaction.

The final check executes in the same SQLite write transaction as Job completion. A stale Snapshot
cannot become a completed Freeze even if a new Trial is registered after an earlier check.
`authorize_oos()` repeats the managed freshness and Selection checks before `validate_frozen_record()`
or any Test data access, so a stale managed Freeze cannot issue an OOS grant. Unregistered legacy
Freeze/OOS behavior remains unchanged.

Training and Walk-forward Workers also verify Trial–Job binding, Protocol identity, allowed Job kind
and canonical request identity before execution. The final publication transaction repeats the managed
request verification.

### Protocol constraint enforcement

Training and Walk-forward requests are normalized into one scientific shape containing canonical label,
window, FactorSpec identity, candidate configuration, evaluation and stopping fields. Training's
top-level label fields therefore cannot bypass `label_specifications`.

The following constraints are enforced:

- `candidate_definitions` and Hypothesis `candidate_constraints` as recursive subsets of the normalized
  candidate;
- Hypothesis `factor_spec_hash` and Protocol `factor_specifications`;
- `evaluation_policy` fields for primary metric, thresholds, walk-forward mode and required walk-forward
  evidence;
- `stopping_policy.max_seconds`, sample/fold/bootstrap limits;
- seed, hyperparameter and walk-forward inference policies.

Unknown evaluation or stopping keys are rejected when the Protocol is registered. A constraint is never
reported as enforced unless the corresponding runtime check exists.

### Budgets and scheduling

Campaign and Protocol budgets are both evaluated inside the registration/submission transaction.
Campaign counters remain cumulative across Protocol revisions. Execution-attempt exhaustion is checked
before claiming a queued or expired Job; the blocked Job is atomically marked failed with
`RESEARCH_TRIAL_BUDGET_EXCEEDED`, then scheduling continues with the next eligible Campaign Job.

### Selection evidence

The sealed Snapshot preserves every Campaign Trial. The current Protocol's Eligible Selection Pool only
contains Trials with an exact compatible Protocol hash; other-Protocol Trials remain visible with
`PROTOCOL_NOT_COMPATIBLE_WITH_SELECTION`.

Label coverage, model-execution coverage and end-to-end coverage are separate evidence fields. Training
uses Baseline cohort counts for label and end-to-end coverage and model Validation prediction coverage
only for model-execution coverage. Missing evidence excludes the candidate. A Protocol requiring
walk-forward evidence excludes training-only candidates with `WALK_FORWARD_EVIDENCE_REQUIRED`.

### Rejected submissions and immutable views

A rejected submission appends `TRIAL_REJECTED` but changes Trial state only from `REGISTERED`. It never
overwrites `SUBMITTED`, `RUNNING`, `COMPLETED`, `FAILED` or `CANCELLED` history.

Ledger and report cursors bind a fixed watermark or Snapshot. New Trials, later Exposure events and new
Campaigns do not change an existing Snapshot report. Live reports (no Snapshot/watermark), explicitly
fixed-watermark reports and Snapshot reports are distinct views; only the Snapshot/fixed-watermark views
make immutability claims.

## Second-review hardening

### Atomic final Freeze publication

`complete_publish()` now opens an explicit `BEGIN IMMEDIATE` when the connection is not already in a
transaction. Managed request/freeze verification and the final completed-state CAS UPDATE execute in
that same write transaction. A separate SQLite connection cannot register a new Trial between the final
freshness check and CAS completion. Existing lease ownership and PR1 OOS rules are unchanged.

### Walk-forward candidate binding

A valid Walk-forward Job elsewhere in the Protocol is not evidence for every Training Candidate. This
release cannot yet prove a candidate-to-Walk-forward scientific-configuration binding across separate
Trials. When `requires_walk_forward=true`, Training Candidates therefore fail closed with
`WALK_FORWARD_CANDIDATE_BINDING_NOT_SUPPORTED`. Unrelated Walk-forward evidence, including different
Horizon, FactorSpec or model configuration, cannot unlock selection. No Final Refit system was added.

### Historical watermark views

Historical Trial existence uses the actual `TRIAL_REGISTERED` event sequence. Submission, completion,
result evidence, Exposure summaries and Hypothesis/Protocol registration are also bounded by the target
Watermark:

- a Trial registered after W1 is absent from the W1 ledger view;
- a Trial running at W1 has no result evidence even if it completes at W2;
- Exposure counts are rebuilt from `TRIAL_RESULT_ACCESSED` events at or before W1;
- Protocol revisions and Hypotheses registered after W1 do not enter W1 history;
- fixed-watermark report pagination encodes Campaign, Watermark, Protocol/Snapshot identity and Report
  Hash in its cursor and rebuilds the same immutable view.

An explicit Watermark does not retroactively adopt a Snapshot created later at the same numeric
watermark. Snapshot reports require an explicit Snapshot hash.
