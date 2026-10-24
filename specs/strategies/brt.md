# BRT: Break-and-retest continuation (BRD 6.4)

Status: **DRAFT v0.4, not approved.** Bullish shown (an upside break of a zone's `zone_high`, E);
bearish mirrors it on `zone_low`. Thresholds use `A_setup` = A_cur at the break slot.

**Hypothesis.** When a known level breaks and then holds on a retest, it tends to act as support
for a continuation. It's a research hypothesis, not a known edge.

## Levels and eligibility

- Zones from SD-07: PDH, PDL, PMH, PML, ORH and ORL. VWAP is not a level in v1. A setup binds to
  the zone version current at its break slot.
- Slots 15 … last candidate. The common guards, one position, cooldown and quota.
- Levels stay available all session (Q-BRT-2); setups expire through their own windows.
- One setup per zone edge per direction at a time.

## State machine

| State | Checks each slot, in this order | Leaves to |
|---|---|---|
| IDLE | **Break:** close(m) > E + `brk_min`·A_cur(m), close(m − 1) ≤ E + `brk_min`·A_cur(m) (a crossing from below; a close above a level broken an hour ago isn't a new break), and rvol(m) ≥ `rvol_min`. `A_setup` = A_cur(m) | BROKEN |
| BROKEN | Slots after the break; the break bar can't be the retest. (1) Failure: close < E − `retest_depth`·A → INVALIDATED `RETEST_FAILED`; **or** low < E − `retest_wick_max`·A → INVALIDATED `RETEST_TOO_DEEP`. (2) Runaway: high ≥ E + `chase_max`·A → INVALIDATED `NO_RETEST`. **If runaway and retest happen on the same slot, runaway wins**, since the intrabar order is unknown. (3) Retest: low ≤ E + `retest_tol`·A → RETESTING, with r0 = this slot. (4) Deadline: slot = break + `retest_window` → INVALIDATED `NO_RETEST` | RETESTING or IDLE |
| RETESTING | (1) Anchor: if low < RL, then RL = low and `RL_bar` = this slot; on equal lows the **earlier** bar stays. RH = high(`RL_bar`). (2) Failure: close < E − `retest_depth`·A → `RETEST_FAILED`, or low < E − `retest_wick_max`·A → `RETEST_TOO_DEEP` (both INVALIDATED). (3) Trigger: slot > `RL_bar` and close > RH. (4) Deadline: slot = r0 + `hold_window`, **fixed from the first retest** and not reset by a new RL → INVALIDATED `HOLD_TIMEOUT` | CANDIDATE or IDLE |

- **Two penetration limits** (Q-BRT-3, D-19), both measured from the frozen edge E with
  `A_setup`:
  - Closes may go up to `retest_depth` (0.5 A) below E.
  - Wicks may go up to `retest_wick_max` (1.0 A) below E.
  - A deeper wick invalidates the setup even if the bar closes back above.
  - Failure checks run before the retest and trigger checks, so a bar beyond either limit can't
    also count as a retest or a trigger.
  - This separates tolerated probing from a materially different rejection. 1.0 A is an initial
    design choice, not validated.
- A slot that sets a new RL can't trigger: its close ≤ its high = RH. The earliest trigger is
  the next slot.

## Candidate

| Field | Value |
|---|---|
| `entry_reference` | close(t) |
| `invalidation_level` | RL − `stop_buffer`·A |
| `r_u` | entry − invalidation (> 0) |
| Obstacle | The nearest `zone_low` strictly above entry, in the bound zone version |
| `target` | If an obstacle exists within `next_level_max`·A of entry, target = that obstacle. A too-close obstacle is **never skipped**. Otherwise (no obstacle, or beyond `next_level_max`·A), target = entry + `fallback_r` × `r_u` |
| Room check | target − entry ≥ `rr_min` × `r_u`; otherwise INVALIDATED `RR_TOO_LOW` |

## Exits (SD-11)

INVALIDATION; TARGET; TRAIL (`trail_activate_r`); FOLLOW_THROUGH (`ft_r`, `ft_bars`); MAX_HOLD.

## Quotas

- Each zone edge yields at most one candidate per direction per session, consumed at emission.
- A failed or expired setup doesn't consume the edge. A **new crossing** of the same edge may
  start a new setup.

## Parameters

| Name | Value | Unit |
|---|---|---|
| `brk_min` | 0.25 | A |
| `rvol_min` | 1.2 | × |
| `chase_max` | 4.0 | A |
| `retest_window` | 30 | slots from the break |
| `retest_tol` | 0.5 | A |
| `retest_depth` | 0.5 | A |
| `retest_wick_max` | 1.0 | A |
| `hold_window` | 10 | slots from r0 |
| `stop_buffer` | 0.25 | A |
| `next_level_max` | 6.0 | A |
| `fallback_r` | 2.0 | × `r_u` |
| `rr_min` | 1.2 | × `r_u` |
| `trail_activate_r` | 1.0 | × `r_u` |
| `ft_r` / `ft_bars` | 0.5 / 10 | |
| `max_hold` | 45 | slots |

## Answered (proposed)

- Q-BRT-1: PDH, PDL, PMH, PML, ORH and ORL only; no VWAP.
- Q-BRT-2: levels last all session; there's no noon age-out.

## Open

None. Q-BRT-3 is settled: wicks are capped at `retest_wick_max` = 1.0 A (D-19).
