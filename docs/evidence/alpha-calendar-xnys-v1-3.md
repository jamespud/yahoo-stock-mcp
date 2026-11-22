# Alpha v1.3 historical calendar reconciliation

Scope: `[1990-01-01, 2012-01-01)` and its `[2002-01-01, 2012-01-01)` subset. This is a reconciliation of daily Session labels and outer bounds, not an intraday trading-halt calendar, a point-in-time announcement record or a price attestation.

Primary records (the NYSE document is a mirrored exchange historical record):

- **NYSE Historical Closings**, revision through January 2011, pp. 15-20, [PDF](https://s3.amazonaws.com/armstrongeconomics-wp/2013/07/NYSE-Closings.pdf), PDF SHA-256 `cbebd12230d3600a92922a97791d4601c6d6326e1b2b0d9348d1d6b1f22f6f77`.
- **SEC Division of Market Regulation**, [Trading Analysis of October 27 and 28, 1997](https://www.sec.gov/news/studies/tradrep.htm) and [Staff Legal Bulletin No. 8](https://www.sec.gov/interps/legal/slbmr8.htm), on the market-wide circuit breakers.
- **ISE Market Control Group**, [Market Information Circular 2002-03](https://www.nasdaqtrader.com/Content/NewsAlerts/ISEMics/MIC-2002-03%24Market_Opening_Time_-_September_11_2002%2420020909.pdf), dated September 9, 2002. This confirms a delayed opening announcement for both NYSE and Nasdaq, but does not establish Nasdaq's actual opening minute.

## XNYS differences

Regular NYSE hours in the comparison are 09:30-16:00 New York time. Times below are New York local time. The machine-readable comparison retains the original UTC values. All listed dates remain Session labels under either interpretation.

| Date | Evidence | Daily outer-bound ruling |
| --- | --- | --- |
| 1990-12-27 | Transformer explosion delayed opening to 11:00 (NYSE p. 15). | Set open to 11:00; neither library is correct. |
| 1991-01-17 | Opening explicitly delayed to 09:31 for a tribute (NYSE p. 15). | Set open to 09:31. |
| 1991-02-25 | Opening explicitly delayed to 09:31 for a tribute (NYSE p. 15). | Set open to 09:31. |
| 1994-02-11 | Snowstorm closed the floor at 14:30 (NYSE p. 16). | Set close to 14:30. |
| 1995-12-18 | Systems trouble delayed opening to 10:30 (NYSE p. 16). | Set open to 10:30. |
| 1996-01-08 | Snow delayed opening to 11:00; floor closed at 14:00 (NYSE p. 16). | Set both bounds. |
| 1997-10-27 | First circuit breaker briefly interrupted trading; second stopped it for the day at 15:30 (NYSE p. 16; SEC analysis and bulletin). | Set close to 15:30. The outer bound does not mark the first interruption as tradable. The SEC and historical table differ by one minute on first halt/resumption; no intraday override is inferred. |
| 1999-12-31 | Actual 13:00 New Year's Eve close (NYSE p. 17). | Keep the runtime's 13:00 close. Oracle misses it. |
| 2001-09-17 | NYSE reports trading reopened at 09:33 after the September 11-14 closure and opening tribute (NYSE p. 18). | Set open to 09:33; retain September 11-14 as non-sessions. |
| 2001-10-08 | Silence at 09:30-09:31 (NYSE p. 18). | Exact revised open not established by the entry; leave the difference unresolved. |
| 2002-09-11 | NYSE opening delayed to 12:00 (NYSE p. 18). | Set XNYS open to 12:00; do not apply to Nasdaq. |
| 2003-03-20 | Silence at 09:30-09:32 (NYSE p. 18). | Exact revised open not established by the entry; leave unresolved. |
| 2004-06-07 | Silence at 09:30-09:32 (NYSE p. 19). | Exact revised open not established by the entry; leave unresolved. June 11 was a separate full closure. |
| 2005-06-01 | A communications failure halted trading at 15:56 without resumption; crossing sessions were cancelled (NYSE p. 19). | Set daily effective close to 15:56. This is not proof of a closing auction, Yahoo bar availability or fillability. |
| 2006-12-27 | Silence at 09:30-09:32 (NYSE p. 19). | Exact revised open not established by the entry; leave unresolved. January 2, 2007 was a separate full closure. |

Shared or omitted cases: November 23, 1990, October 22, 1991, June 8, 2001 and September 11, 2003 include intraday suspensions; no change to daily endpoints is supported by those entries. The July 2, 2009 entry records handling of orders affected by a systems issue until 16:15; it does not establish an open-to-all core session through 16:15. No regular-session extension is applied.

## Venue boundary

Nasdaq and NYSE are different venues. The Nasdaq alias of the pinned provider is not independent point-in-time calendar evidence. The NYSE floor-specific snow and systems events cannot be copied to Nasdaq. The following records establish venue differences without certifying the rest of the Nasdaq schedule:

| Date | Nasdaq-specific evidence | Scope |
| --- | --- | --- |
| 1997-10-27 | The SEC analysis supports a market-wide early end. | Does not establish the rest of Nasdaq's historical calendar. |
| 2001-06-29 | [Nasdaq's same-day announcement](https://mondovisione.com/media-and-resources/news/nasdaq-experiences-network-interruption-then-extends-trading-to-5-pm-eastern-2011124/) records a network interruption, extension to 17:00 ET and cancellation of the separate after-hours session. A [subsequent SEC-filed prospectus](https://www.sec.gov/Archives/edgar/data/806085/000095013604003265/file001.htm) notes the 17:00 deadline. | Nasdaq-specific exception outside the NYSE comparison, not a rule applied by this release. |
| 2002-09-11 | The ISE circular confirms a delay and an anticipated 11:00 ET start. A [Chicago Stock Exchange announcement](https://mondovisione.com/media-and-resources/news/chicago-stock-exchange-announces-september-11th-market-opening-2011124/) corroborates the scheduled Nasdaq time. | NYSE's final 12:00 time cannot be used for Nasdaq. The notices are not an actual Nasdaq opening record. |
| 2005-06-01 | [ISE circular 2005-14](https://beta.nasdaqtrader.com/Content/NewsAlerts/ISEMics/MIC-2005-14%24NYSE_Amex_Halt%2420050602.pdf) confirms that the NYSE and Amex halted at different times and that Nasdaq-related option classes were unaffected. | The NYSE 15:56 boundary is not a Nasdaq correction. |

The four silence-only XNYS cases and the full venue-specific Nasdaq calendar remain outside acceptance. The NYSE policy alone does not change the engineering or trading-validity gates.

## Archive follow-up

- The [NYSE Daily TAQ specification](https://www.nyse.com/publicdocs/nyse/data/Daily_TAQ_Client_Spec_v4.3.pdf) documents historical trades and quotes from 1993, covering all four unresolved XNYS dates. Its consolidated administrative-message files are listed only from February 28, 2011. No historical files were obtained. Venue-filtered, eligible opening prints could corroborate an exchange status record, but the first print for an individual listing is not proof of the venue-wide opening instant.
- The [Nasdaq Market System Status archive](https://www.nasdaqtrader.com/trader.aspx?id=marketsystemstatussearch) advertises coverage from December 29, 2000. It is a candidate source for exceptions within the 2002-2012 window, not an independent Session calendar by itself. The [public trading calendar](https://www.nasdaqtrader.com/Trader.aspx?id=Calendar) does not supply annual calendars for the target windows. The longer window also requires a source for dates before December 29, 2000.

No opening boundary was changed on the basis of these sources. Prefer contemporaneous exchange status records or a venue-specific calendar with documented historical coverage before changing the runtime policy.
