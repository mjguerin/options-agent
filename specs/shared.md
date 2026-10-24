# Shared definitions (MD-02 to MD-07, BRD 6.1)

Status: **DRAFT v0.4, not approved.** Feature version `features-v1` once approved. Every strategy
uses these definitions as written and may not redefine them. Parameter names in `code font` live in
versioned configuration (SD-14). Decision IDs (D-nn) refer to [decisions.md](decisions.md).

**Two layers.** The strategy engine runs a deterministic **underlying reference-position model**,
identical in replay and paper (SD-11). Strategy timers, one-position eligibility, cooldowns,
quotas and directional brakes are all governed by that model. Paper option execution keeps its
own positions and safety controls (Step 3). Execution outcomes never change strategy state,
unless a future approved version deliberately adds such a dependency.

## SD-01 Clock, bars and availability

**Session minute slots.** A regular session is divided into scheduled one-minute slots
m = 1 … N, where N = 390 on a full day and 210 on a 13:00 early close. Slot m covers
[open + (m − 1) min, open + m min).

Each slot has these times:

| Time | Meaning |
|---|---|
| `bar_start(m)` | open + (m − 1) min |
| `bar_end(m)` | open + m min |
| `scheduled_finalization_at(m)` | `bar_end(m)` + `finalize_delay` (2 s, D-03). This is a **decision-snapshot policy**, not a guarantee that every relevant trade has arrived |
| `finalized_at(m)` | The **actual** moment the slot's snapshot is frozen: the later of the scheduled time and the moment all input received up to the ingestion boundary has been processed |
| `decision_at(m)` | When strategies evaluate slot m: at or after `finalized_at(m)` |
| `receive_ts` | When an input actually arrived (live only) |

**Rules.**
- A decision on slot m uses exactly one immutable snapshot, frozen at `finalized_at(m)`. Every
  feature value carries `available_at` = the **actual** `finalized_at`. A delayed snapshot never
  claims to have been available at the scheduled time.
- Clocks that must not stretch with processing delays are anchored to **market time**:
  - strategy timers to `bar_end` (SD-11)
  - candidate expiry to `scheduled_finalization_at` (SD-10)

  A slow pipeline can therefore only shorten a window, never lengthen it.
- The 2-second timer never overrides a data guard. If feed health is uncertain, the slot's
  `interval_status` decides (below), not the timer.
- **Monitoring** (live and shadow): arrival delay distribution, late-trade and correction counts
  after finalization, their effect on OHLC, VWAP and volume, processing lag, and whether any of it
  would have changed a decision. The delay is chosen for data quality and timeliness, **never
  against strategy P&L**.
- **Windows and timers count scheduled slots**, not observed bars. "Within n bars after bar j"
  means slots j + 1 … j + n, both ends inclusive. A deadline at slot j + n is checked after that
  slot's other checks, so a trigger on slot j + n still counts.
- Strategies and underlying exits evaluate on finalized bars only (v1). Intrabar logic is a later,
  versioned change.
- **No intrabar ordering is inferred from OHLC**, with one valid exception: close is the last
  eligible price in the bar, so a bar whose close is below its high had that high before its close.

**Interval status.** Every slot gets exactly one status:

| `interval_status` | Meaning | Live source | Replay source |
|---|---|---|---|
| `PRESENT` | At least one price-eligible trade | Trades received | Trades on the tape |
| `KNOWN_NO_ELIGIBLE_TRADES` | The feed was healthy and nothing price-eligible traded (possibly volume-only) | Connection healthy, heartbeats on time, no price-eligible trade | An empty slot **without** a download gap flag. This is a declared replay approximation: the tape can't prove feed health |
| `DATA_MISSING_OR_UNCERTAIN` | The feed may have missed inputs | Disconnect, heartbeat gap, sequence gap, stale feed | A download gap or quality flag |
| `HALTED` | A trading halt covers the slot | Halt and resume events | Halt records, where the source has them |

- No status ever produces a fabricated OHLC.
- Entries are allowed only on `PRESENT` slots (SD-10).
- **Setups in progress** continue through `KNOWN_NO_ELIGIBLE_TRADES`, because nothing traded and
  no failure could have happened. They're **cancelled** on `DATA_MISSING_OR_UNCERTAIN` or
  `HALTED`, because a failure might have happened unseen and can't be inferred later (D-32).
