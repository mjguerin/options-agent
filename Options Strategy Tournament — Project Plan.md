# Options Strategy Tournament — Project Plan

Oct 21, 2026 · @Marc Guerin

## How to use this plan

Work the steps in order; a step's gate must pass before the next step starts. Steps follow the delivery phases in BRD v1.1, with a foundations step first and a platform build between replay and paper trading. There is no incumbent strategy: the project starts fresh with the five candidates (BRD v1.2).

- Each bullet is a task to tick off. Requirement codes (CS-05, AC-21) point to the BRD.
- No dates are committed (BRD section 17). Add target dates once D02 and D07 are closed.
- Code changes happen only after the market close, with the engine stopped.

## Step 1 — Foundations

Goal: the Mac mini, accounts and repo are ready, and the blocking questions are in motion.

**Accounts and data**

- [x] Email Alpaca support: is a second login for extra paper accounts permitted? (D02) Confirmed: second login permitted, 6 paper accounts available
- [x] Subscribe to Algo Trader Plus and confirm the price and OPRA access (D03) Subscribed on the second login at $99/month; real-time SIP and OPRA confirmed Oct 23, 2026 with `tournament data-check` (data keys: alias `data`)
- [x] Create one $10,000 paper account for development; keep the rest for Phase 2

**Mac mini host**

- [x] Disable sleep, enable restart after power failure, confirm network time sync
- [x] ~~Connect a UPS~~ Dropped (owner decision): no UPS. A power cut is an abrupt shutdown; autorestart brings the mini back, and startup reconciliation (OM-05) recovers state
- [x] SSH key-only login; add a `Host mini` entry on the MacBook
- [x] Install Homebrew, PostgreSQL, Python 3.12, uv, Supervisor
- [x] Store Alpaca keys in a gitignored `.env` with mode 600 (replaces the Keychain of BRD 9.1)
- [x] Set up nightly local pg\_dump (no off-machine copy, owner decision), and test one restore

**Code and tooling**

- [x] Create the private GitHub repo and clone it into the single working directory on the mini
- [x] ~~Connect the IDE from the MacBook with Remote-SSH~~ Not needed: development happens directly on the mini through Claude Code over SSH
- [x] Scaffold the modular monolith: adapters, features, strategies, accounts, orders, analytics
- [x] Add pytest and a migrations tool

**Gate (passed Oct 23, 2026):** host survives a reboot and power-cut test; the repo runs a hello-world against the Alpaca paper API with keys from `.env`. Reboot and power cut both recovered with no login: Postgres crash recovery clean, paper API reachable.

## Step 2 — Specify strategies and build replay (Phase 1)

Goal: all five strategies are written down precisely and replay through the same code that will trade paper.

**Specifications**

- [x] Write a spec per strategy: thresholds, confirmations, invalidation, exits, re-entry, cooldowns (D05) (draft v0.4 in specs/; approval is part of the gate)
- [ ] Approve the shared VWAP definition, bar construction and session calendar (MD-05, MD-06)
- [x] Define the event, feature-snapshot and candidate schemas (BRD 6.1, 11) (specs/schemas.md, draft v0.4)

**Data and features**

