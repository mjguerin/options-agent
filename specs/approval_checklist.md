# Specs v1 approval checklist (Step 2 gate: "specs approved and tagged")

The other two gate conditions, no look-ahead and traceability, are evidenced by the gate run
(`research/replay/step2_gate.py`). This list is what the owner decides before the specs are
frozen as `specs-v1`. Nothing here asks for parameter tuning: every number stays as written.

## 1. Owner decisions still open

| # | Item | Options |
|---|---|---|
| 1 | **D-02 data partition dates** | Set `historical_development` and `historical_holdout`. Behaviour has been inspected only for Feb–Apr 2024 (access log #8 and #17); every later period is integrity-only |
| 2 | **D-04 freeze item 9 (OPEN-SRC-1 to 4)** | (a) Obtain the older CTS editions and lifecycle text, then close the gaps. (b) Freeze v1 with the gaps documented: the `O` and `N` assumptions stay development-only, so the held-out report must disclose them (E-5) |

## 2. Drafter choices awaiting explicit approval

Each is in [decisions.md](decisions.md) with its rationale.

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

## 3. Shared definitions to approve (project plan Step 2)

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
