# Strategy review protocol (APPROVED by the owner, 2026-11-20)

Drafted Nov 3, 2026; approved by the owner on Nov 20, 2026 with the scope decision below. Its purpose
is to settle **in advance, before any outcome is looked at**, when and how the five strategies may
be changed, so that a change comes from evidence and not from the last few trades.

## 1. What this protocol is, and is not

- It governs **changes to a strategy's rules**: the setup, trigger, invalidation, target, trail,
  timers and eligibility in the strategy specs. It does not govern the risk limits, order handling
  or the exit-rule scoreboard (D-93), which are execution-layer questions with their own track.
- It **never changes the live shadow system while a session runs**, and never replaces v1. A
  change becomes a new version (`v2`) that runs *beside* v1 on the same signals as its own
  evaluation segment (BRD FR-09; plan: "no code changes during the evaluation period").
- **Scope (owner, Nov 20).** All five strategies stay in the tournament. Strategies are reviewed
  **one at a time**: round 1 is **BRT only**. FBR and then ORB follow only after the previous
  strategy's round has ended. VPC and VMR are not changed under this protocol: they produce too few
  signals to test (VPC about none, VMR about 0.1 to 0.3 a session). If their frequency ever changes
  they can be added here by the owner's decision.
- "No change" is an acceptable result. If nothing survives, v1 stands, and a finding such as "BRT
  shows no demonstrated edge in the development period" is reported honestly (FR-12: not trading is
  a valid answer).

## 2. Why live results are not the trigger

Live shadow results are 19 trades in six sessions. A single trade moves a strategy by about one
R, and seeing a 0.2R improvement takes roughly 200 trades per strategy, which is 100 to 150
sessions for BRT and FBR and over 500 for ORB. Live results may raise **questions**. They may not
select, tune or justify a change. A catastrophic failure (a bug, a feed problem, behaviour that
contradicts the spec) is an integrity matter, handled outside this protocol.

## 3. Data partitions and their roles

