# FBR: Failed-breakout reversal (BRD 6.5)

Status: **DRAFT v0.4, not approved.** Bearish shown (a failed upside excursion through a zone's
`zone_high`, E); bullish mirrors it on `zone_low`. Thresholds use `A_setup` = A_cur at the first
excursion slot.

**Hypothesis.** An excursion through a known reference level that's quickly rejected tends to
unwind back toward the interior. v1 tests **rejection at a reference level**: the level universe
defines levels, not complete ranges, so "back into the range" is approximated by the midpoint pairs
of SD-07. It's a research hypothesis, not a known edge.

## Eligibility

- Slots 31 … `last_entry_slot`. FBR waits until the trend filter is ready (`disp_30_atr` exists at
  31); the v0.1 start of 09:50 is gone.
- **Trend block:** no bearish setup while `disp_30_atr` ≥ `trend_block`; no bullish setup while it's
  ≤ −`trend_block`. If the block appears **during** a setup, the setup is cancelled
  (INVALIDATED `REGIME_STRONG_TREND`).
- The common guards, one position, cooldown and quota.

## State machine

| State | Checks each slot, in this order | Leaves to |
|---|---|---|
| IDLE | **Episode start:** high(m) ≥ E + `exc_min`·A_cur(m) and close(m − 1) ≤ E (price approached from inside). If high(m) ≥ E + `exc_max`·A on that same slot, no setup starts (`EXCURSION_TOO_FAR`). `A_setup` = A_cur(m); X = high(m) | EXCURSION, and the return check runs on this same slot (below) |
| EXCURSION | Slots m … m + `return_window`, including m itself. (1) X = max(X, high). (2) Too far: X ≥ E + `exc_max`·A → INVALIDATED `EXCURSION_TOO_FAR`. This takes precedence even if the same bar returns inside. (3) Return: close < E − `return_buffer`·A → RETURNED, with `ret_bar` = this slot, `RetLow` = low(`ret_bar`), and X frozen. (4) Deadline: slot = m + `return_window` → INVALIDATED `NO_RETURN` | RETURNED or IDLE |
| RETURNED | Slots `ret_bar` + 1 … `ret_bar` + `trig_window`. (1) Reclaim: close > E → INVALIDATED `RECLAIMED`. (2) Trigger: close < `RetLow`. (3) Deadline at `ret_bar` + `trig_window` → INVALIDATED `TRIGGER_TIMEOUT` | CANDIDATE or IDLE |

- **Same-slot return is allowed.** The excursion bar can close back inside, because a close below
  the bar's high means the high came first (SD-01).
- **X is frozen at the return.** A later high above X that doesn't close above E keeps the setup
  alive, and the frozen invalidation doesn't widen. A close above E has already reclaimed.
- The return bar can't be its own trigger (close(`ret_bar`) ≥ `RetLow`); the earliest trigger is
  `ret_bar` + 1.

## Candidate

| Field | Value |
|---|---|
| `entry_reference` | close(t) |
| `invalidation_level` | X + `stop_buffer`·A |
| `r_u` | invalidation − entry (> 0) |

**Target, a declared hierarchy** (bearish; "valid" means strictly below entry):
1. VWAP(t), if valid. Frozen at signal time.
2. Otherwise, a **range-pair midpoint**, never the clustered zone's own midpoint, which describes
   the obstacle rather than the range interior (D-20):
   1. Take every **upper-boundary** member in E's zone (PDH, PMH, ORH; for a bullish trade, the
      lower-boundary members PDL, PML, ORL). Keep each member's range-pair identity through the
      clustering.
   2. Each yields a candidate midpoint from its pair (SD-07): (PDH + PDL) / 2, (PMH + PML) / 2,
      (ORH + ORL) / 2. A member whose pair half is absent yields none.
   3. Drop any candidate that isn't strictly below entry.
   4. Choose the **nearest** remaining candidate to entry. There's no fixed priority such as
      "OR beats prior day".
3. Otherwise, **no structural reference lies ahead**, so target = entry − `fallback_r` × `r_u`.

The room check applies to the **chosen** target: entry − target ≥ `rr_min` × `r_u`. If an
applicable structural target (step 1 or 2) exists but is too close, it's INVALIDATED `RR_TOO_LOW`.
A too-close applicable target is **not** the same as having no target: it never falls through to
a farther midpoint or to `fallback_r`. Choosing VWAP first is a design choice, not evidence that it's
better.

The candidate records `target_source` (`vwap`, `range_midpoint` or `fallback_r`),
`range_pair_id` (for example `OR`, `PD`, `PM`; null otherwise), `zone_version` and `target_price`.

## Exits (SD-11)

INVALIDATION; TARGET; FOLLOW_THROUGH (`ft_r`, `ft_bars`); STAGNATION (`stag_bars`, no new lowest
close); MAX_HOLD. No trailing stop.

## Quotas

- Each zone edge yields one candidate per direction per session.
- At most `max_cand_session` FBR candidates per session in total. Both are consumed at emission.

## Parameters

| Name | Value | Unit |
|---|---|---|
| `exc_min` | 0.5 | A |
| `exc_max` | 4.0 | A |
| `return_window` | 15 | slots from m (inclusive) |
| `return_buffer` | 0.1 | A |
| `trig_window` | 5 | slots after `ret_bar` |
| `trend_block` | 2.0 | `disp_30_atr` (dimensionless) |
| `stop_buffer` | 0.25 | A |
| `rr_min` | 1.0 | × `r_u` |
| `fallback_r` | 1.5 | × `r_u` |
| `last_entry_slot` | 330 (bar ending 15:00) | slot |
| `ft_r` / `ft_bars` | 0.5 / 10 | |
| `stag_bars` | 15 | slots |
| `max_hold` | 45 | slots |
| `max_cand_session` | 2 | per session |

## Answered (proposed)

- Q-FBR-1: break of the return bar's low for v1; the failed-reclaim trigger is a separate variant.
- Q-FBR-2: a declared hierarchy with VWAP first, only when directionally valid.

## Open

None. Q-FBR-3 is settled: nearest valid range-pair midpoint, chosen from the zone's
boundary-side members (D-20).
