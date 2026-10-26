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

from tournament.appconfig import app_config
from tournament.features.pipeline import calendar_days, carry_context
from tournament.live.engine import ShadowEngine
from tournament.live.options_runner import OptionsCollector
from tournament.live.session import LiveSession
from tournament.live.shadow_accounts import ShadowAccounts, shadow_db_path
from tournament.live.stream import AlpacaStockStream
from tournament.marketdata.calendar import SessionDay
from tournament.marketdata.eligibility import EligibilityConfig, Usage
from tournament.marketdata.store import HistoryStore

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG = REPO_ROOT / "config" / "live" / "feed-v1.yaml"
COMMON = REPO_ROOT / "config" / "strategies" / "common-v1.yaml"
log = logging.getLogger(__name__)


class PreflightFailed(RuntimeError):
    def __init__(self, report: dict) -> None:
        super().__init__(f"preflight failed: {report['failures']}")
        self.report = report


def load_feed_config(path: Path = CONFIG) -> dict:
    return yaml.safe_load(path.read_text())


def synthetic_session(minutes: int, now: datetime, lead_min: int) -> SessionDay:
    """A synthetic session for the test stream: starts `lead_min` minutes ahead, on the minute."""
    open_utc = (now + timedelta(minutes=lead_min)).replace(second=0, microsecond=0)
    return SessionDay(open_utc.date(), open_utc, open_utc + timedelta(minutes=minutes))


def all_workers() -> list:
    from tournament.strategies.brt import BRT
    from tournament.strategies.fbr import FBR
    from tournament.strategies.orb import ORB
    from tournament.strategies.vmr import VMR
    from tournament.strategies.vpc import VPC

    return [VPC(), ORB(), BRT(), FBR(), VMR()]


def make_engine(
    session: LiveSession, store: HistoryStore | None, day: SessionDay
) -> ShadowEngine | None:
    """The shadow engine needs the context of the previous session: the store must hold every
    session before today. Otherwise the feed is recorded without decisions (and says why)."""
    if store is None:
        return None
    earlier = [d.session_date for d in calendar_days(store) if d.session_date < day.session_date]
    if not earlier or not store.has("spy_trades", earlier[-1]):
        log.warning("history is missing the session before %s: recording without decisions",
                    day.session_date)  # fmt: skip
        return None
    prior, baseline = carry_context(store, Usage.LIVE, day.session_date)
    return ShadowEngine(session, all_workers(), prior, baseline, usage=Usage.LIVE)


def latest_spy_price(creds, feed: str, symbol: str) -> float:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockLatestTradeRequest

    client = StockHistoricalDataClient(creds.key_id, creds.secret_key)
    return float(
        client.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=symbol, feed=feed))[
            symbol
        ].price
    )


