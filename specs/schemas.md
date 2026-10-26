# Record schemas (BRD 6.1, 11; MD-02)

Status: **APPROVED v1 (owner, 2026-10-25), tag `specs-v1`.** `schema_version = signals-v1`.

These are signal and replay records. They are **not** the paper-execution model; separate Step 3
schemas cover risk reservations, order intents, client and broker order IDs, fills, positions,
reconciliation, accounting and incidents.

## S-1 Envelopes and identity

There are two envelopes. Shared market records carry no strategy or account fields.

| Shared-data envelope | Strategy-experiment envelope |
|---|---|
| `schema_version` | `schema_version` |
| `record_id` | `record_id` |
| `dataset_id`: the history store version, or `live` | `experiment_id` |
| `stream_id`, for example `spy_sip_bars` | `run_id` |
| `session_id`, for example `XNYS-2024-02-01` | `strategy_id`, `strategy_version`, `config_hash` |
| `event_ts` (kind-specific meaning, S-2) | `account_binding`: the logical binding (for example `acct:vpc`) in replay and paper; `broker_account_ref` may be null in replay |
| `created_at` | `event_ts`, `created_at` |
| `correlation_id` | `correlation_id` |

- Missing values are `null`, never empty strings.
- **Runs:** a fresh replay or rerun gets a new `run_id`. A process restart that resumes saved state
  keeps the `run_id` and records a new `process_instance_id`.

## S-2 MarketEvent

| Field | Notes |
|---|---|
| `stream_id`, `session_id`, `seq` | `seq` is dispatch order within (stream, session) and is unique only within that pair. It doesn't promise increasing source timestamps |
| `source` | Data origin, for example `alpaca_sip`. Never `replay` |
| `execution_mode` | `replay`, `shadow` or `paper` |
| `event_key` | Source identity where the source has one (trade ID, tape, timestamp); otherwise a hash of declared identity fields (S-3) |
| `revision` | 0 at first emission; increases if a record is reissued (live corrections only) |
| `integrity_hash` | Hash of the full canonical payload, used to detect corruption, not to identify the event |
| `kind` | One of the kinds below |
| `payload` | Kind-specific; see below |
| `quality` | Flags: `late`, `no_price`, `volume_only`, `unknown_condition`, `out_of_order`, `gap_before`, … |
| `receive_ts` | The recorded arrival time in live; **null in replay** (never fabricated) |
| `modeled_available_at` | Replay only: the modeled availability under `clock_model` (SD-01) |

| `kind` | Payload and time meaning |
|---|---|
| `bar_1m` | `slot`, `bar_start`, `bar_end`, `finalized_at`, `interval_status` (SD-01), `bar_present`, OHLC (nullable), `volume`, `notional`, `trade_count`, `rules_version`. `event_ts` = `bar_end`. Emitted for **every** scheduled slot, including ones without a bar |
| `session_open` / `session_close` | Scheduled open and close; `event_ts` = the effective time |
| `halt` / `resume` | `effective_at`, scope (symbol or market-wide), reason |
| `data_gap` | Affected interval [from, to), detected at |
| `correction` | Original `event_key`, action (`cancel` or `correct`), new values, received at |

**Duplicates:** two deliveries with the same `event_key` and `revision` are one event. Different
keys are different events, even with identical content.

**Restart:** after a restart, `seq` continues from the last persisted value for that stream and
session. Broker and account streams have their own `stream_id`s.

## S-3 Hashing and canonical form

- Canonical JSON per RFC 8785, after the application rules below. The participating fields,
  timestamp representation and numeric representation are frozen with v1. Canonical
  serialization alone doesn't define record identity:
  - Each record type lists the fields that take part.
  - Prices are rounded half-even to 1e-6; ratios to 1e-9.
  - Timestamps are UTC ISO-8601 with nanoseconds and a `Z` suffix.
  - Nulls are explicit.
  - NaN and infinity are forbidden; a record containing one is rejected.
- Hash: SHA-256, lowercase hex.
- **Logical keys vs record IDs:**
  - `signal_key` = hash(`strategy_id`, `strategy_version`, `config_hash`, `dataset_id`,
    `session_id`, `setup_ordinal`, trigger `slot`). It's identical across reruns, so runs can be
    compared. It's the logical signal identity.
  - `candidate_id` = hash(`run_id`, `signal_key`). It's the candidate occurrence within its
    experiment or run context, unique per run.

## S-4 FeatureSnapshot

Built once per `bar_1m` and shared by all workers (FR-03).

