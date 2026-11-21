# Option-level replay method (strategy review protocol section 5)

**Dated 2026-11-21. Frozen before the first Validate run (access log #31).** Used to score a variant
against v1 in dollars per position, because R is a proxy and the money is made in options. It is a
model of the market from stored one-minute option **trade** bars; there are no stored historical
quotes. Its job is to check the *sign* of the R result, not to estimate profit.

## Rules (identical for v1 and the variant)

1. **Contract.** Same-day expiry, the strike nearest the reference entry price (lower strike on a tie),
   call for bullish, put for bearish (D-57, config/risk/risk-v1.yaml).
2. **Entry.** The order is sent one second after the signal bar closes (`reference_entry_time`). The fill
   is the **open** of the first one-minute option bar that starts at or after that time, within 3
   minutes, **plus a half-spread haircut** `h`. No bar in that window: the position is **unpriced**.
3. **Haircut `h` = 0.5% of price on each side** (buy at +h, sell at -h). Measured on live shadow quotes
   (access log #30): the median half-spread of 0DTE quotes with mid between $0.50 and $8 was 0.32% of
   mid, the 75th percentile 0.53%; 0.5% is deliberately toward the wide side.
4. **Premium stop.** After the entry bar, in each bar until the reference exit, if the bar's low times
   (1 - h) is at or below 80% of the entry fill (risk-v1 `premium_stop.loss_pct` 0.20), the position
   exits at the lower of that bar's open times (1 - h) and 80% of the entry fill.
5. **Reference exit.** If the stop was not hit, the exit is the open of the first option bar at or after
   `reference_exit_time` (within 3 minutes) times (1 - h); none: **unpriced**. A position still open
   at the last bar of the session exits at that bar's close times (1 - h).
6. **Size.** quantity = floor(0.20 x $10,000 / (entry fill x 100)), then capped to the daily-loss
   headroom: floor((500 - realized loss so far today) / (entry fill x 100 x 0.25)) (D-92). Below 1: the
   signal is skipped. Equity basis is $10,000 on every day (no compounding).
7. **Account rules.** One open position at a time (a signal arriving while one is open is skipped); the
   day's entries stop once realized loss reaches $500.
8. **Costs (D10).** 0.0403 per contract per side, plus 0.0000206 of the sale value on the sell.
9. **Result.** Dollars per position after costs. The paired difference is variant minus v1, pooled over
   sessions (total dollars over total positions per arm, session-resampled), as for R.

## Coverage

A session where either arm holds an **unpriced** position is dropped from the paired comparison for
both arms, and the number of sessions and positions dropped is reported. If the retained sessions
hold fewer than 80% of the positions the R test used, the option-level check is **NOT_EVALUABLE**,
which blocks the hypothesis at Validate (the bar says the check must be positive). The check passes
when the point estimate is positive and has the same sign as the R result.

## Limits

Trade bars give no spread or depth; the haircut is a flat model of both. Bars carry the last trade in
a minute, which in the first minutes can be stale. The stop is tested on bar lows, so a fast spike
within a minute can trigger it earlier than a 1-second quote would, and a recovery within the minute
can hide one. None of these favours either arm by construction, but the numbers are not P&L.
