# Strategy review registry

Hypotheses are registered here **before their first Screen run** and are frozen from the moment the
commit that adds them is pushed (protocol: `strategy_review_protocol.md`, D-94). Values, predictions,
bars and the analysis plan below do not change after that. A factual error is corrected by a dated
erratum at the bottom that states its effect; a value, a bar or a plan is never edited. A changed
idea is a new hypothesis with a new ID and counts against the round's cap.

## Round 1: BRT only

Registered 2026-11-20 by the owner's decision, against code at commit `503a5f3`. Each hypothesis is
tested **alone** against v1; combinations are not tested (a combination would be a new hypothesis).
Variant configs are copies of `brt-v1.yaml` with one value changed; v1 is untouched. **Values were
chosen by simulation on the Design partition under the rule in protocol section 3a** (access log
#26), not guessed. Four hypotheses of the six a round allows.

| ID | Variant | Change (units: multiples of A) | Mechanism |
|---|---|---|---|
| BRT-H1 | `config/strategies/brt-v2a.yaml` | `brk_min` 0.25 to **0.75** | A break that clears the level by only a quarter of a typical bar is more likely noise; a break must be decisive before it is traded |
| BRT-H2 | `config/strategies/brt-v2b.yaml` | `retest_wick_max` 1.0 to **0.7** | The spec calls 1.0 A "an initial design choice, not validated"; a deep wick under the level means it failed intrabar even if the bar closes back above. 0.7 is the value the section 3a rule selects |
| BRT-H3 | `config/strategies/brt-v2c.yaml` | `rr_min` 1.2 to **1.35** (times r_u) | A setup whose target is close to its entry offers little reward for its risk and is overtaken by costs and noise |
| BRT-H4 | `config/strategies/brt-v2d.yaml` | `retest_wick_max` 1.0 to **0.5** | The same mechanism as H2 at the value that makes wick and close tolerance equal (`retest_depth` = 0.5 A), registered separately because the rule's choice for H2 is a change of three positions |

### What the Design simulation showed (in sample, 62 sessions, 74 v1 positions, mean R -0.273)

| ID | Positions / session (v1 1.19) | Mean R | Paired difference vs v1 | 80% lower bound | Mar / Apr difference |
|---|---|---|---|---|---|
| H1 `brk_min` 0.75 | 0.94 (78% of v1) | -0.037 | **+0.236R** | +0.070 | +0.32 / +0.16 |
| H2 `retest_wick_max` 0.7 | 1.18 (99%) | -0.221 | **+0.052R** | +0.011 | +0.08 / +0.03 |
| H3 `rr_min` 1.35 | 1.18 (99%) | -0.152 | **+0.121R** | +0.066 | +0.14 / +0.09 |
| H4 `retest_wick_max` 0.5 | 1.11 (93%) | -0.105 | **+0.168R** | +0.104 | +0.24 / +0.10 |

Things a reader should know about that simulation:

- February shows no positions for any variant, so the rule's "positive in at least 2 of 3 months"
  was in effect "positive in both March and April".
- H1's gain comes mostly from a **different set of trades**, not from filtering: 51 of v1's 74
  positions drop out and 35 new ones form (later, more decisive breaks). The new ones averaged +0.16R
  and the v1 positions that remain -0.34R.
- H3's effect is flat at about +0.10R to +0.12R across the whole grid 1.35 to 1.75, and the positions
  it removes average between -0.44R and -0.74R: the most credible of the four.
- H2's value is a change of three positions and is too small for any partition to detect; H4's value
  is the best of its row but is not the one the rule picks.
- The values originally guessed before the simulation (0.5, 0.5, 1.5) were never frozen. The H1
  guess (0.5) would have **failed** the rule (+0.03R, negative in April).
- Everything above is **in sample**, with a standard error near 0.12R on a mean. Out of sample the
  effects should be expected to be smaller.

### Predictions, frozen ex ante

Made before any Screen or Validate result. Rule: between a quarter and three quarters of the Design
estimate (shrinking for selection bias), and the positions-per-session ratio around the Design value.

| ID | Mean R per position, variant minus v1 | Positions per session, share of v1 |
|---|---|---|
| BRT-H1 | +0.06 to +0.18R | 70% to 85% |
| BRT-H2 | +0.01 to +0.04R | 95% to 100% |
| BRT-H3 | +0.03 to +0.09R | 93% to 100% |
| BRT-H4 | +0.04 to +0.13R | 88% to 98% |

The Screen bar is a difference of at least +0.05R with the 80% lower bound above zero. H1 and H4
predict effects that clear it; H3's range straddles it; H2's predicted effect is **below** it and is
too small for Screen to detect, so a fail there is weak evidence against it. Screen is a loose gate, not
a claim: it is likelier to turn away a real small improvement than to let in a false one. Claims rest
on Validate and Confirm only.

Frozen file fingerprints (SHA-256 of the file bytes):

| File | SHA-256 |
|---|---|
| `config/strategies/brt-v1.yaml` | `80550d007adad20124e1e8db5a876165bed496f36359ed550de181d071910cc7` |
| `config/strategies/brt-v2a.yaml` | `26ad5c121ae711f9dc5758c6c1e66fd10d372faf90dbc68d229b8b21c829181e` |
| `config/strategies/brt-v2b.yaml` | `dd7a1a056a75ab1246ba10e8f326b106702fcf2fecce6db54f62ba79a397260c` |
| `config/strategies/brt-v2c.yaml` | `8ddd6aad04d0227c66c867ea07010be571d8680cb8942d742bcf30c2ea22fe83` |
| `config/strategies/brt-v2d.yaml` | `27cef3486947a0d36cba4a8b777ce48a21620ddd335e73741e1e689e1dfeed7b` |

## Frozen analysis plan (Screen, 2024-05-01 to 2024-08-31)

- **Data and sessions.** The development replay (`Usage.DEVELOPMENT`) over every session in the
  history store dated 2024-05-01 to 2024-08-31, as the replay harness runs them, early closes
  included, none excluded. The dataset identifier is recorded in the output. The Validate,
  Confirm and live partitions are not read.
- **Runs.** v1 and the four variants over the same sessions in one process, same features, only the
  BRT worker's config differing. Before reading any result, a check that a variant with v1's values
  reproduces v1's positions exactly (as the Design script did).
- **Position and result.** Each emitted candidate opens one reference position, as in the Step 2
  replay. Its result is R = direction x (reference exit price - reference entry price) / `r_u`. Only
  positions with a reference exit are counted.
- **Primary statistic.** The difference in mean R per position, variant minus v1, pooled over the
  partition. Its uncertainty is a bootstrap over **sessions**: 10,000 resamples of the sessions with
  replacement, seed 20261120, the statistic recomputed for each. The one-sided 80% lower bound is the
  20th percentile of the resampled differences.
- **Screen pass, all of:** difference at least **+0.05R** (lowered from +0.10R by the owner on
  2026-11-20, before any Screen run and before this registration was frozen); the 20th-percentile
  bound above zero; positions per session at least 70% of v1's (and not above 130%).
- **Reported, never decisive:** positions and positions per session for v1 and the variant; mean and
  median R; win share; payoff ratio; total R per session; the difference and its bound by calendar
  month (May to August); the mean R of positions the variant removes, keeps and adds. Printed output
  is aggregates only: no per-position, per-date or per-session rows, and no charts.
- **Logging.** Each run gets an access-log entry **before** its output is read, naming the data, what
  is printed and the permitted consequence: "advance, or stop; no value, bar or plan may change".
- **After Screen.** Passing hypotheses go to Validate (Sep-Dec 2024) with the same statistic. There the
  bound is one-sided 95% after Holm adjustment across the **4** registered tests, the worst calendar
  month must not be below -0.30R against v1, and the option-level paired difference in dollars per
  position must have a positive point estimate. **The option-level replay method (how entry and exit
  prices are taken from the stored option data) is registered in a separate dated note before the first
  Validate run**; it is not needed for Screen. A hypothesis that fails a stage is closed; it cannot be
  re-run with a changed value.
- **Multiple testing.** Screen uses the loose bound above (a gate, not a claim). Claims rest on
  Validate and Confirm only.

## Errata

None.