- Timers advance on scheduled slots under every status. A gap never pauses or extends a lifetime.

**Late inputs (live).** A trade arriving after its slot is finalized never changes that bar or any
decision already made. It's recorded as `late`, and its volume and notional enter the cumulative
VWAP and volume state from the next finalized slot onward. Corrections and cancels follow the same
rule (SD-03). Emitted snapshots and decisions are never rewritten.

**Replay is an event-time approximation.** The historical tape has no arrival history, so replay
assumes every trade of slot m arrived by `finalized_at(m)`. Replay results are labelled
`clock_model = event_time_finalize_2s`.

## SD-02 Session calendar

- Source: Alpaca's calendar, the selected provider calendar, stored as `data/history/calendar.parquet`
  with its dataset ID.
- Stored in UTC; the exchange-local schedule is derived from it. Every rule written as "close − X"
  is relative to the session's own close, so it shifts on early closes.
- If the calendar is missing, or disagrees with observed trading (for example, trades on a
  calendar holiday), the session is marked `calendar_inconsistent`. No strategy evaluates it, and
  an incident is recorded.

## SD-03 Trade eligibility and bar construction

**Eligibility matrix.** Its construction is a **separate deliverable and approval gate (D-04)**,
specified in the next subsection. The draft deliverable is in [eligibility.md](eligibility.md). Each trade is classified by the versioned matrix
`eligibility(trade, tape, session_part, rules_version)` into four independent flags:

| Flag | Used for |
|---|---|
| `upd_oc` | Bar open and close |
| `upd_hl` | Bar high and low |
| `upd_vol` | Bar volume, RVOL |
| `upd_vwap` | VWAP notional and volume |

- **Multiple conditions** combine as the applicable source rules and footnotes say. No universal
  shortcut is assumed. The matrix records the combination rule; where the sources are silent, the
  application rule must be stated explicitly and approved.
- **Unknown condition** (D-36): the trade is quarantined (excluded from every field), and:
  - **Scope:** the affected symbol and slot become `DATA_MISSING_OR_UNCERTAIN`. SPY is shared
    input, so all five workers are affected. Entries are blocked and setups in progress are
    cancelled.
  - **Recovery:** a later clean slot doesn't resurrect cancelled setups; new setups form normally.
  - **Late discovery:** if the unknown condition arrives after its slot was finalized, earlier
    decisions stand. The record notes when the uncertainty became known, and the guard and
    cancellation apply from that discovery onward, never retroactively.
  - **Positions keep being managed:** reference positions and option positions continue under
    their missing-data contingency and scheduled deadlines (SD-11). The guard never disables exits.
  - **Monitoring:** unknown codes, affected slots, setup cancellations and late discoveries are
    tracked and alerted. "Near zero" is an expectation to monitor, not an assumption correctness
    relies on, so a mapping defect can't silently suppress the tournament.
- **Session part matters.** In the regular session, extended-hours conditions (Form T `T`, `U`) are
  price-ineligible. In pre-market they are the normal case and **are** eligible for the
  pre-market high and low (SD-07). Without this rule PMH and PML would never exist.
- **Ordering ties:** trades with equal timestamps keep source order (the trade ID where the tape
  provides one).

**D-04 deliverable: the eligibility matrix.**
- **A. Source register.** For each source document (CTA and UTP output specifications and condition
  tables, plus any provider encoding documents): title, revision or effective date, retrieval date,
  document hash, applicable tape or feed, and the relevant sections and footnotes. The history
  (Feb 2024 onward) and the paper run may span several rule versions.
- **B. Machine-readable matrix.** Columns: `tape`, `condition_code`, `effective_from`, `effective_to`,
  `consolidated_high_low_rule`, `consolidated_last_rule`, `volume_rule`, `conditional_note_id` and
  `source_reference`. Conditional rules stay conditional; they're never flattened to booleans. For
  example, CTA's contingent-trade handling updates volume but excludes specified price statistics.
- **C. Minute-bar adapter.** The explicit mapping: raw trade + conditions + tape + context → SIP
  eligibility interpretation → the four flags above. **VWAP eligibility is an application policy**
  stated in the deliverable, not assumed from the volume rule.
