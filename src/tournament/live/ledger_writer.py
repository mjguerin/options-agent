"""After the close: write each account's two ledgers, their reconciliation, and the ranking.

Each account gets a JSON file in the session folder (always, even if the database is down) and a
row set in the append-only ledger tables. Outages and incidents travel with the numbers (AC-17):
the operational block records how many slots were uncertain and which incidents opened, so a
result is never shown without the conditions it was produced under.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

import pandas as pd

from tournament.ledger.config import LedgerConfig
from tournament.ledger.rank import rank_accounts
from tournament.ledger.session import build_session_ledger, load_equity
from tournament.ledger.store import write_session

log = logging.getLogger(__name__)


def incident_counts(alert_store, session_date: date, account: str) -> dict[str, int]:
    """Incidents opened on this session day for this account (and the engine-wide ones)."""
    counts: dict[str, int] = {}
    if alert_store is None:
        return counts
    try:
        for i in alert_store.all_incidents():
            if i.opened_at.tz_convert("UTC").date() != session_date:
                continue
            if i.account in (None, "", account):
                counts[i.kind] = counts.get(i.kind, 0) + 1
    except Exception:  # noqa: BLE001 - the ledger is written even if the incident table isn't
        log.exception("could not count incidents for the ledger")
    return counts


def write_session_ledgers(
    accounts,
    session,
    cfg: LedgerConfig,
    now: pd.Timestamp,
    experiment_id: str,
    mode: str,
    out_dir: Path,
    history_file: Path,
    alert_store=None,
    ledger_engine=None,
) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    bars = pd.DataFrame(session.bars)
    uncertain = (
        int((bars["interval_status"] == "DATA_MISSING_OR_UNCERTAIN").sum()) if len(bars) else 0
    )
    halted = int((bars["interval_status"] == "HALTED").sum()) if len(bars) else 0
    session_date = session.day.session_date
    entries, report = [], {}
    for binding, rt in accounts.router.runtimes.items():
        sim = accounts.brokers[binding]
        body = build_session_ledger(
            rt.store,
            binding,
            cfg,
            session_date=session_date.isoformat(),
            start_equity=rt.start_of_day_equity,
            broker_cash=sim.cash(),
            broker_positions={p.symbol: p.qty for p in sim.positions()},
            open_marks=rt.positions_detail(now),
            equity=load_equity(history_file, binding),
            execution_source="modeled" if mode == "shadow" else "broker",
            operational={
                "uncertain_slots": uncertain,
                "halted_slots": halted,
                "incidents": incident_counts(alert_store, session_date, binding),
                "operator_halt": rt.operator_halt,
                "daily_limit": rt.halted,
            },
        )
        (out_dir / f"{binding.replace(':', '_')}.json").write_text(
            json.dumps(body, indent=1, default=str)
        )
        entries.append(body)
        stored = None
        if ledger_engine is not None:
            try:
                _, revision, created = write_session(ledger_engine, experiment_id, mode, body, now)
                stored = {"revision": revision, "new": created}
            except Exception as exc:  # noqa: BLE001 - the JSON file above is the fallback record
                stored = {"error": f"{type(exc).__name__}: {exc}"}
                log.exception("could not store the ledger for %s", binding)
        report[binding] = {
            "reconciled": body["reconciliation"]["ok"],
            "net_pnl_est_usd": body["summary"]["net_pnl_est_usd"],
            "stored": stored,
        }
    ranking = rank_accounts(entries, cfg)
    (out_dir / "ranking.json").write_text(json.dumps(ranking, indent=1, default=str))
    return {"accounts": report, "ranking": ranking}
