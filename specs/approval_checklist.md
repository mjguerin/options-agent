# Specs v1 approval checklist (Step 2 gate: "specs approved and tagged")

The other two gate conditions, no look-ahead and traceability, are evidenced by the gate run
(`research/replay/step2_gate.py`). This list is what the owner decides before the specs are
frozen as `specs-v1`. Nothing here asks for parameter tuning: every number stays as written.

## 1. Owner decisions

| # | Item | Options |
|---|---|---|
| 1 | **D-02 data partition dates** | **Decided 2026-10-25:** development 2024-02-01 to 2024-12-31; held-out 2025-01-02 to 2026-10-22 |
| 2 | **D-04 freeze item 9 (OPEN-SRC-1 to 4)** | **Decided 2026-10-25:** option (b), freeze with the gaps documented; `O`/`N` assumptions are development-only and fall inside the development partition |
| 3 | **PROPOSED items from the owner's review** (D-07, D-09, D-10, D-16, D-21, D-22, D-23, D-25, D-27, D-28, D-29) | **Decided 2026-10-25:** all confirmed; D-21 reworded as signal-anchored. Clarifications are written into the specs |
| 4 | **D-46 detail: calendar contradiction tests** (SD-02) | **Decided 2026-10-25:** approved as drafted |

## 2. Drafter choices: approved

The owner approved all of these on 2026-10-25, and they're marked AGREED in [decisions.md](decisions.md).

| ID | Choice |
|---|---|
| D-13 | Single-linkage zones; `zv1` at the open, `zv2` at slot 15; setups bound to their version |
| D-14 | RANGE needs the full 60-slot cross window; no hysteresis |
| D-15 | Order within a slot, with two named exceptions (FBR same-slot return, VMR confirmation-slot trigger) |
| D-24 | A halt cancels setups; positions are still managed |
| D-26 | VPC reconstructs the pullback between H's pivot and its confirmation |
| D-32 | Interval statuses; setups continue through known no-trade slots and are cancelled on missing or halted data |
| D-37 (detail) | A late snapshot still opens the reference position, with `submission_eligible = false` |
| D-38 (detail) | VPC waiting state is IDLE with a replacement context |
| D-39 | Bearish mirrors by orientation |
| D-40 | Other setups cancelled with `POSITION_OPENED` when one emits |
| D-41 | Same-slot trigger preemption: earliest setup first, bullish on ties |
| D-42 | "Previous close" = most recent valid close |
| D-43 | BRT/FBR edge identity across zone versions |
| D-44 | VPC replacement depth test uses the new `A_setup` |

## 3. Shared definitions (project plan Step 2): approved 2026-10-25 with the owner's wording (D-46, D-47)

- **Bars:** SD-03 together with the D-04 matrix and adapter (E-1, E-2, E-3, E-7).
- **VWAP:** last-eligible session VWAP, E-4(a).
- **Session calendar:** SD-02, with Alpaca's calendar as the selected provider calendar.

## 4. On approval, the drafter will

1. Change every spec status line from `DRAFT v0.4` to `APPROVED v1`. Change config versions from
   `*-draft` to `*-v1` (features, common, five strategies, eligibility adapter), which changes their
   config hashes on purpose.
2. Mark the approved decisions AGREED in decisions.md, with the approval date.
3. Rerun the full test suite and the Step 2 gate on that commit. Then tag it `specs-v1` and push the
   tag.
4. Record the gate report and tag in the project plan, and tick the Step 2 gate.

From then on, any behavioural change is a new version and evaluation segment (FR-09).
