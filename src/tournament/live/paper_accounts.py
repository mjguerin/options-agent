"""Real Alpaca paper accounts for the strategies that have graduated from shadow-only (Step 4).

They run beside the modeled accounts, never instead of them: the same candidates and the same
reference exits reach both, so the shadow accounts stay the benchmark and the paper accounts show
what a real broker did with the same signals. Each strategy has its own Alpaca paper account
(one credential alias each), its own order manager and its own durable database
(`paper-accounts.sqlite`, bindings `paper:<strategy>`), so paper orders never mix with modeled ones
in the ledgers. An account that fails its checks is left out and reported; it never trades.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pandas as pd

from tournament.live.shadow_accounts import ShadowAccounts
from tournament.orders.paper import PaperBroker, PaperChecks, account_problems
from tournament.secrets import load_alpaca_credentials

log = logging.getLogger(__name__)


def paper_db_path(day_dir: Path) -> Path:
    return day_dir / "paper-accounts.sqlite"


def paper_checks(pc: dict) -> PaperChecks:
    return PaperChecks(
        pc["min_options_level"],
        pc["expected_equity_usd"],
        pc["equity_tolerance_usd"],
        pc["cash_cache_s"],
    )


def connect_accounts(pc: dict, strategies: list[str]) -> tuple[dict[str, PaperBroker], dict]:
    """The brokers that pass every check, and a report of each strategy's verdict. Two
    strategies on one Alpaca account would break their isolation (FR-01), so a repeated account
    number excludes both."""
    checks = paper_checks(pc)
    brokers: dict[str, PaperBroker] = {}
    report: dict[str, dict] = {}
    numbers: dict[str, list[str]] = {}
    for sid in pc["strategies"]:
        if sid not in strategies:
            report[sid] = {"ok": False, "problems": ["strategy is not running"]}
            continue
        alias = pc["aliases"].get(sid, sid)
        try:
            broker = PaperBroker(load_alpaca_credentials(alias), checks.cash_cache_s)
            summary = broker.summary()
        except Exception as exc:  # noqa: BLE001 - one account's trouble never stops the others
            report[sid] = {
                "ok": False,
                "alias": alias,
                "problems": [f"{type(exc).__name__}: {exc}"],
            }
            continue
        block, warn = account_problems(summary, checks)
        report[sid] = {
            "alias": alias,
            "account": summary.account_number_masked,
            "equity_usd": float(summary.equity),
            "cash_usd": float(summary.cash),
            "options_level": summary.options_trading_level,
            "problems": block,
            "warnings": warn,
        }
        numbers.setdefault(summary.account_number_masked, []).append(sid)
        if not block:
            brokers[sid] = broker
    for number, sids in numbers.items():
        if len(sids) > 1:
            for sid in sids:
                brokers.pop(sid, None)
                report[sid]["problems"].append(f"shares account {number} with {sids}")
    for r in report.values():
        r["ok"] = not r["problems"]
    return brokers, report


class PaperAccounts(ShadowAccounts):
    PREFIX = "paper"
    DECISIONS = "paper-decisions.jsonl"

    def __init__(self, brokers: dict[str, PaperBroker], *args, **kw) -> None:
        self._paper = brokers
        super().__init__(*args, **kw)

    def _make_broker(self, sid, collector, starting_balance, mult):
        return self._paper[sid]

    def _candidate(self, rec: dict) -> None:
        sid = rec["account_binding"].split(":", 1)[1]  # acct:<strategy>
        mine = {**rec, "account_binding": f"{self.PREFIX}:{sid}"}
        if mine["account_binding"] in self.router.runtimes:
            super()._candidate(mine)  # a strategy without a paper account stays shadow-only

    def _reference_exit(self, strategy_id: str, candidate_id: str, reason: str, when) -> None:
        if f"{self.PREFIX}:{strategy_id}" in self.router.runtimes:
            super()._reference_exit(strategy_id, candidate_id, reason, when)


def start_paper_accounts(
    pc: dict,
    day,
    collector,
    engine,
    cutoff_min: int,
    db_path: Path,
    starting_balance: float,
    recovered: dict | None,
    manage_only: bool,
    incidents,
) -> PaperAccounts | None:
    """Connect, check and start the paper accounts; None when off or when none may trade."""
    if not pc["enabled"]:
        return None
    try:
        running = [w.strategy_id for w in engine.harness.workers]
        brokers, report = connect_accounts(pc, running)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db_path.with_name("paper-accounts-check.json").write_text(
            json.dumps(report, indent=1, default=str)
        )
        now0 = pd.Timestamp.now(tz="UTC")
        for sid, r in report.items():
            if r["problems"] and incidents is not None:
                incidents.open(now0, "PAPER_ACCOUNT_REFUSED", f"{sid}: {r['problems']}"[:200], sid)
        if not brokers:
            log.error("paper accounts: none passed their checks: %s", report)
            return None
        paper = PaperAccounts(
            brokers,
            day,
            collector,
            list(brokers),
            starting_balance,
            cutoff_min,
            db_path,
            pc["tick_every_s"],
            pc["reconcile_every_s"],
        )
        if recovered is not None and "error" in recovered:
            for rt in paper.router.runtimes.values():
                rt.operator_halt = "RECOVERY_FAILED"
            paper.errors.append(f"journal replay: {recovered['error']}")
        paper.recover(now0, engine.harness.closed_references())
        for sid, rt in ((r.strategy_id, r) for r in paper.router.runtimes.values()):
            # sizing follows what the account really holds today, not the day-one balance
            rt.start_of_day_equity = report[sid]["equity_usd"]
        if manage_only:
            for rt in paper.router.runtimes.values():
                rt.operator_halt = "PREFLIGHT_FAILED"
        previous = collector.held_symbols
        collector.held_symbols = lambda: set(previous()) | paper.held_symbols()
        paper.attach(engine.harness)
        log.info("paper accounts started for %s", sorted(brokers))
        return paper
    except Exception as exc:  # noqa: BLE001 - the shadow session goes on without them
        log.exception("paper accounts not started: %s", exc)
        return None
