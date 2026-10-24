# Trade eligibility (D-04 deliverable)

Status: **DRAFT 2. Conditionally approved in principle by the owner (2026-10-24); not frozen.**
`features-v1` stays blocked until the freeze checklist at the end is complete. Scope: SPY (tape B,
CTS).

| Part | Where |
|---|---|
| A. Source register | [config/trade_eligibility/source_register.yaml](../config/trade_eligibility/source_register.yaml) |
| B. Conditional matrix | [cts_sale_conditions.csv](../config/trade_eligibility/cts_sale_conditions.csv), [notes.yaml](../config/trade_eligibility/notes.yaml) |
| C. Minute-bar adapter | [minute_bar_adapter.yaml](../config/trade_eligibility/minute_bar_adapter.yaml) |
| D. Combination and lifecycle | notes.yaml (rules, state, ordering, constraints); SD-01 and SD-03 (late trades, corrections) |
| E. Evidence | The evidence section below; scripts in [research/eligibility/](../research/eligibility/) |
| Config validation | [tests/test_trade_eligibility_config.py](../tests/test_trade_eligibility_config.py): parsing, rule values, notes, interval coverage, scoped assumptions |

## Owner decisions (2026-10-24)

| ID | Decision | Conditions, and how this draft meets them |
|---|---|---|
| E-1 Open/close | Accepted in principle | SIP state and application inclusion are now separate stages; the ordering is declared (below) |
| E-2 High/low = open/close set | Accepted as application policy | They're described as last-eligible execution prices, **not** SIP consolidated highs and lows. `4`, `P` and `Z` contribute when their `NOTE_2` resolves true, so tests must cover those cases |
| E-3 Volume | Accepted in principle | Counting and correction/cancel accounting defined in adapter `upd_vol.counting` |
| E-4 VWAP | **(a) selected for v1** | Named `last_eligible_execution_trades` (SD-04). `vwap_notional`, `vwap_volume` and `total_volume` are persisted separately; session VWAP is never rebuilt from bar volume |
| E-5 History gaps | **Not approved as certification** | Now **development-only assumptions** (`verification_status: assumed`, `allowed_use: [development]`), with a held-out disclosure rule. The source gaps stay open |
| E-6 Pre-market | Accepted in principle | It's an application adaptation in a separate state namespace that never writes SIP state. The download is still to do |
| E-7 `M`, `Q`, `9` | Accepted as application exclusion | Excluded from bars in stage 2 only; they still take part in stage-1 SIP state. The CTS lifecycle check is OPEN-SRC-4 |

## A. Sources

- **CTS Pillar Output Specification 2.11b** (2026-01-29) is the rule source.
- **CTS 2.12b**, the ETH edition, is registered; its sale-condition table text matches 2.11b.
- **UTP 4.1** is registered, not transcribed (no tape C symbol in scope).
- **Alpaca's pages** are provider references for diagnostics only.

**The owner couldn't fetch the CTS PDFs** (nyse.com appears to block automated retrieval). The exact
bytes used are archived on the mini, and anyone with access can verify them:

```bash
ssh mini 'cd ~/options-agent/data/sources && shasum -a 256 *.pdf'
```

These must equal the SHA-256 values in the register. The transcription can then be checked
against CTS 2.11b pp. 82–85.

**Dated events inside the history.** Each carries a `date_status`, because document dates and
go-live dates differ:

| Date | Event | Status |
|---|---|---|
| 2024-03-28 | `O` made always last-eligible | Document date; go-live unverified |
| 2024-05-28 | `N` reserved (T+1 settlement start) | Stated effective date for T+1; the `N` change date is unverified |
| 2025-11-03 | SPY round lot 40 | Stated, and observed in our data |

## B. Matrix

- **28 rows.** Effective intervals are `effective_from` inclusive and `effective_to` exclusive.
  The validator checks that every code is covered contiguously from 2024-02-01, with no gaps or
  overlaps.