- **D. Combination and lifecycle logic.** Multiple conditions, conditional footnotes, unknown codes,
  corrections and cancels, session context, ordering and late arrival, and provider encoding
  differences.
- **E. Acceptance evidence.** Unit fixtures for every implemented row, conditional note and
  supported combination; an integration comparison with provider bars; a discrepancy report; and
  owner approval of the application-specific mappings.

Until D-04 is approved, bar construction is not frozen and `features-v1` cannot be approved.

**Bars.** For slot m:
- open and close come from the first and last `upd_oc` trade; high and low from the max and min
  `upd_hl` trade; volume is the sum of `upd_vol` sizes; notional is Σ price × size over
  `upd_vwap` trades.
- **Empty slot:** no trade at all. The bar is `null`.
- **Volume-only slot:** volume or notional exists, but there is no price-eligible trade. Price fields
  are `null`; they are never forward-filled.
- In either case the slot gets the flag `no_price`. Its `interval_status` is never `PRESENT`
  (SD-01), so entries are blocked. VWAP and volume state still update.

**Corrections and cancels (live).** A cancel removes the original trade's contribution, and a
correction replaces it, in the cumulative VWAP and volume state from the next finalized slot. Past
bars and snapshots stay as emitted; the event is logged. Replay can't see corrections: the
historical tape is already final, which is a declared approximation.

**Verification.** Correctness is proven by synthetic tests per flag, condition combination,
correction, empty slot and volume-only slot ([acceptance_tests.md](acceptance_tests.md)). Comparing
with Alpaca's bars is a **diagnostic**: mismatches are reported by field, time of day and
condition, and by whether a strategy decision would have changed. A single overall match rate
doesn't count as proof.

## SD-04 VWAP and VWAP displacement

- **Last-eligible session VWAP** (E-4(a), population `last_eligible_execution_trades`):
  `VWAP(m) = vwap_notional(m) / vwap_volume(m)`, where both accumulators sum over `upd_vwap` trades
  (the same population as bar open and close) from the open through slot m. Reset each session;
  pre-market is excluded.
- The snapshot persists `vwap_notional`, `vwap_volume` and `total_volume`. `total_volume` uses the
  `upd_vol` population (odd lots included), a **different** population. So session VWAP is never
  rebuilt by weighting minute VWAPs with bar volume.
