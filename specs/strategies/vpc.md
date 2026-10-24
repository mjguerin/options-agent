# VPC: VWAP pullback continuation (BRD 6.2)

Status: **DRAFT v0.4, not approved.** Bullish shown. Bearish is the exact mirror image:
TREND_DOWN, swing lows ↔ swing highs, above ↔ below, max ↔ min. All thresholds use `A_setup`
unless stated otherwise.

**Hypothesis.** In a session trending up, a pullback toward VWAP that holds, followed by a break
of the pullback's high, offers a continuation entry. It's a research hypothesis, not a known edge.

## Eligibility (for starting a setup and for emitting a candidate)

- Slots 31 … last candidate (SD-10). Before slot 31, `disp_30_atr` isn't ready.
- Regime = TREND_UP (SD-09) on the evaluated slot.
- The common guards, one position, cooldown and quota (SD-10).
- Losing TREND_UP, or passing the last-candidate slot, while a setup is in progress **cancels** the
  setup (`REGIME_LOST`, `WINDOW_CLOSED`). Data-status slots follow SD-01: the setup continues
  through `KNOWN_NO_ELIGIBLE_TRADES` and is cancelled on missing or halted data. Cooldown and an
  open reference position can't occur during a setup, because setups only start when neither
  applies.

## State machine

| State | Entered when | Checks each slot, in this order | Leaves to |
|---|---|---|---|
| IDLE | Start of session, or after a setup ends | Is there a **qualifying H**? (below) | EXTENDED |
| EXTENDED | H selected; `A_setup` = `A_cur` of this slot | (1) Failure: close < VWAP − `pb_max_depth`·A → INVALIDATED `PULLBACK_TOO_DEEP`. (2) Age: slot > p_H + `setup_max_age` → INVALIDATED `SETUP_AGE`. (3) Re-anchor: a newly confirmed swing high above H with pivot after p_H replaces H (logged; `A_setup` unchanged). (4) Pullback: low ≤ VWAP + `pb_zone`·A | PULLBACK |
| PULLBACK | First pullback slot | (1) Failure as above. (2) Age as above. (3) Support: a swing low confirms with pivot q > p_H and low(q) ≥ VWAP(q) − `pb_max_depth`·A. In a **replacement setup** (below), q must also be ≥ the undercut slot of the setup it replaces | SUPPORTED |
| SUPPORTED | L = low(q), confirmed at c = q + 2. P = max high over q … c, frozen | (1) **Undercut:** any slot after c with low < L → INVALIDATED `SUPPORT_UNDERCUT`. L is never moved within a setup. (2) Failure and age checks as above. (3) Trigger (slot > c): close > P + `trig_buffer`·A, and rvol ≥ `rvol_min`. (4) Deadline: slot = c + `trig_window` with no trigger → INVALIDATED `TRIGGER_TIMEOUT` | CANDIDATE or IDLE |

**Qualifying H** on slot m: the **most recent** swing high confirmed at or before m whose pivot
p_H satisfies:
- p_H ≥ m − 59
- high(p_H) − VWAP(p_H) ≥ `ext_min` × A_cur(p_H). This uses the geometry at the pivot bar, known
  once the swing is confirmed.
- H is not **retired** (see Quotas and re-entry).

**Pullback window, reconstructed:** when EXTENDED is entered on slot m, the pullback test is also
applied to slots p_H + 1 … m. That uses only finalized data, and action still waits until slot m.

**Undercut rule (a substantive change, D-38).** Before entry, any slot after c with **low < L**
invalidates the current bullish setup, **regardless of its close**. This wick-based anchor
invalidation is separate from the close-based pullback-depth failure (`PULLBACK_TOO_DEEP`) and from
the close-based invalidation exit after entry. It reads a finalized bar value, not an intrabar
inference. Bearish mirror: any slot after c with high > L, where L is the bearish setup's swing-high anchor.

**Replacement setup.** Processed in this order:
1. Detect the undercut on slot u and invalidate the old setup (`SUPPORT_UNDERCUT`).
2. Record `undercut_slot` = u.
3. Consider only replacement pivots with `pivot_slot ≥ undercut_slot`. The undercut bar itself may
   become the pivot once it confirms; an older pivot can never be reused.
4. Wait for confirmation (`confirmed_at ≤ current decision time`) before building the replacement.