def run_session(
    day: SessionDay,
    mode: str,
    creds,
    out_root: Path,
    store: HistoryStore | None = None,
    options: bool = True,
    manifest=None,
) -> LiveSession:
    key_id, secret = creds.key_id, creds.secret_key
    fc = load_feed_config()
    delay_s = yaml.safe_load(COMMON.read_text())["clock"]["finalize_delay_s"]
    test = mode == "test"
    underlying = app_config().underlying
    symbol = fc["test_symbol"] if test else underlying
    out = out_root / day.session_date.isoformat() / mode
    out.mkdir(parents=True, exist_ok=True)
    session = LiveSession(
        day,
        EligibilityConfig(),
        symbol=symbol,
        rules_symbol=underlying,
        finalize_delay=timedelta(seconds=delay_s),
        stale_after=timedelta(seconds=fc["test_stale_after_s" if test else "stale_after_s"]),
        halt_codes=frozenset(fc["halt_status_codes"]),
        resume_codes=frozenset(fc["resume_status_codes"]),
        on_bar=lambda r: log.info(
            "slot %s %s close=%s trades=%s",
            r["slot"],
            r["interval_status"],
            r["close"],
            r["trades_total"],
        ),  # fmt: skip
    )
    preflight_report = None
    if manifest is not None:
        from tournament.accounts.manifest import preflight

        result = preflight(manifest)
        preflight_report = {
            "manifest_hash": result.manifest_hash,
            "commit_sha": result.commit_sha,
            "passed": result.passed,
            "failures": [[c.name, c.detail, c.blocking] for c in result.failures],
        }
        for c in result.failures:
            log.warning("preflight %s: %s %s", "FAIL" if c.blocking else "warn", c.name, c.detail)
        if not result.passed:
            raise PreflightFailed(preflight_report)
    engine = make_engine(session, store, day) if mode == "shadow" else None
    collector = None
    if mode == "shadow" and options:
        from tournament.adapters.alpaca_history import AlpacaHistory

        try:
            collector = OptionsCollector(
                fc["options"],
                fc,
                creds,
                day,
                mode,
                latest_spy_price(creds, fc["spot_feed"], underlying),
                AlpacaHistory(creds),
            )
        except Exception as exc:  # noqa: BLE001 - the SPY recording must go on without options
            log.exception("option feed not started: %s", exc)
    accounts = None
    sa = fc["shadow_accounts"]
    if engine is not None and collector is not None and sa["enabled"]:
        sess = yaml.safe_load(COMMON.read_text())["session"]
        try:
            accounts = ShadowAccounts(
                day,
                collector,
                [w.strategy_id for w in engine.harness.workers],
                sa["starting_balance_usd"],
                sess["cutoff_min_before_close"],
                shadow_db_path(out),
                sa["tick_every_s"],
            )
            accounts.attach(engine.harness)
        except Exception as exc:  # noqa: BLE001 - recording must go on without the accounts
            log.exception("shadow accounts not started: %s", exc)
            accounts = None
    if collector is not None:
        previous = session.on_bar

        def on_bar(row: dict) -> None:
            if previous:
                previous(row)
            collector.on_bar(row)

        session.on_bar = on_bar
    until = (
        pd.Timestamp(day.close_utc) + session.delay + pd.Timedelta(seconds=fc["shutdown_grace_s"])
    )
    with (out / "stream.jsonl").open("a") as journal:

        def record(msgs: list[dict], received: pd.Timestamp) -> None:
            journal.write(json.dumps({"r": received.isoformat(), "m": msgs}) + "\n")

        stream = AlpacaStockStream(
            "test" if test else "sip",
            key_id,
            secret,
            [symbol],
            mode=mode,
            on_messages=session.on_messages,
            on_connected=session.on_connected,
            on_disconnected=session.on_disconnected,
            on_tick=(lambda now: (session.tick(now), accounts.tick(now)))
            if accounts is not None
            else session.tick,
            on_frame=record,
            tick_s=fc["tick_s"],
            backoff_s=tuple(fc["reconnect_backoff_s"]),
            ping_interval_s=fc["ping_interval_s"],
            handshake_timeout_s=fc["handshake_timeout_s"],
        )

        async def main() -> None:
            tasks = [stream.run(until)]
            if collector is not None:
                tasks.append(collector.run(until))
            await asyncio.gather(*tasks)

        asyncio.run(main())
    session.tick(until)
    write_outputs(
        session, out, mode, stream.connects, engine, collector, accounts, preflight_report
    )
    return session


def write_outputs(
    session: LiveSession,
    out: Path,
    mode: str,
    connects: int,
    engine: ShadowEngine | None = None,
    collector: OptionsCollector | None = None,
    accounts: ShadowAccounts | None = None,
    preflight_report: dict | None = None,
) -> None:
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
    if engine is not None:
        result = engine.result()
        result.write(out / "records")
        lat = engine.latencies
        summary["engine"] = {
            "error": engine.error,
            "slots_decided": engine.stepped,
            "run_id": result.manifest["run_id"],
            "counts": result.manifest["counts"],
            "step_latency_ms_p50_max": [
                round(1000 * sorted(lat)[len(lat) // 2]),
                round(1000 * max(lat)),
            ]
            if lat
            else None,
        }
    if collector is not None:
        summary["options"] = collector.write(out)
    if accounts is not None:
        summary["accounts"] = accounts.summary()
    if preflight_report is not None:
        summary["preflight"] = preflight_report
    (out / "summary.json").write_text(json.dumps(summary, indent=1, default=str))


def today_session(calendar_fn, today: date) -> SessionDay | None:
    days = calendar_fn(today, today)
    return days[0] if days and days[0].session_date == today else None