| Field | Notes |
|---|---|
| shared-data envelope | |
| `snapshot_id` | Hash of `dataset_id`, `stream_id`, `session_id`, `seq`, `feature_version`, `features_config_hash` and the canonical values |
| `seq`, `slot` | The source bar |
| `market_as_of` | The bar's `bar_end` |
| `scheduled_finalization_at` | `bar_end` + `finalize_delay` (SD-01) |
| `available_at` | The **actual** `finalized_at` (or `modeled_available_at` in replay); never backdated to the scheduled time |
| `computed_at` | Wall-clock computation time |
| `feature_version`, `features_config_hash` | |
| `bar` | The finalized bar object: the single source for OHLC and volume |
| `vwap` | The only authoritative VWAP value: last-eligible session VWAP (SD-04) |
| `vwap_notional`, `vwap_volume`, `total_volume` | Cumulative accumulators. VWAP's population differs from `total_volume`'s by design |
| `disp_30_atr`, `occ_up_30`, `crosses_60`, `regime` | SD-04, SD-09. `regime` ∈ TREND_UP, TREND_DOWN, RANGE, INCOMPLETE, NEUTRAL, or null when NOT_READY |
| `atr` | `{a_cur, atr_source}` |
| `rvol` | SD-08 |
| `zones` | `zone_version` plus a reference to the zone-set record (zones aren't copied into every snapshot) |
| `levels` | PDH, PDL, PDC, PMH, PML, ORH, ORL and OR status |
| `swings_new` | Swings confirmed on this slot. Earlier swings are referenced by `swing_id` |
| `readiness` | Map from feature name to `{ready, reason}`. An unready value is `null` |

## S-5 StrategyEvaluation (one per strategy per slot)

| Field | Notes |
|---|---|
| strategy envelope | |
| `evaluation_id` | Hash of `run_id`, `strategy_id`, `session_id`, `slot` |
| `seq`, `slot`, `snapshot_id` | |
| `evaluated_at` | |
| `readiness_status`, `eligibility_status` | With reason codes (SD-12) |
| `setup_state` | State-machine state, including `IDLE` |
| `outcome` | One of the five outcomes |
| `reason_codes` | All the reasons that applied |
| `setup_id`, `candidate_id`, `transition_id` | Nullable |

## S-6 StrategyTransition (only when state changes)

| Field | Notes |
|---|---|
| strategy envelope, `transition_id` | |
| `cause_type` | `snapshot`, `reference_exit`, `session_event`, `expiry`. Broker events, execution exits and operator controls are **never** causes of strategy transitions (SD-11) |
| `cause_record_id` | The record that caused it |
| `snapshot_id` | Nullable: non-bar causes don't reference a snapshot |
| `from_state`, `to_state`, `outcome`, `reason_codes` | |
| `setup_id` | Nullable before a setup exists |
| `anchors` | The setup's frozen values at this point (`A_setup`, `zone_version`, H, L, P, X, RL, …) |

## S-7 Candidate

| Field | Notes |
|---|---|
| strategy envelope | Experiment, run and logical account binding are set **at creation**; never added later |
| `candidate_id`, `signal_key`, `setup_id` | S-3 |
| `underlying_direction` | `bullish` or `bearish`. Option side is decided later by contract selection |
| `trigger_bar` | `{slot, bar_start, bar_end}` |
| `signal_bar_end` | The trigger bar's `bar_end`; also `strategy_clock_origin` (SD-11) |
| `signal_available_at` | The trigger bar's **actual** `finalized_at` (telemetry) |
| `scheduled_finalization_at` | `signal_bar_end` + `finalize_delay` |
| `emitted_at` | Wall-clock emission time |
| `expires_at` | `scheduled_finalization_at` of the signal slot + `candidate_ttl` (SD-10): market-time anchored, so processing delay never extends it |
| `submission_eligible` | false if `signal_available_at` ≥ `expires_at` (SD-10). The candidate and its reference position exist either way |
| `snapshot_id` | |
| `entry_reference` | The trigger bar's close |
| `trigger_level` | The level whose crossing fired |
| `invalidation_level` | Close-based thesis exit (SD-11) |
| `r_u` | \|`entry_reference` − `invalidation_level`\|; must be positive and finite. It's frozen as the initial underlying risk, and later stop moves never change it |
| `target` | Typed: `{type: fixed_level, price, anchor}` or `{type: r_multiple, r, price}`. The price is computed and frozen at emission. It also carries `target_source`, `range_pair_id` (nullable) and `zone_version` where the strategy defines them (FBR) |
| `trail` | `{type: none}` or `{type: swing_trail, activate_r, buffer_atr}` |
| `timers` | Each: `{name, clock: session_slots, origin: signal_bar_end, length, endpoint: inclusive}`. The final slot is evaluated before the deadline fails |
| `geometry_check` | Bullish: invalidation < entry < target; bearish: the reverse. A candidate failing it is never emitted |
| `features` | A declared subset of snapshot values; tests check that they match the snapshot exactly |
| `reason_codes` | |

**Downstream records** (Step 3), never edits to the candidate: `candidate_accepted`,
`candidate_rejected` (with reason), `candidate_expired`, `contract_selection_failed`, plus a
reference to the execution policy version used.

## S-8 ReferencePosition (strategy layer, replay and paper)

One per emitted candidate. It's the strategy's deterministic position (SD-11). It isn't an
account position, and its prices are bookkeeping anchors, not fills.

| Field | Notes |
|---|---|
| strategy envelope | |
| `reference_position_id` | Hash of `candidate_id` |
| `candidate_id` | |
| `underlying_direction` | `bullish` or `bearish` |
| `reference_entry_price` | Signal bar close |
| `reference_entry_time` | `signal_bar_end` (= `timer_origin`) |
| `initial_invalidation`, `r_u` | Frozen from the candidate |
| `target_policy`, `trail_policy` | Frozen from the candidate; `trail_stop` history is kept as transitions |
| `exit_conditions` | The strategy's reference-exit set and parameters |
| `reference_exit_time` | `bar_end` of the exit slot |
| `reference_exit_price` | Exit slot close, or the last valid close, with `stale_reference_price` |
| `exit_primary_reason` | SD-11 precedence |
| `exit_triggered_reasons` | Every reference exit that fired on that slot |

Paper execution has its own records (Step 3) that link to `reference_position_id`. Nothing in
them writes back to this record.