- VWAP is cumulative, so it carries forward unchanged through a `KNOWN_NO_ELIGIBLE_TRADES` slot,
  once its denominator is positive. A `DATA_MISSING_OR_UNCERTAIN` slot is different: VWAP after it
  is flagged `vwap_uncertain` for the rest of the session. An unknown gap is never converted into
  a valid unchanged value. `vwap_uncertain` makes VWAP and everything built on it (`disp_30_atr`,
  regime, VMR's d) **not ready**. So only strategies that use VWAP stop (VPC, FBR, VMR); ORB and
  BRT are unaffected. Restoring VWAP by backfilling the missed trades from the historical API is a
  possible later, versioned addition.
- Displacement is indexed by scheduled time: VWAP at the end of slot m against VWAP at the end of
  slot m − 30.
- **VWAP displacement**:
  `disp_30_atr(m) = (VWAP(m) − VWAP(m − 30)) / A_cur(m)`.
  It's a dimensionless displacement over 30 minutes, not a per-minute rate, and it's compared with
  plain numbers such as 1.5, never "1.5 A".
- It needs slots m and m − 30, so the first value is at **m = 31**: the bar ending 10:01, decided at
  10:01:02. The 09:30 boundary has no accumulated volume, so a 10:00 value would need a special
  opening convention; none is defined. Before slot 31 it is NOT_READY, and it also needs ATR ready
  and both VWAP endpoints valid.

## SD-05 ATR (the unit A)

- True range: TR(1) = high − low (no overnight gap); TR(m) = max(high − low, |high − close(m−1)|,
  |low − close(m−1)|), using the most recent bar with a valid close.
- **Session reset**, proposed:
  - Bars 1–13: `A_cur` = the prior session's final ATR, labelled `atr_source = PRIOR_SESSION_FALLBACK`.
  - Bar 14: `A_cur` = mean(TR(1) … TR(14)), `atr_source = CURRENT_SESSION`.
  - After that: A(m) = (13 × A(m−1) + TR(m)) / 14.
- Slots without a price have no TR; A carries forward unchanged and the slot is counted as a gap.
  Bar 14 initializes only once 14 valid TRs exist.
- The first session of the data has no fallback, so bars 1–13 are NOT_READY there.
- **The fallback never authorizes an entry.** Entries need `atr_source = CURRENT_SESSION`. The
  fallback's only use is building the 09:30 level zones (SD-07).
- **Named references:**
  - `A_cur(m)` is the current value at slot m.
  - `A_setup` is `A_cur` frozen at the bar a setup starts.
  - `A_OR` is `A_cur(15)`, frozen for ORB's day.
  - Every strategy states which reference each threshold uses. The default: **setup thresholds
    and buffers use `A_setup`**, and regime and eligibility use `A_cur`.

## SD-06 Swing points

- A **swing high** at pivot bar p: high(p) > high(b) for every b in p − k … p − 1 and p + 1 … p + k
  (strict; ties disqualify). Swing lows are the mirror image. k = `swing_k` (2).
- All 2k + 1 bars must lie in the same session and have valid prices. Any gap, or the session edge,
  means no pivot.
- **Confirmed at slot p + k** and available from `finalized_at(p + k)`. Strategies may act on a
  swing only once it's confirmed.
- Each swing record holds: `swing_id` (session, side, p), pivot time, price, `confirmed_at`, k and
  feature version. Once confirmed, a swing is immutable.
- **Post-entry swings**, for trailing stops, are those with pivot bar p > the entry bar.

## SD-07 Reference levels and zones

**Levels.**

| Level | Definition | Known from |
|---|---|---|
| PDH / PDL | Prior session's regular-hours high and low (eligible prices) | Open |
| PDC | Prior session's last `upd_oc` regular-session trade, not the official close; a feature only, not a zone member | Open |
| PMH / PML | High and low of pre-market eligible trades, 04:00 to open. Absent if there were none | Open |
| ORH / ORL | Max high and min low over **scheduled** slots 1–15 (09:30–09:45) | `finalized_at(15)` |

- **OR completeness:** all 15 slots must have valid prices; otherwise the OR is `incomplete`. It is
  then absent for the day, and ORB is INELIGIBLE (`OR_INCOMPLETE`).
- **Midpoint pairs** (FBR targets): ORH↔ORL, PDH↔PDL, PMH↔PML. Each pair's midpoint is
  (upper + lower) / 2. If either half is absent, the midpoint is absent.

**Zones.** Levels are grouped into zones by **single-linkage clustering**:
1. Sort the member levels by price.
2. Start a new zone whenever the gap to the previous level exceeds `cluster_gap` × `A_zone`
   (`cluster_gap` = 1.0).
3. This is transitive: with A = 1.00, levels at 100.00, 100.80 and 101.60 form **one** zone spanning
   100.00–101.60.

Each zone has `zone_id`, `zone_low`, `zone_high`, member levels, `created_at`, `A_zone` and
`zone_version`.

| Zone version | Built at | Members | `A_zone` |
|---|---|---|---|
| `zv1` | Open | PDH, PDL, PMH, PML | Prior-session fallback ATR |
| `zv2` | `finalized_at(15)` | `zv1` members plus ORH and ORL (if the OR is complete) | `A_cur(15)` |

- Zones are rebuilt only at those two moments. A setup binds to the zone version current when it
  starts and keeps it for life.
- Upside tests use `zone_high` and downside tests use `zone_low`. Each strategy names the edge it
  uses.

## SD-08 Relative volume

- `rvol(m) = volume(m) / baseline(m)`, where baseline(m) is the mean volume of slot m over the
  previous 20 **reference sessions** that have a valid slot m.
- Reference sessions are full-length sessions; early-close sessions are excluded from the baseline.
  On an early-close day, slot m still compares with full sessions' slot m.
- NOT_READY until 20 reference values exist, or if the baseline is 0. An empty slot has
  volume = null, so rvol is null; a real zero volume is 0, never null.
- Only strategies whose rules use rvol wait for it. VMR and FBR don't use rvol.

## SD-09 Regime

Evaluated at each slot from m = 31.

| Quantity | Definition |
|---|---|
| `occ_up_30(m)` | Share of slots m − 29 … m with close > VWAP, among slots with a valid close. If fewer than `regime_min_valid` (27) valid closes, the regime is NOT_READY |
| `side(m)` | +1 if close > VWAP, −1 if close < VWAP; if exactly equal, `side` carries the previous non-zero side |
| `crosses_60(m)` | Number of slots in m − 59 … m where `side` differs from the previous slot's side. Needs the full 60-slot window: defined from m = 60 (the bar ending 10:30) |

Classification, first match wins:

| Order | Regime | Rule (thresholds are hypotheses, Q-SD-1) |
|---|---|---|
| 1 | TREND_UP | `disp_30_atr` ≥ `trend_disp` (1.5) and `occ_up_30` ≥ `trend_occ` (0.80) |
| 2 | TREND_DOWN | `disp_30_atr` ≤ −1.5 and `occ_up_30` ≤ 0.20 |
| 3 | RANGE | \|`disp_30_atr`\| ≤ `range_disp` (0.75) and `crosses_60` ≥ `range_crosses` (2) |
| 4 | INCOMPLETE | No trend, but `crosses_60` isn't ready yet (slots 31–59), so RANGE can't be decided |
| 5 | NEUTRAL | All components ready, and none of the above |

**Readiness is per component, with full windows only** (D-35):
- The trend tests need `disp_30_atr` (from slot 31) and a ready `occ_up_30`.
- RANGE needs the full 60-slot `crosses_60` (from slot 60).
- If the trend inputs aren't ready, the regime is NOT_READY.
- Missing history never turns into NEUTRAL. Before slot 60, a no-trend slot is **INCOMPLETE**,
  which no strategy treats as RANGE or NEUTRAL.

- No hysteresis in v1. The thresholds stay as starting hypotheses (Q-SD-1). Development checks
  look at operational behaviour only: share of time in each regime, transitions and short-lived
  classifications, warm-up boundaries, gap handling, and example sessions of different structure.
  Any revision is a new research variant, never a change to raise a backtest's profit.
- What a regime change does to a setup in progress is defined by each strategy; the draft cancels
  the setup rather than suspending it.

## SD-10 Session rules, deadlines and guards

**Common deadlines** (operating policy, D-07):

| Rule | Slot or time |
|---|---|
| Global floor | **No strategy may emit a candidate before 09:45 ET** (decision on slot 15 at the earliest), and every strategy must also satisfy its own window and feature readiness (D-08). Slot 15 is the **last** opening-range bar, not a bar after the range; the first bar wholly after the OR is slot 16 (09:45–09:46) |
| Setups before the floor | No strategy starts a setup before slot 15. Shared levels and indicators accumulate from the open. An event that happened before the floor never becomes eligible just because the clock reaches 09:45 |
| Last candidate | The slot whose `bar_end` = close − `last_entry_min` (30): slot 360 on a full day. A strategy's own `last_entry_slot` is capped by this on early closes |
| Cancel unfilled entry orders | At the later of each candidate's expiry and the last-candidate deadline (Step 3 execution) |
| Liquidation start (session cutoff) | Decision on the slot whose `bar_end` = close − `cutoff_min` (10): slot 380. Every open position exits with reason SESSION_CUTOFF |
| Desired flat | close − 5 min. Anything still open is an incident with an operator alert (Step 3) |

**Strategy-layer guards** (they decide strategy state):

| Guard | Scope | What it blocks |
|---|---|---|
| `underlying_feature_ready` | The features a strategy needs | New setups and candidates for that strategy |
| `entry_data_eligible` | This slot: `interval_status` = `PRESENT`, no `calendar_inconsistent` | New setups and candidates for all strategies this slot. Setups in progress follow SD-01 |

**Execution-layer guards** (Step 3). They block **submissions only** and never change strategy
state:

| Guard | Scope | What it blocks |
|---|---|---|
| `selected_contract_executable` | One chosen option contract | That submission. A bad quote on an unrelated contract blocks nothing |
| `position_management_health` | Broker connection, reconciliation | New option entries for the affected account. **Never blocks exits** |
| Account limits | Actual option positions, pending orders, risk (RK-01, FR-07) | The submission. The strategy candidate is still recorded |

- Entry blocking never stops exits, cutoffs or safety controls.

**Per-strategy limits** (on the reference model, not on option positions):
- **One reference position at a time** (`REF_POSITION_OPEN`). A reference position opens when a
  candidate is emitted (SD-11). Working option orders aren't strategy state; account limits handle
  them in the execution layer.
- **Cooldown:** after a reference exit decided on slot j, no candidate on slots j + 1 … j +
  `cooldown_bars` (5).
- **Candidate expiry** (D-37). Three distinct clocks:

  | Clock | Anchor |
  |---|---|
  | Strategy timers | `signal_bar_end` (SD-11) |
  | Candidate expiry | `expires_at = scheduled_finalization_at + candidate_ttl` (60 s), where `scheduled_finalization_at = signal_bar_end + 2 s`. That's the scheduled finalization of the next one-minute slot |
  | Telemetry | Actual `signal_available_at`, evaluation and emission times |

  - **Submission eligibility:** `signal_available_at ≤ now < expires_at`.
  - If the snapshot only becomes available at or after `expires_at`, the strategy layer still
    **emits the candidate and opens its reference position**. Strategy state never depends on
    processing time, which keeps replay and paper identical. The candidate is marked
    `submission_eligible = false`, and execution never submits it.
  - Expiry is an outer bound for execution only. Step 3 rechecks age, window, setup validity, contract quality, price
  deviation and risk before any submission. A late emission is never submission-eligible, and
  expiry is never extended.
- **Execution must also cancel or reject an unfilled entry once its reference position has
  exited.** A candidate's lifetime never permits buying an option for a thesis the strategy model
  considers finished.

**Quotas** are consumed when a candidate is **emitted**. Execution rejection doesn't refund the
quota and doesn't create a new candidate; retries reuse the same candidate ID.

## SD-11 Reference positions and exits

**The reference-position model is the strategy's own state machine.** It runs identically in
replay and in paper.

```text
Shared market inputs
        │
        ▼
Strategy + reference-position state machine ──► candidates, reference exits
        │                                        quotas, cooldowns, brakes
        ▼
Paper execution adapter (Step 3) ──► option orders and positions, premium stops,
                                     operational liquidation
```

**Opening.** Every emitted candidate opens exactly one reference position (schemas S-8):
- `reference_entry_price` = the signal bar's close
- `reference_entry_time` = `signal_bar_end`
- the frozen invalidation, target, trail and timers from the candidate

The entry price is a **bookkeeping anchor, not a claimed fill**: a decision computed after a close
doesn't prove an order could have filled at that close. The reference position opens whatever
execution then does.

**Strategy clock.** `strategy_clock_origin = signal_bar_end` of the signal slot e (D-05). It's
distinct from `signal_available_at` (+ `finalize_delay`) and `candidate_emitted_at`, and neither
one restarts or extends it. "Within n bars of entry" means slots e + 1 … e + n, so a deadline
falls at `signal_bar_end` + n scheduled minutes. **The final slot e + n is evaluated for progress
before the deadline is declared failed.**

**Reference exits.** These are the only exits that change strategy state. Each is evaluated on
finalized slots (bullish shown; bearish is the mirror image):

| Exit | Fires when |
|---|---|
| SESSION_CUTOFF | Slot = liquidation start (SD-10) |
| INVALIDATION | close ≤ `invalidation_level` |
| TARGET | close ≥ `target_price` |
| TRAIL | Trailing is active and close ≤ `trail_stop` |
| FOLLOW_THROUGH | At slot e + `ft_bars`, no close in e + 1 … e + `ft_bars` (that slot included) reached entry + `ft_r` × `r_u` |
| STAGNATION | `stag_bars` consecutive slots without a new highest close since entry |
| MAX_HOLD | Slot = e + `max_hold` |

- **Close-based only.** "Reaches +0.5R" means a close ≥ entry + 0.5 `r_u`; a wick doesn't count.
- **Without a close** (any non-`PRESENT` slot), INVALIDATION, TARGET and TRAIL can't fire and
  can't be inferred. Scheduled exits still do (SESSION_CUTOFF, FOLLOW_THROUGH, STAGNATION,
  MAX_HOLD); FOLLOW_THROUGH and STAGNATION use the closes that exist.
