# Step 3 gate: acceptance criteria in shadow mode (AC-01..16, AC-21..24)

Written Oct 26, 2026. "Test" means an automated test that passes today (608 pass, 2 network
skips). "Host" means checked on the installed mini. "Live" means it needs a real session: a
shadow day on the installed jobs. Nothing below is marked passed on a claim; each row names its evidence.

| AC | What it requires | Evidence now | Still needed | Status |
|---|---|---|---|---|
| 01 Account isolation | Opposing candidates reach only their own accounts | Test: shadow_accounts `accounts_are_isolated`, account_runtime routing, live_engine opposing candidates | A live day with opposing signals, if one occurs | Covered |
| 02 Shared observations | All workers use one snapshot per broadcast event | Test: live_engine `every_finalized_slot_is_decided_once_with_one_shared_snapshot`; gate traceability `every_slot_has_one_snapshot_shared_by_workers` | None | Covered |
| 03 Deterministic replay | Repeated replays match | Step 2 gate: determinism, traceability, truncation on Feb–Apr 2024 (rerun after every refactor); recovery test of crash vs uninterrupted run | Rerun the gate on the release commit | Covered |
| 04 Risk reservation | A race cannot exceed the limit | Test: `concurrent_reservations_cannot_exceed_the_limit`, `asking_to_reserve_twice_is_one_claim` | None | Covered |
| 05 Submission timeout | Accepted-but-unacknowledged order reconciled, no duplicate | Test: `a_lost_reply_is_reconciled_by_client_id_and_not_resubmitted`, recovery `an_entry_order_in_flight_is_settled_on_restart_not_resent` | The modeled broker never loses a reply; the real case is Step 4 (paper) | Covered for shadow |
| 06 Partial fills | Reservations and exits follow actual quantities | Test: `the_claim_follows_partial_fills_cancels_and_exits`; SimBroker displayed-size cap tests | None | Covered |
| 07 Exit race | Simultaneous target, time and invalidation triggers cannot oversell | Test: `the_cutoff_and_a_stop_together_make_one_exit_with_both_reasons` (reference-layer triggers) | None | Covered: `every_exit_trigger_at_once_sells_the_position_only_once` (reference exit, operator close, flatten and the tick loop together give one sell, never short) |
| 08 Stale feed | Entries halt, incident recorded, positions follow the contingency | Test: live `silence_beyond_the_stale_threshold_is_uncertain`, alerts `a_feed_must_stay_down_past_the_grace_period_to_be_an_incident`, blind-exit contingency tests | Live: a real feed gap | Covered by tests |
| 09 Restart | Orders and positions found and managed before new entries | Test: the recovery suite (rebuild, in-flight order, orphan position, halts survive) and `recover_adopts_orders_the_broker_already_has` | Live: kill the engine mid-session and watch it recover (do once, deliberately) | Covered; live drill pending |
| 10 Versioning | A parameter change makes a new immutable version | Test: `a_manifest_is_immutable_and_approval_changes_nothing_else`; ledger revisions append; config hashes pinned | None | Covered |
| 11 Accounting | Synthetic fixtures reconcile to independent values | Test: `the_books_reconcile_to_the_cent` and the hand-computed ledger fixtures | Live: the first real ledger must reconcile | Covered; live pending |
| 12 Comparison | An unprofitable leader leads but is not qualified | Test: `an_unprofitable_leader_leads_but_is_not_promotion_qualified` | Promotion thresholds are unset (owner decision) | Covered |
| 13 Agent permissions | Order, credential and settings attempts are denied and logged | Host: verify_agent.sh (no .env, no DB writes, no view writes) passed; a live headless and a Remote Control session both refused writes and found no .env | Run `sudo ops/host/verify_agent_tools.sh` once (needs the agent signed in) and keep its output | Verified on the host (Oct 26, 2026): `verify_agent_tools.sh` 5 of 5 pass against the real model; shell, write, edit and web tools are never offered, and a read of `.env` was denied by the OS and audited |
| 14 Agent independence | Trading continues if the agent is down | Test: `test_the_trading_code_never_imports_the_agent` (added with this file); the agent has its own jobs and account | Live: stop the agent jobs during a session and confirm nothing changes | Covered statically; live pending |
| 15 Paper-only | Live endpoint or credential configs fail startup | Test: alpaca_adapter `live_endpoint_rejected`; secrets `a_live_looking_key_is_rejected`; feed guards | Check that a live-key pattern in `.env` also fails | Partial: check the credential side |
| 16 Controls | Pause, cancel, flatten, resume are distinct and audited | Test: the api suite (queue, confirmation, audit, expiry), account_runtime pause and flatten, breakglass | Live: a pause and resume through the dashboard | Covered; live pending |
| 21 Release preflight | Dirty tree or HEAD off the tag blocks paper and is logged | Test: manifest `a_dirty_tree_blocks_paper_but_only_warns_in_shadow`, `head_must_be_at_the_manifest_tag_for_paper` | Step 4 | Covered |
| 22 Risk-budget selection | No contract exceeds the budget; none feasible is rejected with a reason | Test: `no_budget_feasible_contract_is_rejected_with_a_reason`, `a_contract_dearer_than_the_budget_is_rejected_never_sized_up` | None | Covered |
| 23 Feed guard | Indicative options feed fails startup | Test: manifest `the_indicative_options_feed_is_refused`, live_options feed guard | None | Covered |
| 24 Agent OS isolation | The agent cannot read credentials or write the trading database | Host: 22 of 22 checks in verify_agent.sh passed on Oct 26, 2026; Postgres trust rules replaced by peer authentication | Rerun verify_agent.sh after any change to the host | Verified on the host |

## Open items before the gate

1. Live drills during the first shadow days: a deliberate engine kill (AC-09), stopping the
   agent jobs (AC-14), one dashboard pause and resume (AC-16), and the first real ledger (AC-11).
2. Rerun the Step 2 gate on the release commit.