- [x] Download SPY and SPY option history from February 2024 into daily Parquet files (684 sessions to 2026-10-22, 4.5 GB; verified complete Oct 23, 2026)
- [ ] Build the event normalizer with gap, duplicate and late-event checks (MD-02, MD-03) (historical part done: interval status, gap/uncertainty handling, condition quarantine; live duplicate and late-event handling moves to Step 3's feed handler)
- [x] Build the shared feature engine, computing each feature once per event

**Replay**

- [x] Build the replay harness: same feature and strategy interfaces as live (BRD 5.3)
- [x] Implement the five strategy workers with the five required outcomes (NOT\_READY to INVALIDATED)
- [x] Write no-look-ahead tests on event-time features (feature truncation property test, plus the whole-chain gate check: trades cut at 12 points per session rebuild identical records; 744/744 on Feb–Apr 2024)
- [x] Prove determinism: two replays of the same day give identical candidates (AC-03) (gate run Oct 25, 2026: 62 sessions, all records identical; traceability 0 failures; report data/reports/step2_gate_2024-02-01_2024-04-30.json)

**Gate:** no look-ahead, every decision traceable to its snapshot, specs approved and tagged.

## Step 3 — Build the trading platform

Goal: everything between a candidate and a reconciled fill works in shadow mode before any paper order.

**Live market data**

- [ ] Live feed handler: one shared SIP + OPRA stream inside the engine process, fanned out to all five workers (FR-03)
- [ ] Subscriptions limited to SPY and the option contracts in play, within stream limits (MD-08, MD-10)
- [ ] Quote freshness and feature-readiness gates on entries (MD-04); stale feed halts entries (AC-08)
- [ ] Startup rejects the indicative options feed in paper mode (MD-09, AC-23)

**Accounts, risk and sizing**

- [ ] Experiment manager: immutable manifest with git tag, $10,000 balances, risk limits (BRD 10.1)
- [ ] Account runtimes with per-account risk state and order namespace (FR-01, FR-02)
- [ ] Risk budget -> contract selection -> quantity pipeline (CS-01A, CS-05)
- [ ] Atomic risk reservation, including pending and partial orders (RK-03, FR-07)

**Orders and exits**

- [ ] Order manager: persist intent and client order ID before submission (OM-02)
- [ ] Timeout handling that reconciles before retrying (OM-03)
- [ ] Position manager with exit precedence and no duplicate liquidations (EX-03, EX-05)
- [ ] Reconciliation at startup, after disconnects and periodically (OM-05)

**Startup and operations**

- [ ] Preflight: clean tree, HEAD at manifest tag, OPRA feed, paper endpoint only (NFR-15, MD-09)
- [ ] Supervisor programs: engine at a fixed pre-market time, API, dashboard, alert bot
- [ ] FastAPI controls: pause, resume, cancel, close, flatten, emergency (BRD 14)
- [ ] Streamlit dashboard bound to localhost; reach it through an SSH tunnel
- [ ] Telegram alert bot in deterministic code
- [ ] Evaluation ledger beside the broker ledger (BRD 12.3)

**Agent**

- [ ] Separate macOS user with a read-only DB role and no read access to `.env` (AG-11)
- [ ] Claude Code installed and signed in to your Pro plan; Remote Control tested
- [ ] Optional: Telegram channel, allowlist only you (AG-12)
- [ ] After-close headless job for the daily narrative (AG-13)

**Gate:** AC-01 to AC-16 and AC-21 to AC-24 pass in shadow mode.

## Step 4 — Two-account validation (Phase 2)

Goal: prove isolation, risk, execution and accounting on real paper fills with two accounts.

- [ ] Set the per-trade risk budget and daily loss limit (D07); review which SPY deltas that budget allows
- [ ] Set the expiration and delta policy from that budget (D06)
- [ ] Approve order types, timeouts and replacement limits (D08)
- [ ] Create two $10,000 paper accounts for two of the five candidates (FR-13)
- [ ] Run one to two weeks in shadow, then switch to paper
- [ ] Measure latency budgets for feed, candidate, submission and exit (NFR-02)
- [ ] Reconcile broker and evaluation ledgers daily; investigate every gap (AC-11)
- [ ] Rehearse incidents: stale feed, restart mid-position, submission timeout (AC-05, AC-08, AC-09)

**Gate:** isolation, risk, execution and accounting acceptance signed off; no unresolved accounting incidents.

## Step 5 — Five-strategy tournament (Phase 3)

Goal: all five candidates trade paper on independent accounts under one frozen manifest.

- [ ] Close D02: second Alpaca login, staged rollout, or an approved internal-ledger scope change
- [ ] Create the remaining $10,000 paper accounts and bind one per strategy (FR-01)
- [ ] Approve the evaluation and confirmation periods and the statistical decision policy before starting (D09)
- [ ] Approve the cost model, marking rules and outage-exclusion rules (D10)
- [ ] Freeze the manifest and tag the release; no code changes during the evaluation period
- [ ] Confirm all five workers run within measured resource and latency budgets (AC-19)

**Gate:** operational stability across the first full week and a frozen manifest.

## Step 6 — Evaluate and confirm (Phases 4–5)

Goal: a ranked, cost-aware result with an honest read on uncertainty, then a check on unseen data.

**Evaluation (Phase 4)**

- [ ] Rank net return on equal capital over the common period (BRD 12.1)
- [ ] Report the full metric set: drawdown, payoff ratio, trade count, cost share, incident counts (BRD 12.2)
- [ ] Run cost-stress scenarios and block resampling on paired daily outcomes (BRD 12.4)
- [ ] Show every outage and exclusion in the results (AC-17)
- [ ] Apply the promotion gates, with no-trade as a valid answer (FR-12, AC-12)

**Confirmation (Phase 5)**

- [ ] Keep the unchanged versions running over the approved confirmation period
- [ ] Compare confirmation results with tournament results
- [ ] Decide: promote to further testing, continue, or reject

**Gate:** written recommendation backed by calculations, not narrative. Live trading needs a separate BRD (Phase 6).

## Operating rhythm (from Step 4 onward)

**Each trading day**

- Pre-market: Supervisor starts the engine; check the preflight log and startup reconciliation
- During the session: no code changes; respond only to Telegram alerts
- After the close: engine stops; read the agent's daily narrative and the ledger reconciliation
- Evening: code work only, committed and tagged before the next start

**Each week**

- Review incidents and any broker-versus-evaluation ledger gaps
- Check backup success and disk usage on the mini
- Record any rule change as a new version and evaluation segment (FR-09)

## Roadmap at a glance

&#91;embedded content: roadmap · 6 steps, gates and decisions to close\]

Each step closes the listed decisions before its gate. Step 4 is highlighted because it is the first time orders reach a paper account.
