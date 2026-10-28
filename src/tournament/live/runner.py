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

from tournament.alerts.bot import heartbeat_path
from tournament.alerts.config import load_alerts_config
from tournament.alerts.incidents import Incidents
from tournament.alerts.monitor import SessionMonitor
from tournament.api.config import load_api_config
from tournament.api.store import ControlStore
from tournament.appconfig import app_config
from tournament.features.pipeline import calendar_days, carry_context
from tournament.ledger.config import load_ledger_config
from tournament.live.controls import ControlPlane
from tournament.live.engine import ShadowEngine
from tournament.live.ledger_writer import write_session_ledgers
from tournament.live.options_runner import OptionsCollector
from tournament.live.recovery import mark_outage_from, previous_runs, replay_journals
from tournament.live.session import LiveSession
from tournament.live.shadow_accounts import ShadowAccounts, has_exposure, shadow_db_path
from tournament.live.stream import AlpacaStockStream
from tournament.marketdata.calendar import SessionDay
from tournament.marketdata.eligibility import EligibilityConfig, Usage
from tournament.marketdata.store import HistoryStore

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG = REPO_ROOT / "config" / "live" / "feed-v1.yaml"
COMMON = REPO_ROOT / "config" / "strategies" / "common-v1.yaml"
log = logging.getLogger(__name__)


