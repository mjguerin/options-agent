# Specs changelog

## v1 approval edits (Oct 25, 2026)

Owner decisions on the approval checklist; no parameter changes.
- Drafter choices D-13 to D-15, D-24, D-26, D-32, D-37/D-38 details and D-39 to D-44 approved.
- D-02 dates: development 2024-02-01 to 2024-12-31, held-out 2025-01-02 to 2026-10-22.
- D-04 item 9 closed by option (b): v1 frozen with the source gaps documented.
- PROPOSED items D-07, D-09, D-10, D-16, D-22, D-23, D-25, D-27 to D-29 confirmed, with the owner's
  clarifications written into SD-10, SD-11, S-3 and orb.md.
- D-21 reworded: ORB follow-through is signal-anchored (from `reference_entry_price` and
  `signal_bar_end`), not "post-entry". The implementation already worked this way; acceptance
  scenario SH-35b added.
- SD-02 (D-46): a calendar problem disables new entries but never position management; the
  contradiction tests are defined. SD-03 and SD-04 (D-47): uncertainty scope and VWAP wording
  completed.

## v0.4 (Oct 23, 2026): owner's provisional decisions on the open items

These are draft implementation defaults, frozen for `v1` after the development correctness
checks. They are not optimized parameters. Numeric values are unchanged except ORB `rr_min` and
the new BRT `retest_wick_max`.

### Behavioural changes

- **Finalization (D-03).** The 2 s delay is a decision-snapshot policy. `available_at` is now the
  **actual** finalization time and is never backdated. Monitoring of arrival delay and corrections
  is added.
- **Candidate expiry (D-37).** It's now `scheduled_finalization_at` + 60 s, replacing "close of the
  next bar". Submission needs `signal_available_at ≤ now < expires_at`. A late snapshot still emits
  the candidate and opens the reference position, with `submission_eligible = false`.
- **09:45 global floor (D-08).** Each strategy's own window and readiness applies on top. No setup
  starts before slot 15.
- **Regime (D-35).** New INCOMPLETE value for slots 31–59 without a trend; missing history is never
  NEUTRAL.
- **Unknown sale conditions (D-36).** The trade is quarantined and the slot becomes
  `DATA_MISSING_OR_UNCERTAIN` for all five workers. No resurrection on recovery; late discovery
  applies forward only; positions keep being managed.
- **Eligibility matrix (D-04).** Multiple conditions now combine per the source rules (the
  universal AND rule is removed). The matrix is a separate deliverable and blocks `features-v1`.
- **ORB (D-12).** `rr_min` goes from 0.0 to **1.0**, on remaining reward/risk at the signal close.
- **VPC (D-38).** A **substantive strategy change**: before entry, any later bar with low < L
  invalidates the setup (`SUPPORT_UNDERCUT`), regardless of its close. v0.2/v0.3 instead moved L
  down, or used a close-based `SUPPORT_BROKEN`. Replacement happens only through a new setup
  pivoting at or after the undercut slot, with IDLE plus a replacement context while waiting. Setup
  ends are now categorized: anchor replacement keeps H usable; structural and post-trade ends
  retire it.
- **BRT (D-19).** New wick cap: `retest_wick_max` = 1.0 A (`RETEST_TOO_DEEP`).
- **FBR (D-20).** The midpoint target is now the nearest valid **range-pair** midpoint among the
  zone's boundary-side members, replacing the edge-defining member's pair. Target provenance is
  recorded.

### Editorial changes

- The SD-09 regime thresholds are confirmed as hypotheses (D-30), with the development checks
  listed.
- README status describes these as provisional defaults.

## v0.3 (Oct 23, 2026): owner's response to the v0.2 challenges

The owner accepted D-01, D-02, D-05, D-06, D-11 and D-17 (now AGREED), with refinements.
Numeric parameters are unchanged.

### Behavioural changes

- **Two layers (D-33).** The reference-position model is now the strategy state machine in paper
  as well as replay. v0.2 let premium stops and operator actions end a reference position through
  `position_closed`, and counted working entry orders toward the one-position rule. Both are
  **removed**: execution never changes strategy state.
- **Premium-loss stop leaves the reference precedence (D-31).** It's now an execution-only exit,
  along with operational liquidation, operator flatten and broker-forced closes.
- **Interval status (D-32).** Every slot is `PRESENT`, `KNOWN_NO_ELIGIBLE_TRADES`,
  `DATA_MISSING_OR_UNCERTAIN` or `HALTED`.
  - Entries happen only on `PRESENT` slots.
  - Setups continue through known-no-trade slots and are cancelled on missing or halted data.
  - VWAP carries forward only through known-no-trade slots; an unknown gap marks it
    `vwap_uncertain`, which blocks entries.
- **Reference exits without a close.** INVALIDATION, TARGET and TRAIL can't fire on a slot without
  a close; scheduled exits still do. A missing exit-slot close uses the last valid close, flagged
  `stale_reference_price`.
- **Strategy clock (D-05).** The origin is `signal_bar_end`, and the final slot of a timer is
  evaluated for progress before the deadline fails.
- **VMR brake (D-11).** Renamed `directional_invalidation_brake`. It fires when INVALIDATION is
  among the **triggered** reasons, whatever the primary reason; it applies only after a reference
  position opened; it resets daily; execution never touches it.
- **Execution follows the reference (D-34).** An unfilled entry is cancelled or rejected once its
  reference position has exited.

### Editorial changes

