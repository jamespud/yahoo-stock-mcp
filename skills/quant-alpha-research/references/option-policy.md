# Alpha option policy

- Contract identity includes underlying, type, strike, expiration, exercise/settlement style,
  multiplier, currency, deliverable and adjustment version. Provider symbols are not universal IDs.
- Keep `observed_at`, nullable `quote_at` and `available_at` distinct. Yahoo cannot prove quote-event
  time, so its snapshot rows use `quote_at = null` and are current-observation evidence only.
- Option factor cross-sections use a bounded observation-time tolerance for provider timestamps that
  differ by seconds. Record the tolerance used and do not combine stale contracts outside that grid.
- Current Yahoo chains may support current ATM IV, skew, term structure, OI/volume ratios and Greeks.
  They cannot admit historical option-return training or quote-driven backtests.
- Historical option research requires licensed historical contracts and valid Bid/Ask, plus explicit
  IV/rate/dividend/day-count/model inputs. IV and return labels are separate.
- Long-only execution: BUY at next valid Ask, SELL at next valid Bid. No Mid/Last/intrinsic close,
  automatic assignment, naked short, margin or broker-grade lifecycle.
- `close_before_expiry` requires a valid exit quote. Missing exits remain unresolved and visible.
  The v1 option engine tracks cash, premiums, commissions and unresolved cost basis, but is not a full
  broker portfolio, margin, assignment or corporate-action model.
- Greeks/IV are diagnostics when a quote, model or solver fails; never fabricate a number.
