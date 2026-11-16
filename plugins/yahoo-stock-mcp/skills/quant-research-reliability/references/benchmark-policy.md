# Benchmark policy

Select benchmarks before reviewing the test outcome. State why each is relevant. At minimum consider
cash and buy-and-hold of the execution instrument; a sector/market proxy requires explicit observed
data and a defensible historical universe. Cash must use the stated risk-free assumption; absent a
modeled yield, call it zero-yield cash, not a market risk-free return.

Use identical timezone-aware start/end, immutable snapshot coverage, currency, initial capital,
price basis, corporate-action treatment and cost conventions. Compare funded exposure fairly:
a one-share strategy and a fully invested benchmark have different cash drag. The current model
uses quantity and next_bar_open; an always-true entry strategy is not automatically a fully invested
fractional buy-and-hold benchmark. Validate its holdings/fills before using that label.

There is no dedicated benchmark MCP tool. If authorized, build a valid DSL baseline (constant true
entry, false exit), validate, register, compile and submit quant_run_backtest against the same
snapshot/window/config. Check actual fills and leftover cash. If a requested total-return index,
delisted assets or currency conversion are unavailable, report the comparison unavailable; never
substitute price-only performance or a remembered index return.

Report absolute strategy and benchmark results plus differences with matching units. Compare net
returns after commission/slippage, drawdown, exposure, turnover and sample duration. Stress costs
with declared multipliers; zero defaults are not evidence of realistic costs. No significance,
alpha, p-value or statistical confidence claim is justified without the required aligned sample
and a stated, actually applied estimation method. A positive excess return is historical evidence,
not proof of causation or future outperformance.
