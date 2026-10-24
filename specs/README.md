# Strategy specifications (D05)

Status: **DRAFT v0.4, not approved.** It revises v0.1 using the owner's consolidated review, v0.2
using the owner's response to the drafter's challenges, and v0.3 using the owner's provisional
decisions on the remaining open items. Those decisions are draft implementation defaults: they get
frozen before the paper tournament, after the development correctness checks, and they are not
optimized parameters.
Recommendations that come from the review are proposals until the owner approves them. None of
the five strategies is known to be profitable; all are research hypotheses, and "none is ready"
is a valid tournament result.

| File | Contents |
|---|---|
| [shared.md](shared.md) | Clock, trade eligibility and bars, VWAP, ATR, swings, levels and zones, RVOL, regime, deadlines and guards, reference positions and exits, outcomes, processing order, configuration |
| [schemas.md](schemas.md) | Envelopes and identity, hashing, MarketEvent, FeatureSnapshot, StrategyEvaluation, StrategyTransition, Candidate, ReferencePosition |
| [strategies/](strategies/) | [VPC](strategies/vpc.md), [ORB](strategies/orb.md), [BRT](strategies/brt.md), [FBR](strategies/fbr.md), [VMR](strategies/vmr.md), each as an explicit state machine |
| [decisions.md](decisions.md) | Decision register: adopted, challenged and open choices |
| [CHANGELOG.md](CHANGELOG.md) | v0.1 → v0.2, with behavioural and editorial changes listed separately |
| [acceptance_tests.md](acceptance_tests.md) | Synthetic scenarios for the shared components and for every strategy |
| [consistency.md](consistency.md) | Cross-check of timing, units, identifiers, quotas and exits |
| [research_access_log.md](research_access_log.md) | Every inspection of market data made while writing these specs |

## Scope

The application evaluates the five approved strategy candidates. An incumbent strategy and any
additional benchmark account are outside scope (BRD v1.2, D-01). "Do not trade" stays as an
analytical reference (FR-12), without its own account.

The primary experiment compares **complete strategy packages**: each with its own entries, exits
and maximum hold, under common capital, risk-budget method, data, execution and accounting rules,
and one common session cutoff. Each strategy runs in its own paper account. One account's position
never suppresses another's signal.

**Two layers** (SD-11, D-17, D-33). The strategy engine maintains a deterministic **underlying
reference-position model** in replay and in paper. Strategy timers, one-position eligibility,
cooldowns, quotas and directional brakes are governed by that model. Paper option execution
maintains separate positions and safety controls, including premium-loss stops. Execution
outcomes don't modify strategy state, unless an explicitly approved future version introduces that
dependency. The project therefore produces two histories, strategy/reference behaviour and
realized paper-account behaviour, and their divergence is evidence in its own right.

## Parameter provenance

Initial defaults are informed by descriptive market statistics from Feb 1 – Apr 30 2024. **No
parameter has been chosen using strategy P&L, win rate or any other performance measure.** Every
data inspection made while writing the specs is recorded in
[research_access_log.md](research_access_log.md). Data-informed defaults are still design choices
shaped by what was seen.

## Units

- **A** is the 1-minute ATR (SD-05). Thresholds in A scale with a local volatility estimate rather
  than fixed dollars. That doesn't imply the same behaviour, or profitability, across price
  levels, regimes or periods. Each threshold names its ATR reference: `A_setup`, `A_cur` or
  `A_OR`.
- **`r_u`** is the underlying risk distance, \|`entry_reference` − `invalidation_level`\|, frozen at
  emission. It is separate from account-dollar risk and option premium, both of which are set in
  Step 3. Later stop moves don't change it.
- **Slots** are scheduled session minutes (SD-01). "10 bars" means 10 scheduled slots, not 10
  observed bars or 600 elapsed seconds.

## Descriptive scales (Feb 1 – Apr 30 2024, 62 sessions)

Reproduce with [research/scale_stats.py](../research/scale_stats.py). Its header defines the exact
populations, the aggregation order and the quantile method. They're computed from Alpaca's bars,
with a simple-moving-average ATR, so they're **descriptive only** and are not SD-05 features. Values
were computed once and haven't been independently re-validated.

| Measure (per-session value, then across sessions) | 10% | Median | 90% |
|---|---|---|---|
| 1-minute ATR (per-session median of SMA-14 TR, bars 31+) | $0.12 | $0.17 | $0.29 |
| Day range / session ATR | 14 | 24 | 34 |
| 5-minute opening-range width / session ATR | 2.8 | 4.1 | 5.9 |
| 15-minute opening-range width / session ATR | 4.1 | 6.1 | 9.3 |
| 30-minute opening-range width / session ATR | 5.3 | 7.5 | 12.0 |
| Max \|close − VWAP\| / rolling ATR, bars 16+ | 7.1 | 11.6 | 19.5 |
| 90th percentile of \|close − VWAP\| / rolling ATR, bars 16+ | 4.0 | 7.7 | 14.4 |
| VWAP sign changes, bars 16+ (zeros counted) | 5 | 12 | 36 |

Full-session summaries describe the data. They never feed intraday decisions.

## Data partitions (D-02)

Agreed in principle (D-02). Dates are still the owner's.

| Data or process | Purpose |
|---|---|
| Historical development | Develop the specs, debug implementations, inspect behaviour |
| Historical held-out replay | One pre-registered report on correctness and behaviour, using previously uninspected inputs |
| Live paper tournament (Phases 3–4) | Select candidates using prospective paper results |
| Later unseen paper period (Phase 5) | Confirm the selected candidate, unchanged |

Historical replay validates implementation behaviour against the specification. It doesn't
select the tournament winner or establish executable profitability. Correctness itself comes
from the explicit specs, synthetic fixtures, invariants and implementation tests. The held-out
report adds evidence on new inputs.

**What the held-out report may lead to** (declared before it runs):
- **A software defect:** fix the implementation and rerun as a new version. The spec is unchanged.
- **A behavioural finding:** this may motivate a **new** spec version. Once it does, that historical
  period is no longer untouched for the revised spec, and the access log records it.
- **Nothing else:** the report never ranks strategies or tunes thresholds.

Implementation debugging against a frozen rule and changing a rule because of the trades it
produced are both logged. Only the second counts as strategy development, and it creates a new
version.

```yaml
data_partitions:
  historical_development: {start: REQUIRED, end: REQUIRED}
  historical_holdout:     {start: REQUIRED, end: REQUIRED}
  paper_selection:        {start: set at Phase 3 activation}
  paper_confirmation:     {start: set at Phase 5 activation}
research_access_log: specs/research_access_log.md
```

## Approval and identity

1. The owner reviews v0.2 and settles the open items in [decisions.md](decisions.md).
2. Status becomes `APPROVED v1`. Every strategy threshold and behavioural choice is written into
   validated, versioned configuration (SD-14). Invariant rules and schema constants may stay in
   code.
3. The approved commit is tagged `specs-v1`. A trade is identified by its spec version, strategy
   `config_hash`, code commit, `feature_version`, `schema_version`, experiment manifest and approval
   record together; a tag alone isn't enough.
4. A behavioural change makes a new version and evaluation segment (FR-09). An editorial
   correction is recorded but doesn't start a new segment.

**Done criterion:** two independent implementers would produce the same state transitions and
candidates from the same event stream, without making undocumented choices.
