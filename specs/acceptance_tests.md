# Acceptance scenarios (synthetic)

These scenarios become pytest fixtures before any comparative replay is looked at. Unless a
scenario says otherwise:
- A = 1.00, held constant: `A_setup` = `A_cur` = `A_OR` = 1.00.
- RVOL is ready and ≥ every threshold.
- Guards pass, and it's a full-length session.
- Prices are SPY-like but arbitrary.
- "Slot n" is a scheduled session slot (SD-01).

Each strategy gets the ten standard cases (S1–S10) and its critical extras (X…). The expected
results are exact: outcome, reason code, and the candidate's fields where one is emitted.

## Shared components

| ID | Scenario | Expected |
|---|---|---|
| SH-01 | A trade stamped inside slot 40 arrives after `finalized_at(40)` | Bar 40 and its snapshot are unchanged. The trade is flagged `late`. Its volume and notional enter VWAP from slot 41's state |
| SH-02 | A cancel arrives for a trade inside slot 40, after finalization | Past snapshots are unchanged. From slot 41, cumulative VWAP and volume exclude the trade. A `correction` event is logged |
| SH-03 | Per-flag eligibility: one trade with a condition that is volume-eligible but not price-eligible (an odd lot) | It adds to volume and VWAP but not to OHLC |
| SH-04 | Multiple conditions on one trade, including a conditional footnote | Flags follow the matrix's recorded combination rule for that pair (D-04); a combination the matrix doesn't cover is treated as an unknown condition (D-36) |
| SH-05 | Unknown condition code | All flags false; `unknown_condition` counted |
| SH-06 | A volume-only slot, feed healthy | `interval_status` = KNOWN_NO_ELIGIBLE_TRADES. OHLC null, volume > 0. Entries blocked; setups in progress continue; timers advance; ATR carries forward; VWAP updates |
| SH-07 | An empty slot | Bar is null; the next TR uses the last valid close. In replay without a gap flag, the status is KNOWN_NO_ELIGIBLE_TRADES |
| SH-08 | Pre-market with only Form T trades | PMH and PML exist (session-part rule) |
| SH-09 | Bars 1–13 with fallback ATR, bar 14 | `atr_source` is FALLBACK through bar 13; at bar 14, A = mean(TR 1–14); bar 15 = (13·A14 + TR15) / 14 |
| SH-10 | `disp_30_atr` boundary | Null at slot 30, defined at slot 31 |
| SH-11 | `crosses_60` boundary | Null at slot 59, defined at slot 60; RANGE impossible before slot 60 |
| SH-12 | close = VWAP exactly | `side` keeps the previous non-zero side; no cross counted |
| SH-13 | Clustering 100.00, 100.80, 101.60 with A = 1.00 | One zone [100.00, 101.60] |
| SH-14 | A setup starts on `zv1`; `zv2` merges its level into a wider zone at slot 15 | The setup keeps its `zv1` edge; new setups use `zv2` |
| SH-15 | OR with one slot in 1–15 lacking a price | OR incomplete; ORB INELIGIBLE (`OR_INCOMPLETE`); `zv2` built without ORH and ORL |
| SH-16 | Swing tie: high(p) = high(p + 1) | No swing at p |
| SH-17 | Swing pivot at slot 389 (k = 2) | No swing (the session edge) |
| SH-18 | Early close (N = 210) | Last candidate slot 180, liquidation slot 200 |
| SH-19 | Cooldown: exit decided on slot 100 | No candidate on slots 101–105; one allowed on slot 106 |
| SH-20 | Snapshot for the trigger slot available 61 s after `scheduled_finalization_at` (past `expires_at`) | Candidate still emitted and reference position opened, with `submission_eligible = false`; execution never submits. Replay of the same session gives the same reference state |
| SH-21 | Same `seq` on two different sessions | Different `snapshot_id` and `evaluation_id` (no collision) |
| SH-22 | One event delivered twice | One event kept. Two distinct events with identical content are both kept (different `event_key`) |
| SH-23 | The same replay run twice | Same `signal_key`s; different `run_id` and `candidate_id` |
| SH-24 | A feature isn't ready | Its value is null with a reason code; never 0 or NaN |
| SH-25 | A slot where a strategy's state doesn't change | One StrategyEvaluation, no StrategyTransition |
| SH-26 | A premium stop (or operator flatten) closes the option position | **No** strategy transition. The reference position, cooldown and quota are unchanged; an execution record is written |
| SH-27 | Candidate features compared with the snapshot | They match exactly |
| SH-28 | A candidate with an account binding that isn't the strategy's | Fails closed, with no submission (FR-02) |
| SH-29 | INVALIDATION and TARGET triggered on one slot | One exit: primary INVALIDATION, both reasons recorded |
| SH-30 | Trailing stop after a lower post-entry swing low | The stop doesn't move down (monotonic) |
| SH-31 | Max hold would land after the liquidation slot | SESSION_CUTOFF exits at slot 380 |
| SH-32 | Feed disconnect covering slot 70, with a setup in progress | `interval_status` = DATA_MISSING_OR_UNCERTAIN; the setup is cancelled; VWAP becomes `vwap_uncertain`. VPC, FBR and VMR are NOT_READY for the rest of the session; ORB and BRT resume on the next PRESENT slot |
| SH-33 | Halt covering slots 80–85 with a reference position open | Status HALTED; setups are cancelled. INVALIDATION and TARGET can't fire without closes; MAX_HOLD still fires on schedule |
| SH-34 | MAX_HOLD lands on a slot with no close | Exit with `reference_exit_price` = last valid close, flagged `stale_reference_price` |
| SH-35 | Follow-through progress first reached on the deadline slot e + `ft_bars` | No FOLLOW_THROUGH exit, because the final slot is evaluated before failure |
| SH-36 | `finalize_delay` and a late emission | Deadlines are still `signal_bar_end` + n minutes, with no shift |
| SH-37 | Candidate emitted, option entry rejected by execution | Reference position opens and runs; cooldown and quota are as if it traded; a rejection record is written |
| SH-38 | Reference exits on slot j while the option order is still unfilled | Execution cancels the entry; a late fill is exited immediately and logged. Strategy cooldown starts from j |
| SH-39 | New candidate while the previous option position is still open (its reference already exited) | Candidate recorded; submission blocked by account limits and recorded |
| SH-40 | Paper and replay over the same recorded session | Identical candidates, reference positions, cooldowns and quotas, whatever happens to the option fills |
| SH-41 | Snapshot available 30 s after `scheduled_finalization_at` | `expires_at` is unchanged (scheduled + 60 s); the submission window is 30 s, not 60 |
| SH-42 | Slot 45, no trend, `crosses_60` not ready | Regime INCOMPLETE (not NEUTRAL, not RANGE) |
| SH-43 | Unknown sale condition in slot 50 with setups active in three strategies | Slot 50 becomes DATA_MISSING_OR_UNCERTAIN; all three setups cancelled; entries blocked for all five; open reference positions keep being managed; an alert is raised |
| SH-44 | Slot 51 is clean after SH-43 | No cancelled setup is resurrected; new setups may form |
| SH-45 | An unknown condition for slot 50 discovered at slot 52 | Decisions on 50 and 51 stand; the record notes the discovery time; the guard and cancellation apply from slot 52 |
| SH-46 | A BRT break on slot 14; ORB price above ORH on slot 15 | No BRT setup (before the floor); no ORB trigger (slot 15 is an OR bar) |