- **Consolidated columns** are evaluated, with conditional notes resolved statefully.
- **Participant columns** are transcribed but **not evaluated**: they'd need per-participant
  state, which isn't implemented, and no application field uses them.
- **State transitions use resolved eligibility.** After a trade's consolidated-last eligibility
  is resolved against the pre-trade state (a literal YES, or a note that evaluated true), set
  `has_qualifying_last` and `last_participant`. *Draft 1 updated state only on literal YES cells,
  which was wrong.* On the 20 diagnostic sessions the fix changed **0 of 10,522,468** trades,
  because a literal-YES trade always came first each day. The fix is still required for
  correctness.
- **Multi-note assumption:** if several notes are present, all must evaluate true. CTS doesn't
  address that case.
- **Counting conventions** (they caused draft 1's confusion):
  - `observed_occurrences_sample` gives **overlapping per-condition occurrence counts** in the
    70-session sample (45,704,447 trades). One trade carrying " |F|I" counts under SPACE, F and
    I, so the column doesn't sum to the trade total.
  - SPACE is counted as a code, so the sample has **17 codes including SPACE** (16 non-space
    codes) in 42 combinations.
  - The sample covers every 10th session plus the first and last; full-store figures are labelled
    as such.

## C. Ordering (not exact tape order)

| Purpose | Rule |
|---|---|
| SIP note and state evaluation | Live: stream arrival order. **Replay: a deterministic approximation**, by timestamp then stored provider order. The historical data has no CTS dissemination sequence |
| Minute assignment | The trade's provider timestamp, floored to the minute |
| Open/close selection | First and last eligible trade by (timestamp, stored order) |

This matters most for `NOTE_2` and `NOTE_3`. Replay doesn't claim to reconstruct tape order.

## D. Assumptions in use (E-5), with full-store coverage

| ID | Code | Window | Full-store use | Affected fields |
|---|---|---|---|---|
| OPEN-SRC-1 | `O` | 2024-02-01 to 2024-03-28 (exclusive) | 72 trades; all 39 sessions; 39 slots (the opening minute of each) | `upd_oc`, `upd_hl`, `upd_vwap`, SIP state |
| OPEN-SRC-2 | `N` | 2024-02-01 to 2024-05-28 (exclusive) | 3,096 trades; 80 sessions; 1,583 slots | `upd_vol` |

`O` is in every session of the full store: 72 trades before the change, 1,193 in 645 sessions
after. Draft 1's CSV showed 0 for the earlier row because the whole sample count had been put on
the later row. It's corrected: 7 before, 126 after, in the sample.

**Held-out use:** prefer a verified rule. Otherwise disclose the assumption, its affected
intervals, and whether any conclusion depends on it.

## E. Evidence

**Method:** we built regular-session minute bars from our trades with the adapter, then compared
them with Alpaca's bars over **20 sessions, 7,620 minutes**: 15 in 2024 and 5 after 2025-11-03.
Agreement means `|ours − provider| ≤ 1e-6` for prices, volume and count, and ≤ 5e-6 for VWAP.
Script: `research/eligibility/bar_final.py`. The resolved-state fix changed 0 trades in these
sessions, so the table holds for draft 2.

| Field | Agreement | Discrepancies, classified |
|---|---|---|
| Open | 7,611 / 7,620 (99.88%) | 9 `opening_minute_T_print`: see below |
| High | 7,620 / 7,620 | |
| Low | 7,620 / 7,620 | |
| Close | 7,620 / 7,620 | |
| VWAP, last-eligible | 7,620 / 7,620 | Agreement on this sample within the stated tolerance. It's not a provider contract: Alpaca doesn't publish its VWAP population |
| Volume | 7,534 / 7,620 (98.87%) | 86 `likely_provider_finalization_difference`; root cause hypothesized, not verified |
| Trade count | 7,528 / 7,620 (98.79%) | 92, the same category |

**Opening-minute observation**, stated narrowly. In all 9 tested opening minutes where we differ,
the provider open equals the price of the **first round-lot `T`-conditioned print** in that minute.
Those prints are stamped 0.2–12 ms after 09:30:00, mostly from NYSE Arca (`P`), and our adapter
excludes them. In one case (2024-09-06), earlier odd-lot `T` prints were not the provider's open.
The full print lists are in `research/eligibility/evidence_v2.py` output. This is an observation,
not an established provider rule.

**Volume and count category.** Our value is higher. A trade on the final tape, usually in the first
0–3 s of the minute, is absent from the provider bar. That's consistent with Alpaca's documented
one-time late recalculation, but final historical data can't show provider arrival times, so the
cause isn't attributed per minute.

**Round-lot structural break** (2025-10-27…31 against 2025-11-03…07). It applies to **every**
population, E-4(a) included:

| Measure | Before | After |
|---|---|---|
| 40–99-share trades: share of trades / volume / notional | 17.0% / 14.0% / 14.0% | 31.4% / 24.7% / 24.6% |
| Odd lots (`I`): share of trades / volume / notional | 66.0% / 20.8% / 20.8% | 46.9% / 7.1% / 7.1% |

Under E-4(a), odd lots alone went from excluding 20.8% of volume to 7.1%, so the VWAP population
grew by roughly 14 points of volume at the break. The full population share, net of the other
excluded codes, wasn't measured; it belongs in the full-history report.

## Pre-market data (`spy_trades_premarket`, downloaded 2026-10-23)

- **Coverage:** 684 / 684 sessions (2024-02-01 to 2026-10-22), 184 MB, every file stamped
  `352077f`. Kept **raw and unfiltered**, so the adapter can be rerun.
- **Validation** ([premarket_validate.py](../research/eligibility/premarket_validate.py)): no
  problems.
  - Every timestamp is inside 04:00 ET to the open, and every file is sorted.
  - All tape B; 14,424,527 trades, 990.6M shares.
  - 10 codes in 17 combinations, **no unknown codes**; 99.97% of trades carry `T`.
- **E-6 price-eligible share** (literal-YES approximation, before stateful notes): 30.6% of trades,
  86.5% of volume. Odd lots are 69% of pre-market trades but a small share of volume.
- **Volume against the provider's pre-market bars**
  ([premarket_volume_diag.py](../research/eligibility/premarket_volume_diag.py), 20 sessions):

  | Measure | Result |
  |---|---|
  | Minutes where the provider has a bar | Volume agrees in **5,391 / 5,393** |
  | Daily totals | Ours run about 0.3% higher (median) |
  | Cause | `provider_no_bar_volume_drop`. In **19 / 20** sessions, the daily gap **exactly equals** our volume in minutes where the provider has no bar, and all 1,098 such minutes contain only odd lots. The provider seems to emit no bar, and so no volume, for a minute without a price-eligible trade. That's evidenced exactly on this sample, and matches an Alpaca forum statement, but it isn't documented provider behaviour |
  | Our design | Deliberately different: SD-03's volume-only slot keeps its volume with null prices |
  | Residual | 1 session (2025-09-18), 278 shares, the same size as the 2 mismatching minutes; not investigated further |

## Implementation

- [src/tournament/marketdata/eligibility.py](../src/tournament/marketdata/eligibility.py): config
  loading, classification per date and use mode (`development`, `holdout` or `live`, which scopes
  E-5), CTS combination rules and constraints, `LastState` updated from resolved eligibility.
- [src/tournament/marketdata/bars.py](../src/tournament/marketdata/bars.py): stage 2. Every
  scheduled slot gets a row with its `interval_status`; one price population for OHLC and VWAP;
  cumulative `vwap_notional`, `vwap_volume` and `total_volume`; `vwap_uncertain` from the first
  uncertain slot; `premarket_levels` with its own state.

## Full-history report (checklist item 8)

The production builder, `Usage.DEVELOPMENT`, against provider bars for **all 684 sessions,
265,680 regular minutes** ([full_history_report.py](../research/eligibility/full_history_report.py);
output in `data/reports/d04_full_history.json`). Tolerances: 1e-6 for prices, volume and count;
5e-6 for minute VWAP.

| Field | Agreement | Discrepancies by category |
|---|---|---|
| Open | 99.887% | 275 opening-minute T print · 23 finalization · 2 unexplained |
| High | 99.990% | 23 opening-minute T print · 4 finalization · 1 unexplained |
| Low | 99.992% | 15 opening-minute T print · 4 finalization · 3 unexplained |
| Close | 99.9996% | 1 finalization |
| Minute VWAP | 99.645% | 624 opening-minute T print · 318 finalization · 2 unexplained |
| Volume, trade count | 99.129% | 2,314 finalization |

**Categories and evidence status:**

| Category | Rule | Status |
|---|---|---|
| `opening_minute_T_print` | Prices in slot 1: the provider value equals a T-conditioned print in that minute. VWAP in slot 1: our last-eligible trades **plus round-lot T prints reproduce the provider's VWAP** within 5e-6 | Observed and reproduced; the provider's rule is not documented |
| `likely_provider_finalization_difference` | Any field, in a minute where our volume exceeds the provider's, i.e. a trade on the final tape absent from the provider bar | Hypothesized, not verified |
| `unexplained` | 8 field values in 5 minutes | See below |

**The 8 unexplained values:**
- **3, a provider price-precision artifact.** 2026-07-10 (open, twice) and 2026-07-13 (low): sub-penny
  trade prices such as 753.942907 against the provider's 753.9429. The provider appears to round to
  4 decimals; every difference is below 1e-5.
- **5, in two minutes of 2024-06-03.** 10:26 and 10:29 ET: high, low and VWAP differ. That morning
  coincides with the publicly reported NYSE technical issue, with halts and later trade
  cancellations; *to verify*. A final tape without busted trades would differ from a live bar. The
  cause isn't established.

**Coverage and data status across all sessions:**
- Every slot is `PRESENT`, and coverage matches the provider exactly.
- 0 quarantined trades; no session with `vwap_uncertain`.
- The E-5 assumptions applied exactly where expected: `O` in 39 slots, `N` in 1,583.
- **Pre-market levels:** 684 / 684 sessions produced PMH and PML. PMH equals the provider's
  pre-market high in 684, PML equals its low in 682.

## Freeze checklist (owner's list)

1. [x] Conditional state updates use resolved eligibility; ordering claims narrowed (this draft)
2. [x] E-4(a) selected; SD-04 and the snapshot schema name the population and its accumulators
3. [x] E-5 recast as scoped development-only assumptions; the gaps stay open
4. [x] Synthetic fixtures ([tests/test_bars.py](../tests/test_bars.py), 84 cases, expected values
       hand-written from CTS): every matrix row; every observed regular combination; qualifying
       `NOTE_2`/`NOTE_3`; multi-note; informational state; constraint violations; unknown codes;
       use-mode scoping of E-5; volume-only and gap slots; VWAP accumulators; ordering;
       pre-market marker and separate state
5. [x] "Zero unexplained" replaced with classified categories and an evidence status
6. [x] Sample counts and `O` coverage reconciled
7. [x] Pre-market trades downloaded, kept raw, and validated (E-6). See "Pre-market data" below.
       Building PMH and PML under the adapter is part of the bar-builder code
8. [x] Full-history report: all 684 sessions, stated tolerances, evidence-status categories
       (below)
9. [ ] Provenance: OPEN-SRC-1 to 4 resolved where possible (CTS 2.9g; an edition before 2.10a;
       go-live dates; the CTS lifecycle for `M`, `Q` and `9`)

## Open source gaps

- **OPEN-SRC-1:** the `O` rule before 2024-03-28 (CTS 2.9g or the edition in force).
- **OPEN-SRC-2:** `N` eligibility before 2024-05-28.
- **OPEN-SRC-3:** go-live dates behind the version-history entries.
- **OPEN-SRC-4:** the CTS lifecycle for informational records (`M`, `Q`, `9`): do they take part
  in consolidated-last state exactly as their matrix row suggests?
