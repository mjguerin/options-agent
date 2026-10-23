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

## Rules of the road

- Code changes only after the close, with the engine stopped.
- Paper mode starts only from a clean tree whose HEAD matches the manifest tag (NFR-15).
- Secrets live only in `.env` (mode 600). They never go in git, env vars, logs or agent context.
