"""Command line entry point: `tournament keys check`, `hello`, `data-check`, `history`, `feed`."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from tournament.appconfig import app_config
from tournament.secrets import (
    InsecureEnvFileError,
    MissingCredentialsError,
    env_file,
    load_alpaca_credentials,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _keys_check(args: argparse.Namespace) -> int:
    try:
        load_alpaca_credentials(args.account)
    except (MissingCredentialsError, InsecureEnvFileError) as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"{env_file()} has Alpaca credentials for {args.account!r}.")
    return 0


def _hello(args: argparse.Namespace) -> int:
    from tournament.adapters.alpaca import get_account_summary, paper_trading_client

    try:
        creds = load_alpaca_credentials(args.account)
    except (MissingCredentialsError, InsecureEnvFileError) as exc:
        print(exc, file=sys.stderr)
        return 1
    summary = get_account_summary(paper_trading_client(creds))
    print(f"Alpaca paper account {summary.account_number_masked} ({args.account})")
    print(f"  status:         {summary.status}")
    print(f"  equity:         {summary.equity} {summary.currency}")
    print(f"  cash:           {summary.cash}")
    print(f"  buying power:   {summary.buying_power}")
    print(f"  options level:  {summary.options_trading_level}")
    print(f"  trading blocked: {summary.trading_blocked}")
    return 0


def _data_check(args: argparse.Namespace) -> int:
    from tournament.adapters.alpaca import check_data_entitlements, data_clients

    try:
        creds = load_alpaca_credentials(args.account)
    except (MissingCredentialsError, InsecureEnvFileError) as exc:
        print(exc, file=sys.stderr)
        return 1
    checks = check_data_entitlements(*data_clients(creds))
    for check in checks:
        print(f"  {check.feed:5} {'OK  ' if check.ok else 'FAIL'} {check.detail}")
    return 0 if all(c.ok for c in checks) else 1


def _history_root() -> Path:
    return Path(os.environ.get("TOURNAMENT_DATA_DIR", REPO_ROOT / "data")) / "history"


def _history_download(args: argparse.Namespace) -> int:
    from tournament.adapters.alpaca import data_account_alias
    from tournament.adapters.alpaca_history import AlpacaHistory
    from tournament.marketdata.history import (
        Downloader,
        SessionGuardError,
        UniverseRule,
        check_outside_session,
        code_commit,
    )
    from tournament.marketdata.store import HistoryStore

    try:
        if not args.allow_during_session:
            check_outside_session()
        creds = load_alpaca_credentials(data_account_alias())
    except (SessionGuardError, MissingCredentialsError, InsecureEnvFileError) as exc:
        print(exc, file=sys.stderr)
        return 1
    end = args.end or date.today() - timedelta(days=1)
    try:
        Downloader(
            AlpacaHistory(creds),
            HistoryStore(_history_root()),
            UniverseRule(max_dte=args.max_dte, strike_pct=args.strike_pct),
            commit=code_commit(REPO_ROOT),
        ).run(
            args.start,
            end,
            args.datasets,
            force=args.force,
            # Checked before every day, so a long overnight run pauses at 09:00 ET.
            before_each_day=None if args.allow_during_session else check_outside_session,
        )
    except SessionGuardError as exc:
        print(f"Paused: {exc} Finished days are kept; rerun to resume.", file=sys.stderr)
        return 2
    return 0


def _history_status(args: argparse.Namespace) -> int:
    from tournament.marketdata.history import DATASETS
    from tournament.marketdata.store import HistoryStore

    store = HistoryStore(_history_root())
    print(f"history store: {store.root}")
    for name in DATASETS:
        days = store.days(name)
        span = f"{days[0]} .. {days[-1]}" if days else "-"
        size = sum(p.stat().st_size for p in (store.root / name).glob("*/*.parquet"))
        print(f"  {name:17} {len(days):4} days  {span:24}  {size / 1e6:9.1f} MB")
    return 0


def _feed_run(args: argparse.Namespace) -> int:
    import logging
    from datetime import UTC, datetime

    from tournament.adapters.alpaca import data_account_alias
    from tournament.adapters.alpaca_history import AlpacaHistory
    from tournament.live.runner import (
        PreflightFailed,
        load_feed_config,
        run_session,
        synthetic_session,
        today_session,
    )
    from tournament.live.stream import FatalStreamError, FeedGuardError

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        creds = load_alpaca_credentials(data_account_alias())
    except (MissingCredentialsError, InsecureEnvFileError) as exc:
        print(exc, file=sys.stderr)
        return 1
    if args.test_minutes:
        lead = load_feed_config()["synthetic_session_lead_min"]
        mode, day = "test", synthetic_session(args.test_minutes, datetime.now(UTC), lead)
    else:
        mode = "shadow"
        day = today_session(AlpacaHistory(creds).calendar, date.today())
        if day is None:
            print("Not a trading session today; nothing to run.")
            return 0
    out_root = Path(os.environ.get("TOURNAMENT_DATA_DIR", REPO_ROOT / "data")) / "live"
    try:
        from tournament.marketdata.store import HistoryStore

        store = HistoryStore(_history_root()) if args.decisions else None
        manifest = None
        if args.manifest:
            from tournament.accounts.manifest import load_manifest

            manifest = load_manifest(args.manifest)
        s = run_session(day, mode, creds, out_root, store, options=args.options, manifest=manifest)
    except (FeedGuardError, FatalStreamError, PreflightFailed) as exc:
        print(f"Feed stopped: {exc}", file=sys.stderr)
        return 1
    print(f"{mode}: {s.builder.finalized} slots, {s.counts['trades']} trades; see {out_root}")
    return 0


def _experiment_draft(args: argparse.Namespace) -> int:
    from tournament.accounts.manifest import ManifestError, draft_manifest, write_manifest

    try:
        m = draft_manifest(args.id, args.mode, args.tag, args.balance)
        print(f"wrote {write_manifest(m)}: review it, add the approval, commit and tag it")
    except (ManifestError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


def _experiment_check(args: argparse.Namespace) -> int:
    from tournament.accounts.manifest import ManifestError, load_manifest, preflight

    try:
        result = preflight(load_manifest(args.manifest))
    except ManifestError as exc:
        print(exc, file=sys.stderr)
        return 1
    for c in result.checks:
        flag = "ok  " if c.ok else ("FAIL" if c.blocking else "warn")
        print(f"  {flag} {c.name:28} {c.detail}")
    print("preflight PASSED" if result.passed else "preflight FAILED")
    return 0 if result.passed else 1


def _data_root() -> Path:
    return Path(os.environ.get("TOURNAMENT_DATA_DIR", REPO_ROOT / "data"))


def _engine_daily(args: argparse.Namespace) -> int:
    import logging

    from tournament.ops.engine import run_daily

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        return run_daily(REPO_ROOT, _data_root() / "live")
    except (MissingCredentialsError, InsecureEnvFileError) as exc:
        print(exc, file=sys.stderr)
        return 3


def _ops_nightly(args: argparse.Namespace) -> int:
    import logging

    from tournament.ops.nightly import run_nightly

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    today = app_config().exchange.tz
    from datetime import datetime

    return run_nightly(datetime.now(today).date(), _data_root())


def _ops_render(args: argparse.Namespace) -> int:
    from tournament.ops.render import render_all

    for path in render_all(
        app_config(), args.owner, Path(args.home), Path(args.repo), args.uv, Path(args.out)
    ):
        print(path)
    return 0


def _alerts_run(args: argparse.Namespace) -> int:
    import logging

    from tournament.alerts.run import run_bot

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        return run_bot(REPO_ROOT)
    except (MissingCredentialsError, InsecureEnvFileError) as exc:
        print(exc, file=sys.stderr)
        return 1


def _alerts_test(args: argparse.Namespace) -> int:
    from tournament.alerts.config import load_alerts_config
    from tournament.alerts.telegram import TelegramSender
    from tournament.secrets import load_telegram_credentials

    try:
        sender = TelegramSender(load_telegram_credentials(), load_alerts_config().telegram)
    except (MissingCredentialsError, InsecureEnvFileError) as exc:
        print(exc, file=sys.stderr)
        return 1
    res = sender.send(args.message)
    print("sent" if res.ok else f"FAILED: {res.error}")
    return 0 if res.ok else 1


def _alerts_status(args: argparse.Namespace) -> int:
    from tournament.alerts.config import load_alerts_config
    from tournament.alerts.store import AlertStore
    from tournament.orders.store import default_engine

    store = AlertStore(default_engine(), load_alerts_config())
    open_ = store.open_incidents()
    print(f"{len(open_)} open incident(s)")
    for i in open_:
        since = f"{i.opened_at:%H:%M:%S}"
        print(f"  [{i.severity}] {i.kind} {i.account or ''} since {since} UTC: {i.detail}")
    print(f"{len(store.due(pd.Timestamp.now(tz='UTC'), 1000))} alert(s) waiting to be sent")
    return 0


def _api_run(args: argparse.Namespace) -> int:
    import logging

    import uvicorn

    from tournament.alerts.config import load_alerts_config
    from tournament.alerts.store import AlertStore
    from tournament.api.app import create_app
    from tournament.api.config import load_api_config
    from tournament.api.store import ControlStore
    from tournament.orders.store import default_engine
    from tournament.secrets import load_api_tokens

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        tokens = load_api_tokens()
    except (MissingCredentialsError, InsecureEnvFileError) as exc:
        print(exc, file=sys.stderr)
        return 1
    cfg = load_api_config()
    engine = default_engine()
    app = create_app(
        ControlStore(engine),
        cfg,
        tokens,
        REPO_ROOT / cfg.state.file,
        AlertStore(engine, load_alerts_config()),
    )
    uvicorn.run(app, host=cfg.server.host, port=cfg.server.port, log_level="info")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tournament")
    sub = parser.add_subparsers(dest="command", required=True)

    keys = sub.add_parser("keys", help="check Alpaca credentials in .env")
    keys_sub = keys.add_subparsers(dest="keys_command", required=True)
    check = keys_sub.add_parser("check")
    dev = app_config().accounts.dev_alias
    check.add_argument("--account", default=dev, help=f"account alias (default: {dev})")
    check.set_defaults(func=_keys_check)

    hello = sub.add_parser("hello", help="read the paper account through the Alpaca API")
    hello.add_argument("--account", default=dev, help=f"account alias (default: {dev})")
    hello.set_defaults(func=_hello)

    data = sub.add_parser("data-check", help="confirm real-time SIP and OPRA market data access")
    data_alias = app_config().accounts.data_alias
    data.add_argument(
        "--account", default=data_alias, help=f"account alias (default: {data_alias})"
    )
    data.set_defaults(func=_data_check)

    from tournament.marketdata.history import DATASETS, history_start

    history = sub.add_parser("history", help="shared historical market-data store")
    history_sub = history.add_subparsers(dest="history_command", required=True)
    dl = history_sub.add_parser("download", help="fill the store; days already on disk are skipped")
    dl.add_argument("--start", type=date.fromisoformat, default=history_start())
    dl.add_argument("--end", type=date.fromisoformat, help="default: yesterday")
    dl.add_argument(
        "--datasets",
        nargs="+",
        choices=DATASETS,
        default=list(DATASETS),
        metavar="DATASET",
        help=f"any of: {', '.join(DATASETS)}",
    )
    universe = app_config().history.universe
    dl.add_argument(
        "--max-dte",
        type=int,
        default=universe.max_dte,
        help="option expirations up to N days out",
    )
    dl.add_argument(
        "--strike-pct",
        type=float,
        default=universe.strike_pct,
        help="strikes within this fraction of the open",
    )
    dl.add_argument("--force", action="store_true", help="re-download days already on disk")
    dl.add_argument("--allow-during-session", action="store_true")
    dl.set_defaults(func=_history_download)
    status = history_sub.add_parser("status", help="what the store holds")
    status.set_defaults(func=_history_status)

    feed = sub.add_parser("feed", help="the shared live SPY feed")
    feed_sub = feed.add_subparsers(dest="feed_command", required=True)
    run = feed_sub.add_parser("run", help="record today's session (shadow) through the close")
    run.add_argument(
        "--test-minutes",
        type=int,
        help="use Alpaca's test stream for a synthetic N-minute session instead",
    )
    run.add_argument(
        "--no-decisions",
        dest="decisions",
        action="store_false",
        help="record the feed only; don't run the five workers",
    )
    run.add_argument("--manifest", help="run the preflight against this experiment manifest first")
    run.add_argument(
        "--no-options",
        dest="options",
        action="store_false",
        help="don't collect OPRA option quotes",
    )
    run.set_defaults(func=_feed_run)

    api = sub.add_parser("api", help="the control API (loopback only)")
    api_sub = api.add_subparsers(dest="api_command", required=True)
    api_sub.add_parser("run", help="serve the API").set_defaults(func=_api_run)

    al = sub.add_parser("alerts", help="the Telegram alert bot")
    al_sub = al.add_subparsers(dest="alerts_command", required=True)
    al_sub.add_parser("run", help="deliver alerts and run the watchdogs").set_defaults(
        func=_alerts_run
    )
    test = al_sub.add_parser("test", help="send one message to check the token and chat ID")
    test.add_argument("--message", default="Options tournament: alert bot test message")
    test.set_defaults(func=_alerts_test)
    al_sub.add_parser("status", help="open incidents and waiting alerts").set_defaults(
        func=_alerts_status
    )

    eng = sub.add_parser("engine", help="the daily engine job (started by Supervisor)")
    eng_sub = eng.add_subparsers(dest="engine_command", required=True)
    daily = eng_sub.add_parser("daily", help="wait for the start time, run today's session")
    daily.set_defaults(func=_engine_daily)

    ops = sub.add_parser("ops", help="host jobs: nightly download, rendering the job files")
    ops_sub = ops.add_subparsers(dest="ops_command", required=True)
    nightly = ops_sub.add_parser("nightly", help="download missing history, check coverage")
    nightly.set_defaults(func=_ops_nightly)
    render = ops_sub.add_parser("render", help="render the Supervisor and launchd files")
    render.add_argument("--owner", required=True)
    render.add_argument("--home", required=True)
    render.add_argument("--repo", default=str(REPO_ROOT))
    render.add_argument("--uv", default="/opt/homebrew/bin/uv")
    render.add_argument("--out", required=True)
    render.set_defaults(func=_ops_render)

    exp = sub.add_parser("experiment", help="experiment manifests and the startup preflight")
    exp_sub = exp.add_subparsers(dest="experiment_command", required=True)
    draft = exp_sub.add_parser("draft", help="write an unapproved manifest for today's configs")
    draft.add_argument("--id", required=True)
    draft.add_argument("--mode", choices=["shadow", "paper"], default="shadow")
    draft.add_argument("--tag", help="git tag of the code; required for paper")
    draft.add_argument("--balance", type=float, required=True, help="starting balance per account")
    draft.set_defaults(func=_experiment_draft)
    check = exp_sub.add_parser("check", help="run the preflight against a manifest")
    check.add_argument("manifest")
    check.set_defaults(func=_experiment_check)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
