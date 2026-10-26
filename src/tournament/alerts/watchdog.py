"""Checks the bot runs itself, about things a dead engine cannot report (AG-09, 10.5).

Each check returns findings: a kind, a key, whether the problem is active now, and a detail. The
bot opens an incident for an active finding with no open incident, and resolves an incident whose
finding is no longer active. Pure functions of their inputs, so tests set the clock and the files.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from sqlalchemy import text

from tournament.alerts.config import Watchdog


@dataclass(frozen=True)
class Finding:
    kind: str
    active: bool
    detail: str = ""

    @property
    def key(self) -> str:
        return self.kind


def read_heartbeat(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def check_engine(
    now: pd.Timestamp,
    session_date: date | None,
    start_at: pd.Timestamp | None,
    close_at: pd.Timestamp | None,
    heartbeat: dict | None,
    cfg: Watchdog,
) -> list[Finding]:
    """During a session, after the engine's grace period: the engine must be beating."""
    none = [Finding("ENGINE_NOT_STARTED", False), Finding("ENGINE_SILENT", False)]
    if session_date is None or start_at is None or close_at is None:
        return none
    deadline = start_at + pd.Timedelta(seconds=cfg.engine_grace_after_start_s)
    if now < deadline or now > close_at:
        return none
    mine = heartbeat is not None and heartbeat.get("session") == session_date.isoformat()
    if not mine:
        return [Finding("ENGINE_NOT_STARTED", True, deadline.strftime("%H:%M UTC")), none[1]]
    age = (now - pd.Timestamp(heartbeat["at"])).total_seconds()
    if age > cfg.engine_heartbeat_max_age_s:
        return [none[0], Finding("ENGINE_SILENT", True, f"{age:.0f} s")]
    return none


def last_weekday_due(now: pd.Timestamp, tz, start_hhmm: str, deadline_s: float) -> date:
    """The most recent weekday whose nightly job should already have reported."""
    h, m = (int(x) for x in start_hhmm.split(":"))
    local = now.tz_convert(tz)
    d = local.date()
    while True:
        due = pd.Timestamp(d).tz_localize(tz) + pd.Timedelta(hours=h, minutes=m, seconds=deadline_s)
        if d.weekday() < 5 and due <= local:
            return d
        d -= timedelta(days=1)


def check_nightly(due_date: date, ops_dir: Path) -> list[Finding]:
    path = ops_dir / f"nightly_{due_date.isoformat()}.json"
    try:
        s = json.loads(path.read_text())
    except (OSError, ValueError):
        return [Finding("NIGHTLY_FAILED", True, f"no report for {due_date}")]
    if s.get("error") or s.get("missing"):
        gaps = len(s.get("missing") or {})
        return [
            Finding(
                "NIGHTLY_FAILED",
                True,
                f"{due_date}: error={s.get('error')}, {gaps} sessions with gaps",
            )
        ]
    return [Finding("NIGHTLY_FAILED", False)]


def check_backup(now: pd.Timestamp, log: Path, cfg: Watchdog) -> list[Finding]:
    try:
        age_h = (now.timestamp() - log.stat().st_mtime) / 3600
    except OSError:
        return [Finding("BACKUP_STALE", True, f"no backup log at {log}")]
    if age_h > cfg.backup_max_age_h:
        return [Finding("BACKUP_STALE", True, f"last run {age_h:.0f} h ago")]
    return [Finding("BACKUP_STALE", False)]


def check_disk(path: Path, cfg: Watchdog, usage=shutil.disk_usage) -> list[Finding]:
    free = usage(path).free / 1e9
    if free < cfg.min_free_disk_gb:
        return [
            Finding("DISK_LOW", True, f"{free:.0f} GB free, minimum {cfg.min_free_disk_gb:.0f}")
        ]
    return [Finding("DISK_LOW", False)]


def check_database(engine) -> list[Finding]:
    try:
        with engine.connect() as c:
            c.execute(text("select 1"))
    except Exception as exc:  # noqa: BLE001 - any failure to answer is the finding
        return [Finding("DATABASE_DOWN", True, type(exc).__name__)]
    return [Finding("DATABASE_DOWN", False)]
