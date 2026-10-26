# VMR: VWAP mean reversion (BRD 6.6)

Status: **APPROVED v1 (owner, 2026-10-25), tag `specs-v1`.** Bearish shown (price stretched above VWAP); bullish mirrors
it. Thresholds use `A_setup` = A_cur at the episode start, except the stretch measure d, which
uses A_cur of the slot it's computed on.

**Hypothesis.** In a range-bound session, price stretched unusually far from VWAP tends to revert
partway toward it once the stretch shows a confirmed turn. It's a research hypothesis, not a known
edge.

## Eligibility

- Slots 60 … `last_entry_slot`. RANGE first exists at slot 60 (SD-09).
- Regime = **RANGE only** (D-10). NEUTRAL is a separate, broader variant.
- Leaving RANGE during a setup, for TREND or NEUTRAL alike, **cancels** the setup (INVALIDATED
  `REGIME_EXCLUDED`). Setups aren't suspended and revived.
- **`directional_invalidation_brake`** (D-11):
  - **Fires** when a VMR reference position exits and INVALIDATION is among the exit's
    **triggered** reasons, whichever reason was primary. For example, INVALIDATION together with
    SESSION_CUTOFF on one slot still fires it.
  - **Effect:** no new VMR candidates in that `underlying_direction` for the rest of the session.
  - It applies only after a reference position has opened. A pre-entry INVALIDATED setup doesn't
    fire it.
  - It resets at the next session.
  - Option-account P&L, premium stops and other execution exits neither fire it nor reset it.
  - This is a **different rule** from v0.1's "one losing trade", not a reimplementation: an option
    trade can lose without an underlying invalidation, and the reverse.
- The common guards, one position, cooldown and quota.

## Stretch

d(m) = (close(m) − VWAP(m)) / A_cur(m).

## State machine

| State | Checks each slot, in this order | Leaves to |
|---|---|---|
| IDLE | **Episode start:** d(m) ≥ `ext_min` and d(m − 1) < `ext_min`. `A_setup` = A_cur(m); `ep_start` = m | EXTENDED |
| EXTENDED | (1) Regime: not RANGE → INVALIDATED `REGIME_EXCLUDED`. (2) Age: slot > `ep_start` + `ep_max_age` → INVALIDATED `SETUP_AGE`. (3) Turn: a swing high confirms on this slot with pivot s ≥ `ep_start` and d(s) ≥ `ext_min` − `exh_tol` → EXHAUSTED, with S = high(s), `S_low` = low(s), c = s + k | EXHAUSTED |
| EXHAUSTED | Slots c … c + `trig_window`, **including the confirmation slot c**, because everything is known there. (1) Regime and age as above. (2) New extreme: high > S → back to EXTENDED (same episode, S cleared). A breached anchor is never used to emit. (3) Trigger: close < `S_low`. (4) Deadline at c + `trig_window` → INVALIDATED `TRIGGER_TIMEOUT` | CANDIDATE, EXTENDED or IDLE |

- On the confirmation slot c, the turn check and the trigger check both run. That's an explicit
  exception to the one-stage-per-slot default (SD-13).

## Candidate

| Field | Value |
|---|---|
| `entry_reference` | close(t) |
| `invalidation_level` | S + `stop_buffer`·A |
| `r_u` | invalidation − entry (> 0) |
| `vwap_sig` | VWAP(t), frozen |
| `target` | Fixed level: `vwap_sig` + (1 − `tgt_frac`) × (entry − `vwap_sig`), i.e. `tgt_frac` of the way to the signal-time VWAP |
| Room check | entry > `vwap_sig` (`NO_ROOM` otherwise), and entry − target ≥ `rr_min` × `r_u` (`RR_TOO_LOW`) |

## Exits (SD-11)

INVALIDATION; TARGET; FOLLOW_THROUGH (`ft_r`, `ft_bars`); MAX_HOLD. No trailing stop.

## Quotas

- At most `max_cand_dir` per direction per session (consumed at emission), and subject to the
  `directional_invalidation_brake` above.

## Parameters

| Name | Value | Unit | Rationale (a hypothesis, not a result) |
|---|---|---|---|
| `ext_min` | 6.0 | A | Initial guess for "unusually stretched". Not derived from the descriptive table |
| `exh_tol` | 1.0 | A | The turn may confirm slightly inside the threshold |
| `ep_max_age` | 30 | slots | |
| `trig_window` | 5 | slots from c | |
| `stop_buffer` | 0.5 | A | |
| `tgt_frac` | 0.75 | of entry→VWAP | Design choice for partial reversion; not an established result |
| `rr_min` | 1.0 | × `r_u` | |
| `last_entry_slot` | 330 | slot | |
| `ft_r` / `ft_bars` | 0.5 / 8 | | |
| `max_hold` | 30 | slots | |
| `max_cand_dir` | 2 | per session | |

## Answered (proposed)

- Q-VMR-1: ATR normalization for v1.
- Q-VMR-2: target fixed at signal time.
- Q-VMR-3: RANGE only.

## Open

None. Q-VMR-4 is settled by D-11: the underlying-only `directional_invalidation_brake`, firing
on any triggered INVALIDATION.
