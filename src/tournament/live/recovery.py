"""Recovering a session after a crash or restart (D-67).

A restart must not erase exposure, losses, quotas, brakes, cooldowns, pending orders or reference
positions. They live in two places that both survive a crash:
- the execution side (orders, fills, reservations, halts) in the day's durable accounts database;
- the strategy side (bars, features, every worker's state, reference positions) is a
  deterministic function of the SPY frames received so far, and every frame is journaled with its
  receive time. Replaying the journal through the same session code, with no accounts attached,
  rebuilds that state exactly, because the strategy layer never depends on execution (D-33).

The stretch between the last journaled frame and the reconnect is marked uncertain, like any
outage: a crash is a gap in the feed, and never silently a quiet market.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pandas as pd

from tournament.live.session import LiveSession

log = logging.getLogger(__name__)


def previous_runs(day_dir: Path, mode: str) -> list[Path]:
    """Earlier engine starts of this session day, oldest first (shadow, shadow-r2, ...)."""
    runs = [p for p in day_dir.glob(f"{mode}*") if (p / "stream.jsonl").exists()]

    def order(p: Path) -> int:
        tail = p.name.removeprefix(mode).removeprefix("-r")
        return int(tail) if tail.isdigit() else 1

    return sorted((p for p in runs if p.name == mode or p.name.startswith(f"{mode}-r")), key=order)


def read_journal(path: Path):
    """(received_at, kind, payload) for each line; a torn last line is skipped, not fatal."""
    with path.open() as f:
        for line in f:
            try:
                row = json.loads(line)
                ts = pd.Timestamp(row["r"])
            except (ValueError, KeyError):
                log.warning("skipping an unreadable journal line in %s", path)
                continue
            if "event" in row:
                yield ts, row["event"], None
            else:
                yield ts, "frame", row["m"]


def replay_journals(session: LiveSession, runs: list[Path]) -> dict:
    """Feed every earlier frame and connection event back through the session, in order."""
    frames = events = 0
    last: pd.Timestamp | None = None
    for run in runs:
        for ts, kind, payload in read_journal(run / "stream.jsonl"):
            if kind == "connected":
                session.on_connected(ts)
                events += 1
            elif kind == "disconnected":
                session.on_disconnected(ts)
                events += 1
            else:
                # the live loop ticks every few hundred milliseconds; one tick just before each
                # frame reproduces what it would have seen (due slots finalized, silence noticed)
                session.tick(ts - pd.Timedelta(milliseconds=1))
                session.on_messages(payload, ts)
                frames += 1
            last = ts
    return {"frames": frames, "events": events, "last_at": last}


def mark_outage_from(session: LiveSession, last_at: pd.Timestamp | None) -> None:
    """The process was gone from the last thing it recorded until it reconnects."""
    if last_at is not None:
        session.on_disconnected(last_at)
