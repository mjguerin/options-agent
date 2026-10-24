# Research access log

Every inspection of market data made while building the data pipeline and writing the specs.
"Behaviour" means price movement, returns or strategy-relevant patterns. "Integrity" means counts,
coverage, ordering and provenance. No strategy has been run, and no P&L, win rate or return of any
rule has been computed.

| # | When (2026) | Data | What was looked at | Kind | Printed market values |
|---|---|---|---|---|---|
| 1 | Oct 23 | Latest SPY quote, latest SPY option quote | `tournament data-check`: SIP and OPRA entitlement | Integrity | SPY 760.46/760.51; SPY261023C00500000 260.37/260.77 |
| 2 | Oct 23 | 2026-10-22 SPY bars, 30 min of trades, 5 min of quotes; 2024-02-01 bars | API capability and volume probe | Integrity | Row counts only |
| 3 | Oct 23 | Raw response shapes: one 2026-10-22 trade and bar; one 2024-02-01 option bar | Field names and types | Integrity | One trade (766.37), one bar (O 766.37 / H 766.71 / L 765.95 / C 766.59), one option bar (O 1.28 / C 1.54) |
| 4 | Oct 23 | Option contracts expiring 2024-02-01 to 02-08 | Expired-contract listing works | Integrity | Counts and one contract record |
| 5 | Oct 23 | 2024-02-02 full day | Per-step download timing | Integrity | Row counts |
| 6 | Oct 23 | 24 sessions sampled 2024-02-01 to 2026-10-12 | At-the-money 0DTE call: bar count and 10-minute trade count (finding the 2024-02-02 bar gap) | Integrity | Counts only; the session's first-minute SPY open was used to pick the strike but not printed |
| 7 | Oct 23 | All 684 sessions, 2024-02-01 to 2026-10-22 | Completeness, row counts (min, median, max of SPY trades, contracts and option bars per day), timestamp order, provenance | Integrity | Activity counts only |
| 8 | Oct 23 | 2024-02-01 to 2024-04-30 (62 sessions) | Descriptive scales: ATR, ranges, opening-range widths, VWAP distance, VWAP crosses ([research/scale_stats.py](../research/scale_stats.py)) | **Behaviour** | The table in README.md |
| 9 | Oct 24 | 70 sessions sampled across 2024-02-01 to 2026-10-22 | Sale-condition codes, combinations and tapes ([research/eligibility/condition_inventory.py](../research/eligibility/condition_inventory.py)) | Integrity | Counts only |
| 10 | Oct 24 | 11 sessions around 2025-11-03, plus 3 others | Odd-lot flag share by trade-size band (round-lot change) ([roundlot_check.py](../research/eligibility/roundlot_check.py)) | Integrity | Shares and counts only |
| 11 | Oct 24 | 20 sessions: 15 in 2024, 5 after 2025-11-03 | Our minute bars against Alpaca's, field by field; mismatch causes ([bar_variants.py](../research/eligibility/bar_variants.py), [bar_final.py](../research/eligibility/bar_final.py)) | Integrity (bar construction) | Match rates. During diagnosis, 9 opening prices and one low from both sources were printed to identify the 09:30 Form T cause |
| 12 | Oct 24 | Full store (`O`/`N` coverage); 2025-10-27 to 11-07 (round lot); the 20 sessions of #11 | Effect of the state fix; `O`/`N` trades, sessions and slots; shares of trades, volume and notional by size band; the provider-open cases with their first prints ([evidence_v2.py](../research/eligibility/evidence_v2.py)) | Integrity (bar construction, coverage) | Counts, shares, and the first six prints (price, size, conditions) of 9 opening minutes |

**Implication for the data partitions:** behaviour has been inspected only for Feb–Apr 2024
(entry 8). Entries 9–11 compared bar constructions and condition codes, not market behaviour. Every later period has been touched only for integrity checks, so it's still clean
for a held-out role, subject to the owner's own record of any charts or reports viewed outside
this project.