def unique_run_dir(day_dir: Path, mode: str) -> Path:
    """One directory per engine start: a restart after a crash never overwrites the outputs of
    the run before it (shadow, shadow-r2, ...)."""
    n = 1
    while True:
        out = day_dir / (mode if n == 1 else f"{mode}-r{n}")
        if not (out / "stream.jsonl").exists():
            out.mkdir(parents=True, exist_ok=True)
            return out
        n += 1


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
    session: LiveSession, store: HistoryStore | None, day: SessionDay, run_id: str
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
    # one run identity for the whole session day, so a restart recreates the same candidate IDs
    return ShadowEngine(session, all_workers(), prior, baseline, usage=Usage.LIVE, run_id=run_id)


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
    fresh: bool = False,
) -> LiveSession:
    key_id, secret = creds.key_id, creds.secret_key
    fc = load_feed_config()
    delay_s = yaml.safe_load(COMMON.read_text())["clock"]["finalize_delay_s"]
    test = mode == "test"
    underlying = app_config().underlying
    symbol = fc["test_symbol"] if test else underlying
    day_dir = out_root / day.session_date.isoformat()
    # earlier starts of this same session day (a crash or a restart), unless this run is a
    # deliberate fresh simulation under its own identity
    earlier = [] if fresh else previous_runs(day_dir, mode)
    out = unique_run_dir(day_dir, mode)
    run_id = f"live-{day.session_date}-{mode}" + (
        f"-fresh-{datetime.now():%H%M%S}" if fresh else ""
    )
    session = LiveSession(
        day,
        EligibilityConfig(),
        symbol=symbol,
        rules_symbol=underlying,
        finalize_delay=timedelta(seconds=delay_s),
        stale_after=timedelta(seconds=fc["test_stale_after_s" if test else "stale_after_s"]),
        halt_codes=frozenset(fc["halt_status_codes"]),
        resume_codes=frozenset(fc["resume_status_codes"]),
        informational_codes=frozenset(fc["informational_status_codes"]),
        unknown_status_hold=timedelta(seconds=fc["unknown_status_hold_s"]),
        on_bar=lambda r: log.info(
            "slot %s %s close=%s trades=%s",
            r["slot"],
            r["interval_status"],
            r["close"],
            r["trades_total"],
        ),  # fmt: skip
    )
    acfg = incidents = None
    if mode == "shadow" and fc["alerts"]["enabled"]:
        acfg = load_alerts_config()
        incidents = Incidents.from_config(acfg)
    manage_only = False
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
            now = pd.Timestamp.now(tz="UTC")
            names = ", ".join(c.name for c in result.failures if c.blocking)
            if incidents is not None:
                incidents.open(now, "PREFLIGHT_FAILED", names)
            # a failed preflight blocks new entries; it never abandons positions already held
            if not has_exposure(shadow_db_path(day_dir, fresh)):
                raise PreflightFailed(preflight_report)
            manage_only = True
            log.error("preflight failed but exposure exists: managing it, no new entries")
    engine = make_engine(session, store, day, run_id) if mode == "shadow" else None
    recovered = None
    if earlier:
        # rebuild the strategy side from the journals: bars, features, worker state, reference
        # positions. No accounts are attached yet, so nothing is ordered during the replay.
        try:
            recovered = replay_journals(session, earlier)
            mark_outage_from(session, recovered["last_at"])
            log.info(
                "recovered %s frames from %d earlier start(s)", recovered["frames"], len(earlier)
            )
        except Exception as exc:  # noqa: BLE001 - say so; accounts below halt rather than guess
            log.exception("journal replay failed: %s", exc)
            recovered = {"error": f"{type(exc).__name__}: {exc}"}
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
                shadow_db_path(day_dir, fresh),
                sa["tick_every_s"],
                sa["reconcile_every_s"],
            )
            now0 = pd.Timestamp.now(tz="UTC")
            if recovered is not None and "error" in recovered:
                for rt in accounts.router.runtimes.values():
                    rt.operator_halt = "RECOVERY_FAILED"  # strategy state is unknown: no entries
                accounts.errors.append(f"journal replay: {recovered['error']}")
            accounts.recover(now0, engine.harness.closed_references())
            if manage_only:
                for rt in accounts.router.runtimes.values():
                    rt.operator_halt = "PREFLIGHT_FAILED"
            collector.held_symbols = accounts.held_symbols
            accounts.attach(engine.harness)
        except Exception as exc:  # noqa: BLE001 - recording must go on without the accounts
            log.exception("shadow accounts not started: %s", exc)
            accounts = None
    monitor = None
    if incidents is not None:
        monitor = SessionMonitor(
            incidents,
            acfg,
            session,
            heartbeat_path(REPO_ROOT, acfg.heartbeat.file),
            mode,
            collector,
            engine,
            accounts,
        )

    control = None
    if accounts is not None and fc["controls"]["enabled"]:
        try:
            from tournament.orders.store import default_engine

            api_cfg = load_api_config()
            try:
                queue = ControlStore(default_engine())
            except Exception:  # noqa: BLE001 - state is still published; commands just can't arrive
                log.exception("control queue unavailable")
                queue = None
            control = ControlPlane(
                queue,
                api_cfg,
                accounts.router.runtimes,
                heartbeat_path(REPO_ROOT, api_cfg.state.file),
                session,
                mode,
                collector,
                engine,
            )
        except Exception:  # noqa: BLE001
            log.exception("controls not started")

    def on_tick(now: pd.Timestamp) -> None:
        session.tick(now)
        if control is not None:
            control.tick(now)  # commands first: an operator pause beats a new entry
        if accounts is not None:
            accounts.tick(now)
        if monitor is not None:
            monitor.tick(now)

    if collector is not None:
        previous = session.on_bar

        def on_bar(row: dict) -> None:
            if previous:
                previous(row)
            collector.on_bar(row)

        session.on_bar = on_bar
    # through the close, then long enough to settle orders and reconcile (BRD 10.6)
    until = (
        pd.Timestamp(day.close_utc)
        + session.delay
        + pd.Timedelta(seconds=fc["shutdown_grace_s"] + app_config().engine.post_close_s)
    )
    with (out / "stream.jsonl").open("a") as journal:

        def record(msgs: list[dict], received: pd.Timestamp) -> None:
            journal.write(json.dumps({"r": received.isoformat(), "m": msgs}) + "\n")
            journal.flush()  # a crash loses nothing the process has already seen

        def event(kind: str, at: pd.Timestamp) -> None:
            journal.write(json.dumps({"r": at.isoformat(), "event": kind}) + "\n")
            journal.flush()

        def on_connected(at: pd.Timestamp) -> None:
            event("connected", at)
            session.on_connected(at)

        def on_disconnected(at: pd.Timestamp) -> None:
            event("disconnected", at)
            session.on_disconnected(at)

        stream = AlpacaStockStream(
            "test" if test else "sip",
            key_id,
            secret,
            [symbol],
            mode=mode,
            on_messages=session.on_messages,
            on_connected=on_connected,
            on_disconnected=on_disconnected,
            on_tick=on_tick,
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

        try:
            asyncio.run(main())
        finally:
            # a crash or a stop signal still leaves the bars, summary and records written so far
            now_end = pd.Timestamp.now(tz="UTC")
            session.tick(min(until, now_end))
            reconciliation = None
            if accounts is not None and now_end >= pd.Timestamp(day.close_utc):
                reconciliation = accounts.post_close(now_end)
                if incidents is not None:
                    for binding, r in reconciliation.items():
                        bad = (
                            r.get("error")
                            or r.get("position_mismatches")
                            or r.get("unresolved_orders")
                        )
                        if bad:
                            incidents.open(
                                now_end, "RECONCILIATION_FAILED", str(bad)[:200], binding
                            )
            ledgers = None
            if accounts is not None and now_end >= pd.Timestamp(day.close_utc):
                ledgers = store_ledgers(
                    accounts, session, now_end, manifest, mode, out, incidents, day_dir.parent
                )
            write_outputs(
                session,
                out,
                mode,
                stream.connects,
                engine,
                collector,
                accounts,
                preflight_report,
                {
                    "recovered": recovered,
                    "reconciliation": reconciliation,
                    "fresh": fresh,
                    "ledgers": ledgers,
                },
            )
            if ledgers is not None and "error" not in ledgers:
                store_trade_paths(out, pd.Timestamp.now(tz="UTC"), incidents)
            if monitor is not None:
                monitor.tick(pd.Timestamp.now(tz="UTC"))
                monitor.finish(pd.Timestamp.now(tz="UTC"))
    return session


def store_ledgers(accounts, session, now, manifest, mode, out, incidents, live_root) -> dict | None:
    """Both ledgers for every account, to JSON beside the session and to the ledger tables. A
    failure to store is reported, never allowed to stop the rest of the shutdown."""
    try:
        from tournament.orders.store import default_engine

        cfg = load_ledger_config()
        experiment = manifest.experiment_id if manifest is not None else "unregistered"
        api_cfg = load_api_config()
        try:
            engine = default_engine()
        except Exception:  # noqa: BLE001
            engine = None
        result = write_session_ledgers(
            accounts,
            session,
            cfg,
            now,
            experiment,
            mode,
            out / "ledger",
            heartbeat_path(REPO_ROOT, api_cfg.state.file).parent
            / "state_history"
            / f"{session.day.session_date.isoformat()}.jsonl",
            incidents.store if incidents is not None else None,
            engine,
        )
        if incidents is not None:
            for binding, r in result["accounts"].items():
                if not r["reconciled"]:
                    incidents.open(
                        now, "RECONCILIATION_FAILED", "ledger identities do not hold", binding,
                        key=f"RECONCILIATION_FAILED:{binding}:ledger",
                    )  # fmt: skip
                if r["stored"] and "error" in r["stored"]:
                    incidents.open(now, "LEDGER_FAILED", r["stored"]["error"][:160], binding)
        return {
            "accounts": result["accounts"],
            "leader": result["ranking"][0] if result["ranking"] else None,
        }
    except Exception as exc:  # noqa: BLE001
        log.exception("ledgers not written")
        return {"error": f"{type(exc).__name__}: {exc}"}


def store_trade_paths(out: Path, now, incidents) -> dict | None:
    """After the session's files are saved: where each trade went. A reading aid, so a failure is
    reported and nothing else depends on it."""
    try:
        from tournament.ledger.paths import build_paths

        paths = build_paths(out, load_ledger_config())
        return {"trades": sum(len(v) for v in paths.values())}
    except Exception as exc:  # noqa: BLE001
        log.exception("trade paths not written")
        if incidents is not None:
            incidents.open(now, "LEDGER_PATHS_FAILED", f"{type(exc).__name__}: {exc}"[:160])
        return {"error": f"{type(exc).__name__}: {exc}"}


def write_outputs(
    session: LiveSession,
    out: Path,
    mode: str,
    connects: int,
    engine: ShadowEngine | None = None,
    collector: OptionsCollector | None = None,
    accounts: ShadowAccounts | None = None,
    preflight_report: dict | None = None,
    lifecycle: dict | None = None,
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
    if lifecycle is not None:
        summary["lifecycle"] = lifecycle
    (out / "summary.json").write_text(json.dumps(summary, indent=1, default=str))


def today_session(calendar_fn, today: date) -> SessionDay | None:
    days = calendar_fn(today, today)
    return days[0] if days and days[0].session_date == today else None
