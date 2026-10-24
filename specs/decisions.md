# Decision register (specs v0.4)

| Status | Meaning |
|---|---|
| **PROPOSED** | Adopted from the owner's review, awaiting approval |
| **CHALLENGED** | The draft takes a different position than the review; the reason is given |
| **DRAFT** | Chosen by the drafter where the review asked for a decision |
| **OPEN** | Owner input needed before approval |
| **AGREED** | The owner accepted it (a drafter challenge, or the owner's own provisional decision). These are draft implementation defaults, frozen for `v1` after development correctness checks and before the paper tournament |

Nothing here is approved.

| ID | Decision | Status | Where | Notes |
|---|---|---|---|---|
| D-01 | No incumbent benchmark and no sixth account; "do not trade" stays as an analytical reference | **AGREED** | README | BRD v1.2 removed the incumbent, Phase 0 and the benchmark account (FR-12) |
| D-02 | History: development + held-out replay; selection = paper tournament; confirmation = later paper period. The held-out report's permitted consequences are declared in advance | **AGREED**; dates **OPEN** | README | Replay validates the implementation against the spec. It doesn't select or establish profitability. A report that changes a spec ends that period's untouched status |
| D-03 | `finalize_delay` = 2 s, as a **decision-snapshot policy**. The actual `finalized_at` is recorded and never backdated; late data never rewrites decisions; monitoring of arrival delay and corrections | **AGREED** (Q-SD-5) | SD-01 | Validated on arrival data from live shadow collection, for data quality only, never against P&L |
| D-04 | Eligibility matrix as a **separate deliverable and approval gate**: (A) source register, (B) conditional machine-readable matrix, (C) minute-bar adapter with an explicit VWAP policy, (D) combination and lifecycle logic, (E) acceptance evidence | **AGREED**; deliverable at **draft 2**: conditionally approved in principle (E-1/2/3/6/7 accepted, E-4(a) selected, E-5 as development-only assumptions); freeze checklist items 4, 7, 8, 9 open ([eligibility.md](eligibility.md)) | SD-03 | Blocks `features-v1`. Combination follows the source rules, with no universal AND shortcut |
| D-05 | `strategy_clock_origin = signal_bar_end`; deadlines = origin + n scheduled minutes, with the final slot evaluated first. Execution timers are separate | **AGREED** | SD-11, S-7 | Finalization delay and emission time never shift the strategy clock |
| D-06 | Timers and VWAP displacement are indexed by scheduled time; VWAP carries forward only through known no-trade slots; first displacement at slot 31 | **AGREED** | SD-01, SD-04 | An unknown gap marks VWAP `vwap_uncertain`. 10:00 would need an opening convention, and none is defined |
| D-07 | Last candidate close − 30; liquidation start close − 10; desired flat close − 5; residual position = incident | PROPOSED (Q-ALL-2) | SD-10 | An operating policy, not a consequence of near-dated options |
| D-08 | ATR resets each session; the fallback can't authorize entries; **09:45 global floor**, on top of each strategy's own window and readiness; no setup starts before slot 15 | **AGREED** (Q-SD-6) | SD-05, SD-10 | Slot 15 is the last OR bar; ORB's earliest is slot 16 |
| D-09 | Quotas consumed at candidate emission; retries reuse the ID; rejection doesn't refund | PROPOSED | SD-10 | |
| D-10 | VMR is RANGE-only | PROPOSED (Q-VMR-3) | vmr.md | NEUTRAL becomes a separate variant |
| D-11 | VMR `directional_invalidation_brake`: fires when INVALIDATION is among a reference exit's **triggered** reasons; affects the same direction for the rest of the session; resets daily; execution never affects it | **AGREED** | vmr.md | A different rule from "one losing trade", not an implementation of it |
| D-12 | ORB `rr_min` = 1.0, on remaining reward/risk at the signal close, with the denominator `r_u` to the invalidation level | **AGREED** (Q-ORB-3) | orb.md | The target is never moved to rescue an entry |
| D-13 | Single-linkage (transitive) clustering; zone versions `zv1` at the open and `zv2` at slot 15; setups bound to their version | DRAFT | SD-07 | |
| D-14 | `crosses_60` needs the full window, so RANGE exists from slot 60; equal values carry the previous side; no hysteresis | DRAFT | SD-09 | |
| D-15 | Order within a slot: anchors, then failure, then one stage, then trigger, then deadline. Exceptions: FBR may return on the excursion slot; VMR may trigger on the confirmation slot | DRAFT | SD-13, fbr.md, vmr.md | |
| D-16 | Milestones and underlying exits are close-based | PROPOSED | SD-11 | |
| D-17 | An underlying reference-position model (S-8) governs one-position, cooldown, quotas, brakes and timers. `reference_entry_price` is the signal close, a bookkeeping anchor, not a fill | **AGREED** | SD-11, S-8 | |
| D-18 | Anchors: VPC H may re-anchor in EXTENDED; VPC L is **never** moved (an undercut invalidates, see D-38); VMR new extreme → EXTENDED; FBR X frozen after the return; BRT RL updates, earliest low wins ties | **AGREED** (Q-VPC-3) | strategy files | |
| D-19 | BRT retest wick cap `retest_wick_max` = 1.0 `A_setup` below E, separate from close depth 0.5 A; a bar beyond the cap can't count as a retest or a trigger | **AGREED** (Q-BRT-3) | brt.md | An initial design choice, not validated |
| D-20 | FBR targets: VWAP, then the **nearest valid range-pair midpoint** from the zone's boundary-side members (never the zone's own midpoint), then `fallback_r` only when no structural target lies ahead. A too-close applicable target → `RR_TOO_LOW`. Provenance recorded | **AGREED** (Q-FBR-3) | fbr.md | |
| D-21 | ORB follow-through is post-entry: +0.5 `r_u` within 10 slots | PROPOSED | orb.md | v0.1's ORH + 0.25 W could already be met at entry |
| D-22 | ORB's R level is its invalidation level; the 0.1 A planned stop is removed | PROPOSED | orb.md | Removes the contradiction between the exit threshold and the R level |
| D-23 | `bullish`/`bearish` naming; RFC 8785 canonical JSON, SHA-256; `signal_key` separate from `candidate_id` | PROPOSED | schemas.md | |
| D-24 | A halt cancels setups in progress; positions are still managed | DRAFT | SD-10 | |
| D-25 | Bars built by us from trades; Alpaca bars are a diagnostic comparison | PROPOSED | SD-03 | |
| D-26 | VPC reconstructs the pullback between H's pivot and its confirmation | DRAFT | vpc.md | Not look-ahead, because action waits for confirmation |
| D-27 | Only strategies that use RVOL wait for it (VPC, ORB, BRT) | PROPOSED | SD-08 | |
| D-28 | Strategy-specific maximum holds with one common cutoff | PROPOSED (Q-ALL-3) | SD-10, SD-11 | A shared maximum hold would be a separate experiment |
| D-29 | One shared 15-minute OR; k = 2; one-minute cadence | PROPOSED (Q-SD-2, Q-SD-3, Q-SD-4) | SD-06, SD-07 | |
| D-30 | Regime thresholds kept: 1.5 / 0.75 / 80% / two crosses | **AGREED** (Q-SD-1) | SD-09 | Development checks look at operational behaviour only; any revision is a new variant |
| D-31 | Premium-loss stops, operational liquidation, operator flatten and broker-forced closes are **execution-only** exits. They're out of the reference precedence and never change strategy state | **AGREED** (owner point 8) | SD-11 | |
| D-32 | Interval status (`PRESENT`, `KNOWN_NO_ELIGIBLE_TRADES`, `DATA_MISSING_OR_UNCERTAIN`, `HALTED`). Entries only on `PRESENT`. Setups continue through known-no-trade slots and are cancelled on missing or halted data. Replay treats empty slots as known-no-trade unless the download flagged a gap | DRAFT | SD-01 | Replay can't prove feed health; that's a declared approximation |
| D-33 | The same reference model runs in paper; execution outcomes never write back to strategy state; divergences are recorded as evidence | **AGREED** (owner point 7) | SD-11, README | |
| D-34 | Execution cancels or rejects an unfilled entry once its reference position has exited; a late fill is exited immediately and logged | **AGREED** (owner point 4) | SD-10, SD-11 | |
| D-35 | Regime readiness per component with full windows only; slots 31–59 without a trend are **INCOMPLETE**, never NEUTRAL | **AGREED** | SD-09 | |
| D-36 | Unknown condition → trade quarantined; the symbol and slot become `DATA_MISSING_OR_UNCERTAIN` (all five SPY workers: entries blocked, setups cancelled). No resurrection on recovery; late discovery applies forward only; positions keep being managed; codes, slots, cancellations and late discoveries are monitored and alerted | **AGREED** | SD-03 | Correctness doesn't rely on unknown codes being rare |
| D-37 | Three clocks: strategy timers from `signal_bar_end`; expiry = `scheduled_finalization_at` (bar end + 2 s) + 60 s; telemetry = actual times. Submission needs `signal_available_at ≤ now < expires_at`. A late snapshot still emits the candidate and opens the reference position, with `submission_eligible = false` | **AGREED**, with a DRAFT detail | SD-10, S-7 | Replaces "close of the next bar". The detail (a late snapshot still opens the reference position) keeps strategy state independent of processing time |
| D-38 | VPC: before entry, any later bar with low < L invalidates the setup (wick-based, regardless of close). Replacement only through a new setup: pivot ≥ `undercut_slot`, confirmed by the current decision time, passing every existing rule, with a new `setup_id` and freshly frozen L and P. Structural failures and post-trade exits retire H. Mirrored for bearish | **AGREED** (**behavioural change**) | vpc.md | The waiting period is IDLE with a replacement context (drafter's detail, to keep it deterministic) |

## Kept as stated in the review

- Every strategy answer in review §§7–11: Q-BRT-1/2, Q-FBR-1/2, Q-ORB-1/2, Q-VMR-1/2/3 and
  Q-VPC-1/2.
- No new indicators and no parameter optimization in this revision. Numbers are unchanged from
  v0.1, except where a contradiction forced a change (CHANGELOG).
