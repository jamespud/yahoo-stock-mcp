# Execution checklist

Discover capabilities, validate definition, register strategy, compile binding, acquire immutable
snapshot, inspect PIT quality, submit job, poll terminal state, read and reconcile results.
These are distinct steps; compilation does not execute a strategy or acquire history.

Choose explicit start/end timestamps including offsets. Supply enough prehistory for source and
decision-grid warmup. Separate modelled bar-end-plus-delay availability from vendor-recorded
publication. Default local vendor prices are unverified; execution requires verified raw prices.
Do not invent missing dividends/splits or claim that no supplied actions means no historical actions.

Execution config uses initial_cash, quantity, commission_bps, slippage_bps,
fill_policy (`next_bar_open`), currency and optional max_positions, max_position_weight,
max_gross_exposure, max_order_notional and volume_participation. Volume participation is based on
previously published volume, not future bar volume. Current broker models are long-only and
same-currency. Unsupported requests should fail visibly. Ask for the intended research assumption
when a material parameter is unspecified; disclose any agreed defaults.

`signal_backend: pit` supports explicit alignment; vector is limited to the capability-declared
one-symbol/timeframe monotone domain. Both use the event broker. Unsupported vector plans must not
be silently switched. A Python plan additionally pins operator image and isolation policy; require
live sandbox availability. A Python portfolio uses one artifact and one isolated lifecycle actor.

For portfolio mode provide portfolio = {symbol: plan_id, ...}, data_snapshot_id, start/end and
config; omit single-asset plan_id/symbol/timeframe. Every binding shares cash and risk limits.
Retry submission with the same idempotency_key and identical request. A key is not a parameter-sweep
identifier. Cancellation cannot overwrite a completed result.
