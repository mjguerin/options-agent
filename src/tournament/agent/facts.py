"""The daily facts the narrative is written from (AG-06, AG-10).

Everything in the facts file is produced by the accounting code from the ledgers on disk and the
incident table: the agent explains these numbers, it never computes them. The file carries a
content hash (generated_at is left out of it) and the calculation versions it came from, so a
narrative cites exactly what it was written from.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import pandas as pd
from sqlalchemy import select
from sqlalchemy.engine import Engine

from tournament.alerts.store import incidents
from tournament.dashboard.data import cumulative, load_ledgers

FACTS_VERSION = "facts-v1"


def _clean(v):
    """JSON-safe: NaN and numpy values become plain Python."""
    if isinstance(v, dict):
        return {str(k): _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    if hasattr(v, "item"):
        v = v.item()
    if isinstance(v, float) and v != v:
        return None
    return v


def _local(v, tz) -> pd.Timestamp:
    t = pd.Timestamp(v)  # a database without time zones hands back UTC wall-clock times
    return (t.tz_localize("UTC") if t.tzinfo is None else t).tz_convert(tz)


def _incidents_on(engine: Engine, day: date, tz) -> list[dict]:
    with engine.connect() as c:
        rows = c.execute(select(incidents).order_by(incidents.c.id)).all()
    out = []
    for r in rows:
        opened = _local(r.opened_at, tz)
        if opened.date() != day:
            continue
        closed = None if r.closed_at is None else _local(r.closed_at, tz)
        out.append(
            {
                "id": r.id,
                "kind": r.kind,
                "severity": r.severity,
                "component": r.component,
                "account": r.account,
                "detail": r.detail,
                "opened_at": opened.isoformat(),
                "closed_at": None if closed is None else closed.isoformat(),
            }
        )
    return out


def build_facts(day: date, live_root: Path, engine: Engine | None, tz, sessions_back: int) -> dict:
    ledgers = [
        b for b in load_ledgers(live_root) if b["summary"]["session_date"] <= day.isoformat()
    ]
    dates = sorted({b["summary"]["session_date"] for b in ledgers})[-sessions_back:]
    ledgers = [b for b in ledgers if b["summary"]["session_date"] in dates]
    today = [b for b in ledgers if b["summary"]["session_date"] == day.isoformat()]
    cum = cumulative(ledgers)
    facts = {
        "facts_version": FACTS_VERSION,
        "session_date": day.isoformat(),
        "mode": "shadow",
        "has_session": bool(today),
        "sessions_covered": dates,
        "today": [
            {
                "summary": b["summary"],
                "metrics": b.get("metrics"),
                "reconciliation": b["reconciliation"],
                "trades": b.get("trades"),
            }
            for b in today
        ],
        "cumulative": [] if cum.empty else cum.to_dict("records"),
        "incidents": [] if engine is None else _incidents_on(engine, day, tz),
        "notes": [
            "Shadow mode: fills are modeled, not broker fills.",
            "Candidate and rejection records are in the replay and journal files, not here.",
        ],
    }
    facts = _clean(facts)
    blob = json.dumps(facts, sort_keys=True, separators=(",", ":"), default=str)
    facts["content_hash"] = hashlib.sha256(blob.encode()).hexdigest()
    return facts


def render_markdown(f: dict) -> str:
    lines = [f"# Facts for {f['session_date']} ({f['mode']})", ""]
    lines.append(f"content hash `{f['content_hash']}`; facts version `{f['facts_version']}`")
    if not f["has_session"]:
        lines += ["", "No session ledger exists for this date."]
    for a in f["today"]:
        s = a["summary"]
        lines += [
            "",
            f"## {s['account']}",
            f"- trades {s['trades']}, net realized ${s['net_realized_eval_usd']:.2f}, "
            f"net P&L estimate ${s['net_pnl_est_usd']:.2f}",
            f"- books reconcile: {a['reconciliation']['ok']}",
        ]
    if f["cumulative"]:
        lines += ["", "## Cumulative"]
        lines += [f"- {json.dumps(r, sort_keys=True)}" for r in f["cumulative"]]
    lines += ["", "## Incidents"]
    lines += [f"- {json.dumps(i, sort_keys=True)}" for i in f["incidents"]] or ["- none"]
    return "\n".join(lines) + "\n"


def write_facts(facts: dict, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = out_dir / facts["session_date"]
    paths = [stem.with_suffix(".json"), stem.with_suffix(".md")]
    paths[0].write_text(json.dumps(facts, indent=1, sort_keys=True, default=str) + "\n")
    paths[1].write_text(render_markdown(facts))
    return paths
