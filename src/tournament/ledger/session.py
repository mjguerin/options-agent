"""One account, one session day: both ledgers, their reconciliation and the metrics (AC-11)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from tournament.ledger.build import Fill, broker_fills, build_trades
from tournament.ledger.config import LedgerConfig
from tournament.ledger.metrics import daily_metrics, equity_metrics, trade_metrics
from tournament.orders.store import IntentStore


def load_equity(path: Path, account: str) -> pd.DataFrame:
    """The engine's published history for one account: estimated and risk P&L, exposure."""
    rows = []
    try:
        for line in path.read_text().splitlines():
            r = json.loads(line)
            a = r["accounts"].get(account)
            if a is None:
                continue
            rows.append(
                {
                    "at": pd.Timestamp(r["at"]),
                    "realized_usd": a["realized_pnl_usd"],
                    "unrealized_usd": a["unrealized_usd"],
                    "estimated_usd": a["realized_pnl_usd"] + a["unrealized_usd"],
                    "risk_usd": a.get("risk_pnl_usd"),
                    "exposure_usd": a["exposure_usd"],
                }
            )
    except (OSError, ValueError, KeyError):
        pass
    return pd.DataFrame(rows).set_index("at").sort_index() if rows else pd.DataFrame()


def reconcile(
    fills: list[Fill],
    trades: list,
    store: IntentStore,
    account: str,
    start_cash: float,
    broker_cash: float,
    broker_positions: dict[str, int],
    cfg: LedgerConfig,
) -> dict:
    """The identities that must hold between the broker's books and the fills behind them. A
    difference here is an accounting incident, never something to net away."""
    mult = cfg.evaluation.contract_multiplier
    rebuilt = start_cash + sum(f.cash_usd for f in fills)
    open_basis = sum(t.open_qty * t.broker_entry * mult for t in trades)
    realized_from_trades = sum(t.broker_gross_usd for t in trades)
    realized_from_cash = sum(f.cash_usd for f in fills) + open_basis
    ours = store.filled_by_symbol(account)
    pos_diffs = {
        s: [ours.get(s, 0), broker_positions.get(s, 0)]
        for s in set(ours) | set(broker_positions)
        if abs(ours.get(s, 0) - broker_positions.get(s, 0)) > cfg.reconciliation.position_contracts
    }
    checks = {
        "cash": {
            "broker_usd": broker_cash,
            "rebuilt_from_fills_usd": rebuilt,
            "difference_usd": broker_cash - rebuilt,
            "ok": abs(broker_cash - rebuilt) <= cfg.reconciliation.cash_usd,
        },
        "realized": {
            "from_trades_usd": realized_from_trades,
            "from_cash_usd": realized_from_cash,
            "difference_usd": realized_from_trades - realized_from_cash,
            "ok": abs(realized_from_trades - realized_from_cash) <= cfg.reconciliation.cash_usd,
        },
        "positions": {"differences": pos_diffs, "ok": not pos_diffs},
    }
    # broker against evaluation, trade by trade: reported, with its cause, never netted away
    gap = [
        {
            "candidate_id": t.candidate_id,
            "broker_gross_usd": t.broker_gross_usd,
            "eval_gross_usd": t.eval_gross_usd,
            "difference_usd": t.broker_gross_usd - t.eval_gross_usd,
        }
        for t in trades
        if t.closed_qty
    ]
    return {
        "ok": all(c["ok"] for c in checks.values()),
        "checks": checks,
        "broker_vs_evaluation": {
            "trades": gap,
            "total_difference_usd": sum(g["difference_usd"] for g in gap),
            "cause": "fill price against the quote at the decision, plus the configured slippage",
        },
    }


def build_session_ledger(
    store: IntentStore,
    account: str,
    cfg: LedgerConfig,
    *,
    session_date: str,
    start_equity: float,
    broker_cash: float,
    broker_positions: dict[str, int],
    open_marks: list[dict],
    equity: pd.DataFrame,
    operational: dict,
) -> dict:
    mult = cfg.evaluation.contract_multiplier
    fills = broker_fills(store, account, mult)
    trades = build_trades(store, account, cfg)
    rec = reconcile(fills, trades, store, account, start_equity, broker_cash, broker_positions, cfg)
    td = [t.as_dict() for t in trades]
    est_open = sum(m["unrealized_usd"] or 0.0 for m in open_marks)
    risk_open = sum(
        (
            m["unrealized_usd"]
            if m["unrealized_usd"] is not None
            else -m["avg_entry"] * m["qty"] * mult
        )
        for m in open_marks
    )
    net_realized = sum(t.net_eval_usd for t in trades if t.closed_qty)
    summary = {
        "session_date": session_date,
        "account": account,
        "start_equity_usd": start_equity,
        "trades": len(trades),
        "broker_realized_gross_usd": sum(t.broker_gross_usd for t in trades),
        "eval_realized_gross_usd": sum(t.eval_gross_usd for t in trades if t.closed_qty),
        "fees_usd": sum(t.fees_usd for t in trades),
        "net_realized_eval_usd": net_realized,
        "open_positions": open_marks,
        "open_estimate_usd": est_open,
        "open_risk_usd": risk_open,
        "unmarked_positions": sum(1 for m in open_marks if m["unrealized_usd"] is None),
        "net_pnl_est_usd": net_realized + est_open,
        "net_pnl_risk_usd": net_realized + risk_open,
        "stress_net_usd": {
            sc.name: sum(t.stress_net_usd[sc.name] for t in trades if t.closed_qty)
            for sc in cfg.stress
        },
        "operational": operational,  # outages and incidents stay visible (AC-17)
    }
    metrics = {
        **trade_metrics(td, cfg, start_equity),
        **equity_metrics(equity, start_equity),
        **daily_metrics([summary], start_equity),
    }
    eq_records = (
        []
        if equity.empty
        else [
            {"at": t.isoformat(), **{k: (None if pd.isna(v) else float(v)) for k, v in r.items()}}
            for t, r in equity.iterrows()
        ]
    )
    body = {
        "summary": summary,
        "metrics": metrics,
        "reconciliation": rec,
        "trades": td,
        "fills": [{**f.__dict__, "at": None if f.at is None else f.at.isoformat()} for f in fills],
        "equity": eq_records,
        "config_hash": cfg.config_hash,
    }
    body["content_hash"] = hashlib.sha256(
        json.dumps(body, sort_keys=True, default=str).encode()
    ).hexdigest()
    return body