## Standard cases for every strategy

| Case | Scenario |
|---|---|
| S1 | A valid setup → exactly one candidate with the expected entry, invalidation, `r_u`, target and timers |
| S2 | Room failure: the target is too close, or at or behind entry → INVALIDATED `RR_TOO_LOW` (or `TARGET_EXHAUSTED`) |
| S3 | A trigger on the last slot of the window → candidate. The same move one slot later → INVALIDATED timeout |
| S4 | Structural failure before the trigger → INVALIDATED with the strategy's failure code |
| S5 | Same-slot collision resolved by the declared precedence |
| S6 | A setup in progress when the session cutoff or a `no_price` slot arrives → per SD-10 (an entry block cancels the setup; a halt cancels the setup) |
| S7 | The quota is used: a second valid setup → INELIGIBLE (`QUOTA`) |
| S8 | The mirrored opposite-direction scenario gives the mirrored candidate |
| S9 | Anchor rules: re-anchor or immutability as the strategy file declares |
| S10 | A candidate emitted after expiry, or rejected downstream → the quota stays consumed; no new candidate |

## VPC

VWAP rises steadily, so TREND_UP holds unless stated.

| ID | Scenario | Expected |
|---|---|---|
| VPC-S1 | Swing high H = 505.00, pivot 100, VWAP(100) = 502.00 (≥ 2 A). Pullback low 502.60 at slot 104, with VWAP(104) = 502.10 (within 1 A). Swing low L = 502.40, pivot 106, confirmed at 108; P = max high over 106–108 = 503.00. Close 503.10 at slot 109 | CANDIDATE: entry 503.10, invalidation 502.15, `r_u` 0.95, target 505.00 (1.90 ≥ 0.95) |
| VPC-S2 | As S1, but H = 503.80 with VWAP(100) = 501.50, so H still qualifies (2.30 ≥ 2) | INVALIDATED `RR_TOO_LOW` (0.70 < 0.95) |
| VPC-S3 | The trigger close comes at slot 123 (c + 15) | CANDIDATE. At slot 124 instead → INVALIDATED `TRIGGER_TIMEOUT` |
| VPC-S4 | A close at VWAP − 0.6 during the pullback | INVALIDATED `PULLBACK_TOO_DEEP` |
| VPC-X1 | The confirmation bar c closes above P | Impossible by construction; no trigger on c |
| VPC-X2 | The pullback touch happens at slot 101, before H confirms at 102 | The reconstruction finds it, and PULLBACK is entered at slot 102 |
| VPC-X3 | In SUPPORTED (L = 502.40), slot 112 has low 502.35 and close 502.90 | INVALIDATED `SUPPORT_UNDERCUT` regardless of the close; `undercut_slot` = 112; H stays usable (replacement context) |
| VPC-X4 | The regime turns NEUTRAL in SUPPORTED | INVALIDATED `REGIME_LOST` |
| VPC-X6 | After X3, slot 112 itself pivots as a swing low (confirmed at 114) and passes the depth test | Replacement setup built in SUPPORTED on 114, with a new `setup_id`, L = low(112) and P frozen |
| VPC-X7 | After X3, the only confirming swing low has pivot 110 (< 112) | Not usable; the strategy keeps waiting |
| VPC-X8 | After X3, the regime is lost before any replacement | Context dropped, H retired; the next setup needs a strictly higher H |
| VPC-X5 | After a stop-out, the next swing high equals the old H | No new setup; it must be strictly higher |