While waiting, the strategy is **IDLE with a replacement context** `{H, p_H, undercut_slot}`
(reason `AWAITING_REPLACEMENT_L`). Each slot, in order:
1. The context is dropped (and H retired) on any structural-failure condition: regime loss, a close
   below VWAP − `pb_max_depth`·A (A = the invalidated setup's `A_setup`), slot > p_H +
   `setup_max_age`, the window closing, or a data-status cancellation.
2. If a **newer** qualifying H appears, the context is dropped without retiring the old H, and a
   normal setup starts on the new H (most-recent-H rule).
3. If a swing low confirms with `pivot_slot ≥ undercut_slot` and passes the depth test, a
   replacement setup is built directly in **SUPPORTED**, since the pullback is already established.
   It gets a new `setup_id`, a new `A_setup` (A_cur of this slot), and newly frozen L, P and c.
   It must then pass the trigger window and the room check at its own trigger, like any setup.

**Confirmation-bar note:** P includes high(c), so close(c) can't exceed P + buffer. The earliest
possible trigger is slot c + 1.

## Candidate

On trigger slot t:

| Field | Value |
|---|---|
| `entry_reference` | close(t) |
| `invalidation_level` | L − `stop_buffer`·A_setup |
| `r_u` | entry − invalidation (must be > 0) |
| `target` | Fixed level high(p_H), frozen; it never moves with later highs |
| Room check | target − entry ≥ `rr_min` × `r_u`. Otherwise INVALIDATED `RR_TOO_LOW` (also when target ≤ entry) |
| `trail` | swing_trail, `trail_activate_r`, `stop_buffer` |

## Exits (SD-11)

INVALIDATION; TARGET; TRAIL; FOLLOW_THROUGH (`ft_r`, `ft_bars`); STAGNATION (`stag_bars`);
MAX_HOLD (`max_hold`).

## Quotas and re-entry

- At most `max_cand_dir` candidates per direction per session, consumed at emission. An
  untriggered replacement consumes nothing.
- How a setup ends decides whether its H can be used again (D-38):

| Category | Reason codes | Effect on H |
|---|---|---|
| **Anchor replacement** (before entry) | `SUPPORT_UNDERCUT` | H stays usable. A **replacement setup** may start against the same H with a new `setup_id`, a new `A_setup`, and a new L (pivot ≥ the undercut slot), with L and P frozen afresh. `setup_max_age` still counts from p_H |
| **Structural failure** (before entry) | `PULLBACK_TOO_DEEP`, `SETUP_AGE`, `REGIME_LOST`, `WINDOW_CLOSED`, `TRIGGER_TIMEOUT`, `RR_TOO_LOW`, data-status cancellation | H is **retired**. The next attempt needs a fresh qualifying H, strictly above every retired H |
| **Post-trade** | Any reference exit after a candidate | H is **retired**; same as above. Execution outcomes (rejection, premium stop) don't matter: only the reference position counts (SD-11) |

## Parameters

| Name | Value | Unit | Rationale (a hypothesis, not a result) |
|---|---|---|---|
| `ext_min` | 2.0 | A | Initial hypothesis for "a real push away from VWAP" |
| `pb_zone` | 1.0 | A | "Near VWAP" without demanding an exact touch |
| `pb_max_depth` | 0.5 | A | Tolerates a small undercut |
| `setup_max_age` | 60 | slots from p_H | Bounds the setup |
| `trig_buffer` | 0.05 | A | Avoids equal-high triggers |
| `trig_window` | 15 | slots from c | |
| `rvol_min` | 1.0 | × | |
| `rr_min` | 1.0 | × `r_u` | |
| `stop_buffer` | 0.25 | A | |
| `trail_activate_r` | 1.0 | × `r_u` | |
| `ft_r` / `ft_bars` | 0.5 / 10 | × `r_u` / slots | |
| `stag_bars` | 15 | slots | |
| `max_hold` | 45 | slots | |
| `max_cand_dir` | 2 | per session | |

## Answered (proposed)

- Q-VPC-1: keep H as a fixed target; trail-only is a separate variant.
- Q-VPC-2: TREND_UP required; extension-only is a separate variant.

## Open

None. Q-VPC-3 is settled: an undercut invalidates the setup; replacement happens only through a
new setup (D-38).