| Partition | Dates | Role | Looks allowed |
|---|---|---|---|
| Design | 2024-02-01 to 2024-04-30 (62 sessions) | Where hypotheses are informed and values are chosen (section 3a). Its behaviour has already been inspected (access log #8, #17, #18, #24, #25) | Unlimited, logged |
| Screen | 2024-05-01 to 2024-08-31 (about 85 sessions) | First test of a registered hypothesis | One run per registered hypothesis |
| Validate | 2024-09-01 to 2024-12-31 (about 85 sessions) | Second, independent test of what survived Screen | One run per survivor |
| Confirm (held out) | 2025-01-02 to 2026-10-22 (about 450 sessions) | Final test of at most two finalists | **One look per finalist, ever** |
| Live shadow | 2026-10-27 onward | Monitoring and falsification of bugs; runs v2 beside v1 once approved | Reading only; never used to select |

- The Screen and Validate partitions have so far had integrity checks only. v1's baseline on them is
  measured **inside the same run** as the hypothesis, paired by session, so no separate look at v1
  "to see how it did" is taken first.
- A failed finalist is dead. A modified version of it is a new hypothesis and needs a new
  registration; it cannot re-use the Confirm look of the one that failed.
- Every run is logged in the research access log **before** it is read, in the usual form: data,
  what is looked at, what is printed, the permitted consequence.

### 3a. Choosing a value from the Design partition (amended Nov 20, 2026, at the owner's request)

A value is no longer a guess. It is picked by a simulation on the Design partition, under rules
fixed in advance:

- **Mechanism first.** The parameter and the *direction* of the change come from the mechanism written
  in the registration. The data only sets the magnitude. No new rule or parameter is found by
  searching the data.
- **Design only** (Feb to Apr 2024). Screen, Validate, Confirm and live are not read.
- **One parameter at a time**, the other values staying at v1's. A grid of 5 values, declared in the
  access log before the run: v1's value and four on the mechanism's side.
- **Printed per grid value:** positions and positions per session; mean and median R; win share; the
  paired difference against v1 with its bootstrap bound (section 5); the difference in each of the
  three calendar months; and the mean R of the positions the change removes, keeps and adds.
- **Selection rule.** A grid value is *eligible* if all hold: positions per session at least 70% of
  v1's; paired difference above zero; positive in at least 2 of the 3 months. An eligible value whose
  neighbours on the grid are both ineligible is discarded as noise. Among the remaining eligible
  values, **take the one closest to v1's**. If there is none, the parameter keeps v1's value and its
  hypothesis is closed as "no support in Design".
- **Freezing.** The chosen value, and the Design result at it as the prediction, are frozen at
  registration. Screen and the later stages see one value per hypothesis.
- **Limits, stated honestly.** Design holds only about 74 BRT positions, so one mean has a standard
  error near 0.12R, larger than most differences between grid values. The rule exists to avoid
  chasing that noise, and a chosen value should be expected to look better in Design than it will out
  of sample. The later stages are the claim; Design is the guide.

## 4. Hypotheses: what a registration contains

A hypothesis is registered, dated and frozen **before** its first Screen run. It states:

1. **Strategy and mechanism**: why the change should help, in words, from how the market is
   supposed to behave, not from a pattern in last week's trades.
2. **The change**: one rule, with its value fixed at registration, chosen by the Design procedure
   in section 3a (not by a sweep on any other partition). If more than one value is genuinely to be
   compared outside Design, each is a separate registered hypothesis.
3. **The prediction**: the direction and a rough size, and which other quantities should *not*
   move (for example "trade frequency stays within 30% of v1").
4. **The test and the bar it must clear** (section 5).

**At most 6 hypotheses are registered in a round**, and at most 2 rounds. The cap exists because
every extra test raises the chance that one passes by luck. Hypotheses are written without
reference to Screen, Validate, Confirm or live outcomes. The material they may draw on is the
Design partition, the specs, and the structure of the strategies.

**Round 1 (BRT only).** The registered hypotheses, with their values chosen on the Design partition
under section 3a, are in `strategy_review_registry.md`: BRT-H1 (`brk_min` to 0.75), BRT-H2
(`retest_wick_max` to 0.7, the section 3a choice), BRT-H3 (`rr_min` to 1.35) and BRT-H4
(`retest_wick_max` to 0.5, the same mechanism at the value that makes wick and close tolerance equal).
That is 4 of the 6 hypotheses a round allows. The mechanisms came from the strategy spec text alone.

Questions for the other strategies (FBR's and ORB's tails, an entry confirmation that removes weak
signals) are deferred until BRT's round has ended.

## 5. Measures and the bar a change must clear

**Unit of analysis:** the strategy's own reference position on SPY, in units of its own risk (R),
as in the Step 2 replay. This isolates the strategy from option pricing and execution. R is a
proxy, though: a trade's money is made in options, where the 20% premium stop clips losses and a
winner can run much further, so a change that helps in R can differ in dollars (BRT's live dollars
have been positive while its replay R was negative). For that reason a candidate is also scored at
the **option level at the Validate stage**, not only after Confirm: the stored one-second or
one-minute option data replayed with the D-80 benchmark and the premium stop, paired by session
against v1, in dollars per position (net of the approved costs, D10).

**Primary measure:** the paired difference in mean R, v2 minus v1, over the same sessions, with
a block bootstrap over sessions for its uncertainty. **Secondary, reported always:** win share,
payoff ratio, trades per session, worst month, and worst losing streak. Win share is never the
decision measure: locking small gains raises it without changing the money.

| Stage | Pass if all hold |
|---|---|
| Screen | Paired difference at least **+0.05R** (lowered from +0.10R on 2026-11-20 by the owner, before any Screen run: the predicted effects are mostly smaller than +0.10R); its one-sided 80% bound above zero; trades per session within 30% of v1 |
| Validate | One-sided 95% bound above zero in R after Holm adjustment across **all** registered hypotheses of the round; trades per session within 30% of v1; no calendar month worse than −0.30R against v1; **and** the option-level paired difference in dollars per position has a positive point estimate (the same sign as the R result) |
| Confirm | Point estimate positive and its one-sided 90% bound above zero in R; the option-level paired difference positive in sign; same frequency band; the Validate result not reversed in sign |

**Rough power (one R is the typical spread of a single position).** With about 100 positions in a
partition the standard error of a mean is near 0.10R, so a single partition can only detect
differences of roughly 0.25R for BRT and FBR and 0.5R for ORB and VMR. The Confirm partition
(about 500 positions each for BRT and FBR) can detect about 0.1R. A hypothesis whose predicted
effect is smaller than the partition can detect is declared **untestable**, not passed.

## 6. How a change is made

- One rule per hypothesis; values fixed by mechanism. The v1 posture of "no new indicators and no
  parameter optimization" is kept unless the owner explicitly lifts it for a named hypothesis.
- The change is implemented as a new strategy file and config (`vpc-v2`, `brt-v2`, and so on), with
  its own hash. v1 files are not edited.
- Before any Screen run the code is run through the existing contract and determinism tests.

## 7. After a candidate survives Validate and Confirm

1. The owner approves the finalist's v2 specs (new decisions, new `specs-v2` tag).
2. The Step 2 replay gate is rerun for the v2 code: determinism, traceability, no look-ahead.
3. The option-level replay is repeated on the Confirm partition with the approved costs (D10).
4. v2 runs in shadow **beside** v1 on the same signals, as its own segment, for a period fixed in
   advance (D09). It does not replace v1 and nothing is back-filled.
5. Entry into the tournament follows D09 and the promotion gates (D-83), not this protocol.

## 8. Stopping

- Round 1 ends when its registered hypotheses have been run through Screen and Validate (and
  Confirm for at most two finalists). Round 2 is allowed once, and only for hypotheses registered
  before Round 1 results were read for that purpose.
- If no hypothesis survives, the review stops and v1 stands.
- A hypothesis cannot be rescued by changing its value, its partition or its bar after a run.

## 9. Decisions

Settled on Nov 20 by the owner:

1. The partition split (Design / Screen / Validate / Confirm).
2. The caps: 6 hypotheses a round, 2 rounds, 2 finalists.
3. The pass bars, including the option-level check at Validate.
4. The "no new indicators and no parameter optimization" posture is lifted for the three BRT
   parameters registered in the registry only (`brk_min`, `retest_wick_max`, `rr_min`).
5. The BRT ideas (break clearance, wick tolerance, room to target) are kept; their values are chosen
   on Design under section 3a and registered as BRT-H1 to H4.
6. **D10, the cost model, is approved** (D-88): no commission; 0.0403 dollars per contract on each
   side (ORF, OCC and CAT) plus the SEC fee on the sale value; the current schedule applied to the
   whole history. Results from Nov 20 are labelled "net of approved costs", so every bar in section 5
   is net from the first Screen run. Fees are about 1% of a typical trade's result and equal for v1
   and v2, so they decide little; their purpose here is to fix the assumption before any run.

**This protocol is therefore approved as written.** The next step is to register BRT-H1 to H3 in
the access log with their frozen values, then the first Screen run.
