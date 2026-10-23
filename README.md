# Options Strategy Tournament

Five SPY options strategies, each on its own $10,000 Alpaca paper account, compared under one
frozen manifest. Requirements: `options_strategy_tournament_brd_v1.2.pdf`. Work order:
`Options Strategy Tournament — Project Plan.md`.

Paper only. There is no code path to a live endpoint (NFR-06).

## Layout

```
src/tournament/
  adapters/     vendor SDKs (Alpaca) behind our own types
  features/     event normalizer + shared feature engine
  strategies/   strategy contract and the five workers
  accounts/     account runtimes, experiment manager, manifests
  orders/       order manager, positions/exits, reconciliation
  analytics/    evaluation ledger, metrics, uncertainty
  secrets.py    Alpaca keys from .env (mode 600, gitignored)
  db.py         PostgreSQL URL and SQLAlchemy metadata
  cli.py        `tournament` command
migrations/     Alembic migrations (NFR-11)
tests/
```

## Setup on the Mac mini

Run everything in the single working directory on the mini (over SSH or IDE Remote-SSH). Don't
build a `.venv` from the MacBook through the file share.

```bash
uv sync
uv run pytest
```

### Alpaca keys

Keys live in `.env` in the repo root. The file is gitignored and must be mode 600: the loader
refuses a file other users can read, which keeps it from the agent's macOS user (AC-24).

```bash
cp .env.example .env && chmod 600 .env
# edit .env: ALPACA_DEV_KEY_ID / ALPACA_DEV_SECRET_KEY
uv run tournament keys check --account dev
```

Each account alias gets its own pair: `ALPACA_<ALIAS>_KEY_ID` and `ALPACA_<ALIAS>_SECRET_KEY`.
The keys are read into memory only, never exported to the process environment. To use a
different file, set `TOURNAMENT_ENV_FILE`.

### Hello world (Step 1 gate)

```bash
uv run tournament hello
TOURNAMENT_NETWORK_TESTS=1 uv run pytest -m network
```

### Database

PostgreSQL connects over the local socket with peer auth, so there is no DB password to store.
To point somewhere else, set `TOURNAMENT_DATABASE_URL`.

```bash
createdb options_tournament
uv run alembic upgrade head
uv run alembic revision --autogenerate -m "describe change"
```

## Historical market data

There is one shared store under `data/history/` (gitignored), filled by one batch job. Strategies
read from it and never fetch data themselves. It holds one Parquet file per dataset per session
day; a file that exists is complete, and each file records its source, feed and code commit.

| Dataset | Contents |
|---|---|
| `calendar.parquet` | Sessions since Feb 2024, open and close in UTC, early closes |
| `spy_bars_1m` | SIP 1-minute bars, 04:00 to 4h after the close, with a `regular_session` flag |
| `spy_trades` | SIP trades, regular session |
| `option_contracts` | The day's universe: expiries up to 7 days out, strikes within ±5% of the 09:30 open |
| `option_bars_1m` | OPRA 1-minute bars for that universe; `bars_source` says whether they came from Alpaca or were built from trades where Alpaca's bars are missing |

```bash
uv run tournament history download     # Feb 1 2024 to yesterday; skips days on disk
uv run tournament history status
```

The download needs the `data` keys. It refuses to start, and pauses between days, from 09:00 to
16:30 ET on weekdays. Rerun it to resume. Alpaca has no historical option quotes, so replay sees
option prices but not past bid/ask spreads.

## Host operations (`ops/`)

One-time host setup, run on the mini after the close. It sets power and time, makes SSH
key-only, starts PostgreSQL at boot without a login, and schedules the nightly backup:

```bash
sudo ops/host/setup_host.sh
```

The backup runs every night at 20:00: a local `pg_dump` into `~/backups/options-tournament`, kept
for 14 days. There's no off-machine copy by default. Setting `BACKUP_OFFSITE` in
`~/.config/options-tournament/backup.conf` adds one. The log is
`~/Library/Logs/options-tournament/backup.log`. To run it by hand, or to test a restore:

```bash
ops/backup/pg_backup.sh
ops/backup/restore_test.sh
```

## Rules of the road

- Code changes only after the close, with the engine stopped.
- Paper mode starts only from a clean tree whose HEAD matches the manifest tag (NFR-15).
- Secrets live only in `.env` (mode 600). They never go in git, env vars, logs or agent context.
