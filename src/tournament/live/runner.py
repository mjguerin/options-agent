"""Run the shared feed for one session and record it (Step 3 shadow data collection).

Writes to data/live/<session date>/<mode>/:
- stream.jsonl: every raw frame with its receive time (audit; later replay with arrival times)
- bars.parquet: the finalized bars with live counters
- summary.json: counts, feed-health and halt intervals, pre-market levels
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import yaml

from tournament.live.session import LiveSession
from tournament.live.stream import AlpacaStockStream
from tournament.marketdata.calendar import SessionDay
from tournament.marketdata.eligibility import EligibilityConfig

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG = REPO_ROOT / "config" / "live" / "feed-v1.yaml"
COMMON = REPO_ROOT / "config" / "strategies" / "common-v1.yaml"
log = logging.getLogger(__name__)


def load_feed_config(path: Path = CONFIG) -> dict:
    return yaml.safe_load(path.read_text())


def synthetic_session(minutes: int, now: datetime) -> SessionDay:
    """A synthetic session for the test stream: starts at the next whole minute."""
    open_utc = (now + timedelta(minutes=1)).replace(second=0, microsecond=0)
    return SessionDay(open_utc.date(), open_utc, open_utc + timedelta(minutes=minutes))


def run_session(
    day: SessionDay, mode: str, key_id: str, secret: str, out_root: Path
) -> LiveSession:
    fc = load_feed_config()
    delay_s = yaml.safe_load(COMMON.read_text())["clock"]["finalize_delay_s"]
    test = mode == "test"
    symbol = fc["test_symbol"] if test else fc["symbol"]
    out = out_root / day.session_date.isoformat() / mode
    out.mkdir(parents=True, exist_ok=True)
    session = LiveSession(
        day,
        EligibilityConfig(),
        symbol=symbol,
        rules_symbol=fc["symbol"],
        finalize_delay=timedelta(seconds=delay_s),
        stale_after=timedelta(seconds=fc["test_stale_after_s" if test else "stale_after_s"]),
        on_bar=lambda r: log.info(
            "slot %s %s close=%s trades=%s",
            r["slot"],
            r["interval_status"],
            r["close"],
            r["trades_total"],
        ),  # fmt: skip
    )
    until = pd.Timestamp(day.close_utc) + session.delay + pd.Timedelta(seconds=1)
    with (out / "stream.jsonl").open("a") as journal:

        def record(raw: str, received: pd.Timestamp) -> None:
            journal.write(f'{{"r":"{received.isoformat()}","m":{raw}}}\n')

        stream = AlpacaStockStream(
            fc["test_url"] if test else fc["sip_url"],
            key_id,
            secret,
            [symbol],
            mode=mode,
            on_messages=session.on_messages,
            on_connected=session.on_connected,
            on_disconnected=session.on_disconnected,
            on_tick=session.tick,
            on_raw=record,
            tick_s=fc["tick_s"],
            backoff_s=tuple(fc["reconnect_backoff_s"]),
        )
        asyncio.run(stream.run(until))
    session.tick(until)
    write_outputs(session, out, mode, stream.connects)
    return session


def write_outputs(session: LiveSession, out: Path, mode: str, connects: int) -> None:
    bars = pd.DataFrame(session.bars)
    if not bars.empty:
        bars["late_adjustment"] = bars["late_adjustment"].map(list)
        bars.to_parquet(out / "bars.parquet", index=False)

    def iv(intervals):
        return [
            [str(s) if s.year > 1 else None, None if e is None else str(e), r]
            for s, e, r in intervals
        ]

    pm = session.premarket_levels
    summary = {
        "mode": mode,
        "session_date": session.day.session_date.isoformat(),
        "symbol": session.symbol,
        "slots_finalized": session.builder.finalized,
        "status_counts": bars["interval_status"].value_counts().to_dict() if len(bars) else {},
        "counts": session.counts,
        "builder_counts": session.builder._counts,
        "connects": connects,
        "unhealthy": iv(session.health.unhealthy),
        "halts": iv(session.health.halts),
        "premarket": asdict(pm) if pm else None,
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=1, default=str))


def today_session(calendar_fn, today: date) -> SessionDay | None:
    days = calendar_fn(today, today)
    return days[0] if days and days[0].session_date == today else None
