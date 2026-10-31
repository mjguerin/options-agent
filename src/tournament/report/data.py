"""What the one-page report shows, read from what the engine already writes (never from the
trading system itself, and never written to).

Per session day: the ledgers (trades), the per-trade review (where price went), every candidate the
strategies emitted with the decision taken on it (executed or not, and why), the strategy's own
reference outcome for each (so a signal that was not traded still shows what it would have done),
and the setup funnel. Plus the engine's live state for positions and status."""

from __future__ import annotations

import glob
import json
import math
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from tournament.ledger.config import ExitRules, load_ledger_config
from tournament.ledger.exit_rules import option_rule_names, r_rule_names, r_rules
from tournament.ledger.paths import reference_path
from tournament.report.config import REPO_ROOT, ReportConfig

NOT_RECORDED = "NOT_RECORDED"


def clean(v):
    """JSON-safe: NaN, NaT, numpy and pandas values become plain Python."""
    if isinstance(v, dict):
        return {str(k): clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [clean(x) for x in v]
    if v is None or v is pd.NaT:
        return None
    if isinstance(v, pd.Timestamp):
        return v.isoformat()
    if hasattr(v, "item") and not isinstance(v, (str, bytes)):
        v = v.item()
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


def read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def read_jsonl(path: Path) -> list[dict]:
    out = []
    try:
        for line in path.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    return out


def read_parquet(pattern: str) -> pd.DataFrame:
    found = sorted(glob.glob(pattern))
    return pd.read_parquet(found[-1]) if found else pd.DataFrame()


def session_folders(live_root: Path) -> dict[str, Path]:
    """The newest run's folder for each session day (shadow < shadow-r2 < shadow-r10)."""
    best: dict[str, tuple[tuple[int, str], Path]] = {}
    for led in live_root.glob("*/*/ledger"):
        run = led.parent
        key = (len(run.name), run.name)
        day = run.parent.name
        if day not in best or key > best[day][0]:
            best[day] = (key, run)
    return {d: best[d][1] for d in sorted(best)}


def _target_price(raw) -> float | None:
    try:
        t = json.loads(raw) if isinstance(raw, str) else raw
        return None if t is None else t.get("price")
    except (ValueError, AttributeError):
        return None


def _funnel(transitions: pd.DataFrame, candidates: pd.DataFrame) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if transitions.empty:
        return out
    for sid, g in transitions.groupby("strategy_id"):
        started = int(((g.from_state == "IDLE") & (g.to_state != "IN_POSITION")).sum())
        ended: dict[str, int] = {}
        for _, r in g[
            (g.to_state == "IDLE") & ~g.from_state.isin(["IDLE", "IN_POSITION"])
        ].iterrows():
            for code in list(r.reason_codes) or ["UNKNOWN"]:
                ended[code] = ended.get(code, 0) + 1
        signals = 0 if candidates.empty else int((candidates.strategy_id == sid).sum())
        out[sid] = {"started": started, "ended": ended, "signals": signals}
    return out


def collect_day(day: str, run: Path, live_root: Path, rules: ExitRules) -> dict:
    ledgers = {}
    for f in sorted((run / "ledger").glob("acct_*.json")):
        body = read_json(f)
        if body:
            ledgers[body["summary"]["account"]] = body
    paths = read_json(run / "ledger" / "paths.json") or {}
    decisions = {d["candidate_id"]: d for d in read_jsonl(live_root / day / "decisions.jsonl")}
    cands = read_parquet(str(run / "records/*/candidates.parquet"))
    refs = read_parquet(str(run / "records/*/reference_positions.parquet"))
    trans = read_parquet(str(run / "records/*/transitions.parquet"))
    bars = read_parquet(str(run / "bars.parquet"))
    summary = read_json(run / "summary.json") or {}
    outcomes = ((summary.get("accounts") or {}).get("accounts")) or {}

    ref_by_cid: dict[str, dict] = {}
    if not refs.empty and not bars.empty:
        for _, r in refs.iterrows():
            ref_by_cid[r["candidate_id"]] = reference_path(r, bars)
            ref_by_cid[r["candidate_id"]]["rules_r"] = r_rules(r, bars, rules)

    accounts: dict[str, dict] = {}
    for acct, body in ledgers.items():
        extra = {r["candidate_id"]: r for r in paths.get(acct, [])}
        trades = []
        for t in body["trades"]:
            o = (extra.get(t["candidate_id"]) or {}).get("option") or {}
            hold, after, end = (
                o.get("hold") or {},
                o.get("after_exit") or {},
                o.get("session_end") or {},
            )
            trades.append(
                {
                    "day": day,
                    "candidate_id": t["candidate_id"],
                    "symbol": t["symbol"],
                    "right": t["right"],
                    "qty": t["qty"],
                    "entry_at": t["entry_at"],
                    "exit_at": t["exit_at"],
                    "entry": t["broker_entry"],
                    "exit": t["broker_exit"],
                    "net_usd": t["net_eval_usd"],
                    "held_s": t["holding_s"],
                    "exit_reasons": t["exit_reasons"],
                    "open_qty": t["open_qty"],
                    "peak_bid": hold.get("peak_bid"),
                    "low_bid": hold.get("low_bid"),
                    "after_exit_peak": after.get("peak_bid"),
                    "last_bid": end.get("last_bid"),
                    "rules": (extra.get(t["candidate_id"]) or {}).get("rules"),
                    "ref": ref_by_cid.get(t["candidate_id"]),
                }
            )
        accounts[acct] = {"trades": trades, "summary": body["summary"], "signals": []}
        accounts[acct]["day_net_usd"] = body["summary"]["net_pnl_est_usd"]

    # every candidate the strategies emitted, and what became of it
    if not cands.empty:
        executed = {t["candidate_id"]: t for a in accounts.values() for t in a["trades"]}
        pending = (
            cands[~cands.candidate_id.isin(executed)].groupby("account_binding").size().to_dict()
        )
        for _, c in cands.sort_values("signal_bar_end").iterrows():
            acct = c["account_binding"]
            if acct not in accounts:
                continue
            cid = c["candidate_id"]
            d = decisions.get(cid)
            if cid in executed:
                status, reasons = "EXECUTED", []
            elif d is not None and d["accepted"]:
                status, reasons = "ACCEPTED_UNFILLED", []
            elif d is not None:
                status, reasons = "REJECTED", d["reasons"]
            elif not bool(c["submission_eligible"]):
                status, reasons = "REJECTED", ["NOT_SUBMISSION_ELIGIBLE"]
            else:
                # no decision on record (before decisions were saved): the day's tally of reasons
                # names it only if it was the account's one unexecuted signal
                tally = {
                    k: v
                    for k, v in (outcomes.get(acct, {}).get("outcomes") or {}).items()
                    if k != "ACCEPTED"
                }
                status, reasons = (
                    "REJECTED",
                    (list(tally) if pending.get(acct) == 1 and tally else [NOT_RECORDED]),
                )
            t = executed.get(cid)
            accounts[acct]["signals"].append(
                {
                    "day": day,
                    "candidate_id": cid,
                    "at": c["signal_bar_end"],
                    "direction": c["underlying_direction"],
                    "status": status,
                    "reasons": reasons,
                    "contract": (t or {}).get("symbol") or (d or {}).get("contract"),
                    "qty": (t or {}).get("qty") or (d or {}).get("quantity"),
                    "requested_qty": (d or {}).get("requested_quantity"),
                    "capped_by": (d or {}).get("capped_by"),
                    "entry_reference": c["entry_reference"],
                    "invalidation": c["invalidation_level"],
                    "target": _target_price(c.get("target")),
                    "rules": (t or {}).get("rules"),
                    "ref": ref_by_cid.get(cid),
                }
            )
    funnel = _funnel(trans, cands)
    for acct, a in accounts.items():
        a["funnel"] = funnel.get(acct.split(":")[1], {"started": 0, "ended": {}, "signals": 0})
    return clean({"accounts": accounts})


def live_state(cfg: ReportConfig, now: datetime, root: Path = REPO_ROOT) -> dict:
    state = read_json(cfg.path("state_file", root))
    hb = read_json(cfg.path("heartbeat_file", root))
    out: dict = {"reporting": False, "accounts": {}}
    if not state:
        return out
    age = (now - datetime.fromisoformat(state["at"])).total_seconds()
    out.update(
        reporting=age <= cfg.data.state_max_age_s,
        age_s=age,
        session=state.get("session"),
        mode=state.get("mode"),
        slot=state.get("slot"),
        feeds=state.get("feeds"),
        heartbeat_at=(hb or {}).get("at"),
    )
    for acct, a in (state.get("accounts") or {}).items():
        out["accounts"][acct] = {
            "positions": a.get("positions_detail") or [],
            "halted": a.get("halted"),
            "operator_halt": a.get("operator_halt"),
            "entries_blocked": a.get("entries_blocked"),
            "pending_orders": a.get("pending_orders"),
            "exposure_usd": a.get("exposure_usd"),
            "realized_pnl_usd": a.get("realized_pnl_usd"),
            "pnl_view": a.get("pnl_view"),
            "decisions": a.get("decisions"),
            "recent_decisions": a.get("recent_decisions") or [],
        }
    return clean(out)


def collect(cfg: ReportConfig, root: Path = REPO_ROOT, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    live_root = cfg.path("live_root", root)
    days = session_folders(live_root)
    rules = load_ledger_config().paths.exit_rules
    per_day = {d: collect_day(d, run, live_root, rules) for d, run in days.items()}
    live = live_state(cfg, now, root)
    accounts = []
    for a in cfg.accounts:
        acct = f"acct:{a.id}"
        trades, signals, series, funnel = [], [], [], {}
        for d, body in per_day.items():
            x = body["accounts"].get(acct)
            if x is None:
                continue
            trades += x["trades"]
            signals += x["signals"]
            series.append({"day": d, "net_usd": x["day_net_usd"]})
            funnel[d] = x["funnel"]
        accounts.append(
            {
                "id": acct,
                "key": a.id,
                "name": a.name,
                "trades": trades,
                "signals": signals,
                "daily": series,
                "funnel": funnel,
                "live": live["accounts"].get(acct),
            }
        )
    return clean(
        {
            "generated_at": now.isoformat(),
            "timezone": cfg.data.timezone,
            "label": cfg.labels.results,
            "days": list(per_day),
            "engine": {k: v for k, v in live.items() if k != "accounts"},
            "accounts": accounts,
            "rule_names": {"option": option_rule_names(rules), "r": r_rule_names(rules)},
            "rule_note": f"fills {rules.fill_delay_s:g} s after a rule fires; what-ifs only",
            "limits": {
                "trades": cfg.tables.trades_max_rows,
                "signals": cfg.tables.signals_max_rows,
            },
            "poll_s": cfg.server.poll_s,
        }
    )