- **Exit price:** `reference_exit_price` = close of the exit slot. If that slot has no close, it's
  the last valid close before it, flagged `stale_reference_price`.

**Trailing.** It activates after the first close ≥ entry + `trail_activate_r` × `r_u`. After each
slot from then on: `trail_stop = max(trail_stop, latest post-entry confirmed swing low −
stop_buffer × A_setup)`. It's monotonic and never loosens; bearish uses min and swing highs.

**Precedence (EX-03).** When several reference exits fire on one slot, the first in the table
wins as the **primary** reason, and **all triggered reasons are recorded**. State rules that depend
on exit reasons look at the full triggered set, never only the primary reason, so reporting
precedence can't silently change behaviour (see VMR's directional brake).

**Execution exits (Step 3, not part of strategy state).** Paper execution carries out every
reference exit as an option exit instruction. It adds its own safety exits: the premium-loss stop,
operational liquidation, operator flatten and emergency controls, broker-forced closes. **An
execution-only exit never changes reference quotas, cooldowns, brakes or timers.** Duplicate-order
prevention (pending exits, partial fills, remaining quantity) belongs to the order layer (EX-04,
EX-05).

**Divergence handling.** Strategy history and paper-account history are recorded separately.
Where they differ, that's evidence about execution, not necessarily a defect:

| Situation | Strategy / reference | Execution |
|---|---|---|
| Candidate emitted, option entry rejected | Reference position opens and runs its rules | Rejection recorded |
| Option premium stop exits early | Reference continues until its own exit fires | Account is flat for that strategy |
| Reference exits while the option is still open | Reference exits; cooldown starts | Submits and manages the option exit |
| New candidate while the previous option position is still open | Candidate recorded | Submission blocked by account limits, recorded |
| Reference exits before the entry order fills | Reference closed | Cancel the pending entry; any late fill is exited immediately and logged |

## SD-12 Readiness, eligibility, setup state and outcome

Every evaluation records four separate things (schemas: StrategyEvaluation):

1. **Readiness:** are the features this strategy needs available? Missing values are `null` with
   reason codes.
2. **Eligibility:** may it act now (window, regime, guards, position, cooldown, quota)?
3. **Setup state:** the strategy's own state-machine state. `IDLE` means no setup in progress.
4. **Outcome,** one of the five BRD outcomes:

| Outcome | When |
|---|---|
| NOT_READY | A required feature is not ready |
| INELIGIBLE | Ready but not allowed to act. A setup in progress follows SD-01 when the cause is the slot's `interval_status`. For any other cause (window, regime, quota) the strategy file decides; the default is to cancel |
| WATCHING | Eligible and either IDLE (explicitly included) or a setup in progress |
| CANDIDATE | A candidate was emitted on this slot |
| INVALIDATED | A setup ended without a candidate on this slot (structural failure, timeout, room failure, regime change) |

## SD-13 Setup-processing order within one slot

Unless a strategy explicitly says otherwise, each setup in progress processes a slot in this order:
1. Update anchors (for example, a new lowest low)
2. Check structural failure and expiry. A failure ends the setup; nothing after it runs
3. Advance at most **one** stage
4. Check the trigger (only if no stage advanced this slot)
5. Check the deadline

A new setup can start on a slot only if no setup in that direction is in progress. Bullish and
bearish setups run independently and symmetrically.

## SD-14 Configuration and invariants

Every research choice is a named, versioned configuration parameter. That includes thresholds,
buffers, R multiples, windows and fallback distances. Invariant rules may stay in code: the
look-ahead rule, strict inequalities for swings and precedence order.
A behavioural change creates a new strategy version and evaluation segment (FR-09); an editorial
change does not.

## Open questions

None in this file. Settled in v0.4: Q-SD-1 (regime thresholds kept, D-30), Q-SD-5 (2 s
decision-snapshot policy, D-03) and Q-SD-6 (09:45 global floor, D-08). The remaining gate is
the D-04 eligibility-matrix deliverable.
