# ORB: Opening-range breakout (BRD 6.3)

Status: **DRAFT v0.4, not approved.** Bullish shown; bearish mirrors it on ORL. Thresholds use
`A_OR` = A_cur(15), frozen for the day.

**Hypothesis.** A first close out of the 15-minute opening range, on unusual volume, tends to
continue. It's a research hypothesis, not a known edge.

## Daily setup (at `finalized_at(15)`, 09:45:02 on a full day)

- The OR must be complete (SD-07); otherwise INELIGIBLE for the day (`OR_INCOMPLETE`).
- W = ORH − ORL. `W_A` = W / `A_OR`, frozen for the whole session. A later ATR change doesn't
  change daily eligibility.
- Range filter: `w_min` ≤ `W_A` ≤ `w_max`. Otherwise INELIGIBLE for the day (`OR_TOO_NARROW` or
  `OR_TOO_WIDE`).
- The filter's intent is a **testable hypothesis**: that very narrow ranges produce more false
  breaks and very wide ranges less follow-through. The 10–90% quantiles only describe the data;
  they don't establish either effect.

## State machine (per direction)

| State | Checks each slot | Leaves to |
|---|---|---|
| ARMED (WATCHING) | Slots 16 … `last_entry_slot`. **Crossing:** close(m) > ORH + `brk_buffer`·A_OR and close(m − 1) ≤ ORH + `brk_buffer`·A_OR. **Confirmation on the same slot:** rvol(m) ≥ `rvol_min`, high > low, and close ≥ (high + low) / 2. A zero-range bar fails confirmation | CANDIDATE, or stays ARMED |
| DONE | The direction's quota is used, or the slot is past `last_entry_slot` | |

- A crossing whose confirmation fails produces nothing. A **new crossing** requires a close back
  at or below the threshold first; that's inherent in the crossing rule. Confirmation on a later
  bar is **not** allowed.
- On slot 16, the previous close is slot 15's, which is ≤ ORH by definition. Slot 15 is the last
  OR bar, so it can never be a breakout bar; the earliest ORB candidate is slot 16 (09:46:02).
- **Purpose of `rr_min`:** the target is anchored to ORH, while a late entry can sit well beyond
  the threshold. The filter keeps a signal from risking more than the room left to its target.
  It's an admission constraint, not a claim that 1.0 is optimal.

## Candidate

| Field | Value |
|---|---|
| `entry_reference` | close(t) |
| `invalidation_level` | ORH − `inv_frac` × W. A close at or below it exits. This is also the R level; the separate 0.1 A planned stop from v0.1 is removed |
| `r_u` | entry − invalidation |
| `target` | Fixed level ORH + `tgt_mult` × W |
| Room check | Requires `r_u` > 0, target > entry (`TARGET_EXHAUSTED` otherwise), and remaining reward/risk `RR_remaining = (target − entry) / r_u` ≥ `rr_min` = 1.0, measured at the signal close (`RR_TOO_LOW` otherwise). The denominator is the single approved initial risk, to the invalidation level (D-22). The target is never moved to rescue a rejected entry (D-12) |
| `trail` | swing_trail, `trail_activate_r`, buffer `stop_buffer` (SD-11) |

## Exits (SD-11)

INVALIDATION; TARGET; TRAIL; FOLLOW_THROUGH: a close ≥ entry + `ft_r` × `r_u` within `ft_bars`.
That's post-entry progress, replacing v0.1's "ORH + 0.25 W", which could already be satisfied at
entry. Then MAX_HOLD.

## Quotas

- One candidate per direction per session, consumed at emission (rejection doesn't refund it).
- After a bullish trade or rejection, a bearish crossing of ORL is still allowed.

## Parameters

| Name | Value | Unit |
|---|---|---|
| `w_min` / `w_max` | 3.0 / 10.0 | `A_OR` |
| `last_entry_slot` | 120 (bar ending 11:30) | slot |
| `brk_buffer` | 0.10 | `A_OR` |
| `rvol_min` | 1.5 | × |
| `inv_frac` | 0.25 | × W |
| `tgt_mult` | 1.0 | × W |
| `rr_min` | 1.0 | × `r_u` |
| `trail_activate_r` | 1.0 | × `r_u` |
| `stop_buffer` | 0.25 | A_setup (= A_cur at the trigger) |
| `ft_r` / `ft_bars` | 0.5 / 10 | × `r_u` / slots |
| `max_hold` | 60 | slots |

## Answered (proposed)

- Q-ORB-1: 15 minutes for v1.
- Q-ORB-2: no extra confirmation bar.

## Open

None. Q-ORB-3 is settled: `rr_min` = 1.0 (D-12).