## ORB

ORH = 500.00, ORL = 494.00, W = 6 (`W_A` = 6).

| ID | Scenario | Expected |
|---|---|---|
| ORB-S1 | Slot 20: close 500.40 (previous close 499.90), high 500.50, low 499.80, rvol 1.6 | CANDIDATE: entry 500.40, invalidation 498.50, `r_u` 1.90, target 506.00 |
| ORB-S2 | A gap bar closes at 506.20 | INVALIDATED `TARGET_EXHAUSTED` |
| ORB-S3 | The S1 crossing happens on slot 120, or instead on slot 121 | Slot 120 → CANDIDATE; slot 121 → no candidate (past `last_entry_slot`, DONE) |
| ORB-X5 | Crossing close at 502.40 (previous 500.05); `r_u` = 3.90, remaining 3.60 | INVALIDATED `RR_TOO_LOW` (0.92 < 1.0) |
| ORB-X1 | `W_A` = 2.5 at slot 15; ATR later halves | INELIGIBLE all day (`OR_TOO_NARROW`), unchanged by the ATR change |
| ORB-X2 | A crossing with rvol 1.2, then the next bar at rvol 2.0 still above the threshold | No candidate. Not a new crossing, and late confirmation isn't allowed |
| ORB-X3 | A zero-range bar above the threshold (on a crossing) | Confirmation fails |
| ORB-X4 | Follow-through: entry 500.40, `r_u` 1.90; no close ≥ 501.35 within 10 slots | Exit FOLLOW_THROUGH at e + 10, even if ORH + 0.25 W was exceeded at entry |

## BRT

Zone edge E = 500.00.

| ID | Scenario | Expected |
|---|---|---|
| BRT-S1 | Break: close 500.30 at slot 50 (previous 499.90). Retest low 500.20 at slot 53. RL bar 53 high 500.70. Slot 54 closes 500.80. Next zone low above is 503.00 | CANDIDATE: entry 500.80, invalidation 499.95, `r_u` 0.85, target 503.00 (2.20 ≥ 1.02) |
| BRT-S2 | Next zone low at 501.50 (within 6 A) | INVALIDATED `RR_TOO_LOW`; the obstacle isn't skipped |
| BRT-X1 | One bar after the break has low 500.30 (a retest) and high 504.10 (runaway) | INVALIDATED `NO_RETEST` (runaway wins) |
| BRT-X2 | Slot 55 sets a new RL and closes above the old RH | No trigger on 55, because RH is now high(55) |
| BRT-X3 | Equal lows at slots 53 and 55 | RL bar stays 53 |
| BRT-X4 | A close at 499.40 after the break | INVALIDATED `RETEST_FAILED` |
| BRT-X5 | A close above E when E was broken 60 slots earlier with no close back below | No new break (a crossing is required) |
| BRT-X7 | Retest bar with low 498.90 (E − 1.1) and close 500.10 | INVALIDATED `RETEST_TOO_DEEP`; it doesn't count as a retest |
| BRT-X8 | Retest bar with low 499.05 (E − 0.95) and close 499.60 (E − 0.4) | A valid retest (within both limits) |
| BRT-X6 | No zone above within 6 A | Target = entry + 2 `r_u` |