- Scope wording for the five candidates, plus "do not trade" as an analytical reference (D-01).
- Data partitions: purpose table; the held-out report's permitted consequences; replay validates
  the implementation and doesn't select (D-02).
- New ReferencePosition schema (S-8). Candidates gain `signal_bar_end`; transitions can no longer be
  caused by broker or operator events.
- The SD-04 opening-boundary rationale for starting displacement at slot 31.

## v0.2 (Oct 23, 2026): revision from the owner's consolidated review

Numeric parameter values are unchanged from v0.1 unless listed under behavioural changes. New
parameter names (`setup_max_age`, `return_buffer`, `next_level_max`, `fallback_r`, `exh_tol`,
`ep_max_age`, `ft_r`, `trail_activate_r`, `rr_min` for ORB, and the common deadlines) give names to
constants that were literal in v0.1 or implicit, at the same values where v0.1 had one.

### Behavioural changes

Each would change the bars, features or candidates.

**Shared**
- **Bars:** per-field trade eligibility (OHLC, volume, VWAP). Pre-market Form T trades now count
  for PMH and PML. *(v0.1 bug: they excluded every pre-market trade.)* Unknown conditions are
  excluded. Empty and volume-only slots are never forward-filled.
- **Finalization:** decisions at `bar_end` + 2 s, instead of at `bar_end`.
- **VWAP displacement:** `disp_30_atr` is first available at slot 31 (10:01), not 10:00. Its
  thresholds are dimensionless; the values are unchanged.
- **ATR:** explicit session reset (initialized at bar 14, Wilder recurrence after that). The prior
  session's fallback can no longer authorize entries.
- **Earliest candidate:** slot 15 (09:45) for everyone, replacing 09:35.
- **Regime:** RANGE needs a full 60-slot window, so it exists from 10:30. Added a minimum number of
  valid closes for occupancy, and an equality rule for sides.
- **Levels:** single-linkage clusters with two zone versions, each setup bound to its version. The
  OR uses scheduled slots 1–15 and must be complete. PDC is out of the zones.
- **RVOL:** the baseline excludes early closes. Ready after 20 reference sessions.
- **Session end:** split into liquidation start (close − 10), desired flat (close − 5) and
  residual-position incident, replacing a single "flat by 15:50".
- **Quotas:** consumed at emission; a rejection doesn't refund them.
- **Exits:** close-based milestones, monotonic trailing on post-entry swings only, and every
  triggered reason recorded alongside the primary one.
- **Timers:** start at the signal slot.
- **Setups:** cancelled on a halt, and on ineligibility by default.
- **Replay:** simulates an underlying reference position.

**VPC**
- H is the most recent qualifying swing, with its geometry taken at the pivot.
- Re-anchoring is allowed: H in EXTENDED, L in SUPPORTED.
- The pullback is reconstructed from H's pivot.
- P is frozen; the earliest trigger is c + 1.
- New `setup_max_age` of 60 slots.
- A new setup needs a strictly higher H after any setup end.
- Losing the regime cancels the setup.
- A target at or below entry is rejected.

**ORB**
- `W_A` is frozen at slot 15.
- A crossing is required, and confirmation is allowed only on the crossing bar; a zero-range bar
  fails confirmation.
- R is measured to the invalidation level (the 0.1 A planned stop is removed).
- An entry at or beyond the target is rejected.
- Follow-through is now +0.5 `r_u` within 10 slots, replacing ORH + 0.25 W.

**BRT**
- A crossing from below is required.
- The break bar can't be the retest.
- Runaway beats retest on the same bar.
- RL anchor and tie rules.
- The hold window is fixed from the first retest.
- An obstacle is never skipped.
- The 2R fallback applies when there's no obstacle, or the nearest is beyond 6 A.

**FBR**
- Starts at slot 31 instead of 09:50.
- The trend block is dimensionless and also cancels setups in progress.
- An episode needs an approach from inside.
- `exc_max` takes precedence over a return.
- X is frozen after the return.
- A same-slot return is allowed; the earliest trigger is the return bar + 1.
- Target hierarchy with explicit midpoint pairs; a too-close target is rejected rather than
  replaced by 1.5R.

**VMR**
- RANGE only (v0.1 allowed RANGE or NEUTRAL); starts at slot 60 instead of 10:00.
- An episode needs a crossing of the stretch threshold; new episode age limit of 30 slots.
- Any excluded regime cancels the setup.
- A new extreme sends the setup back to EXTENDED.
- A trigger is allowed on the confirmation slot.
- The brake fires on an underlying invalidation exit instead of "one losing trade".

### Editorial changes

None of these change behaviour.
- "Nothing here is tuned" is replaced by an exact provenance statement and the research access log.
- Unit wording: A doesn't imply equivalence across regimes; `r_u` is named and frozen; slots aren't
  seconds.
- Descriptive statistics now state populations and methods; the script is in the repo.
- Rationales are softened to hypotheses: ORB range filter, VMR `ext_min`, VMR `tgt_frac`, VPC
  `ext_min`.
- Schemas restructured into two envelopes. Added StrategyEvaluation; source is separated from
  execution mode; event identity, revision and integrity are separate; canonical hashing;
  `bullish`/`bearish`; typed targets and timers; downstream records are separate.
- The "never hard-code a threshold" wording is replaced (SD-14).
- New supporting documents: decision register, acceptance tests, consistency check, access log.

## v0.1 (Oct 23, 2026)

First draft.