## FBR

E = 500.00 (from ORH); ORL = 494.00; `disp_30_atr` = 0.

| ID | Scenario | Expected |
|---|---|---|
| FBR-S1 | Slot 60: high 500.80, previous close 499.70. Slot 61: high 500.90, close 500.20. Slot 62 closes 499.80 (< 499.90), low 499.60 → `RetLow` = 499.60, X frozen at 500.90. Slot 63 closes 499.50. VWAP 497.00 | CANDIDATE: entry 499.50, invalidation 501.15, `r_u` 1.65, target 497.00 (2.50 ≥ 1.65) |
| FBR-S2 | VWAP = 498.60 | INVALIDATED `RR_TOO_LOW`; it doesn't fall through to the midpoint or 1.5R |
| FBR-X1 | An excursion bar with high 504.20 that closes at 499.50 | No setup (`EXCURSION_TOO_FAR` takes precedence) |
| FBR-X2 | VWAP above entry; midpoint 497.00 valid | Target 497.00 |
| FBR-X3 | VWAP above entry, and the E member has no pair (PMH with PML absent) | Target = entry − 1.5 `r_u` (no structural target) |
| FBR-X4 | The excursion bar itself closes at 499.85 | RETURNED on the same slot; the earliest trigger is the next slot |
| FBR-X5 | `disp_30_atr` reaches 2.1 during EXCURSION | INVALIDATED `REGIME_STRONG_TREND` |
| FBR-X9 | E's zone holds ORH 500.00 and PDH 500.60 (E = 500.60); PDL 492.60; VWAP above entry 499.50 | Midpoints: OR 497.00, PD 496.60 → nearest valid, 497.00. `target_source = range_midpoint`, `range_pair_id = OR` |
| FBR-X10 | As X9, but `r_u` = 2.80 | The nearest midpoint 497.00 is too close (2.50 < 2.80) → INVALIDATED `RR_TOO_LOW`. The farther PD midpoint 496.60 **would pass** (2.90 ≥ 2.80) but is never chosen: a too-close applicable target isn't skipped |
| FBR-X6 | Slot 29 excursion | No setup (before slot 31) |

## VMR

RANGE holds; VWAP = 500.00.

| ID | Scenario | Expected |
|---|---|---|
| VMR-S1 | Slot 100 close 506.10 (d = 6.1; previous d 5.8). Slot 101: high 506.50, low 506.00, close 506.20 (d = 6.2 ≥ 5). Slots 102–103 have lower highs, so S = 506.50 confirms at 103 with `S_low` = 506.00. Slot 103 closes 505.90 | CANDIDATE on 103 (the confirmation slot): entry 505.90, invalidation 507.00, `r_u` 1.10, target 501.475 (4.425 ≥ 1.10) |
| VMR-S2 | Entry at 500.80 | INVALIDATED `RR_TOO_LOW` (0.60 < `r_u`) |
| VMR-X1 | A high of 506.70 in EXHAUSTED before the trigger | Back to EXTENDED; S cleared |
| VMR-X2 | The regime becomes NEUTRAL in EXTENDED | INVALIDATED `REGIME_EXCLUDED` |
| VMR-X3 | The first bearish reference position exits with INVALIDATION | `directional_invalidation_brake` fires: no further bearish VMR candidates this session; bullish still allowed |
| VMR-X4 | The first bearish reference position exits on MAX_HOLD, and its option trade lost money | The brake doesn't fire (underlying rule, D-11) |
| VMR-X5 | Slot 59 stretch | No setup (before slot 60) |
| VMR-X6 | INVALIDATION and SESSION_CUTOFF on the same slot (primary = SESSION_CUTOFF) | The brake fires, because INVALIDATION is among the triggered reasons. It resets the next session |
| VMR-X7 | A setup INVALIDATED before any candidate | The brake doesn't fire |
| VMR-X8 | A premium stop exits the option; the reference later exits on TARGET | The brake doesn't fire |
