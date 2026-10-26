"""Ranking and promotion gates (BRD 12.1, 12.5; AC-12).

Ranking is by net result on equal starting capital. Promotion qualification is separate: an
account can lead and still fail every gate, and the report says so. A gate whose threshold is not
set is listed as not evaluated, never as passed.
"""

from __future__ import annotations

from tournament.ledger.config import LedgerConfig


def gate_results(entry: dict, cfg: LedgerConfig) -> dict[str, dict]:
    g = cfg.promotion_gates
    m, s = entry["metrics"], entry["summary"]
    net = s["net_pnl_est_usd"]
    out: dict[str, dict] = {}

    def gate(name: str, threshold, value, ok):
        out[name] = {
            "threshold": threshold,
            "value": value,
            "status": "not_evaluated" if threshold is None else ("pass" if ok else "fail"),
        }

    gate(
        "min_net_pnl_usd",
        g.min_net_pnl_usd,
        net,
        g.min_net_pnl_usd is not None and net > g.min_net_pnl_usd,
    )
    dd = m.get("max_drawdown_pct_of_start")
    gate(
        "max_drawdown_pct_of_start",
        g.max_drawdown_pct_of_start,
        dd,
        dd is not None
        and g.max_drawdown_pct_of_start is not None
        and dd >= -abs(g.max_drawdown_pct_of_start),
    )
    gate(
        "min_trades",
        g.min_trades,
        m["closed_trades"],
        g.min_trades is not None and m["closed_trades"] >= g.min_trades,
    )
    gate(
        "min_active_days",
        g.min_active_days,
        m["active_days"],
        g.min_active_days is not None and m["active_days"] >= g.min_active_days,
    )
    bad = sum(1 for c in entry["reconciliation"]["checks"].values() if not c["ok"])
    gate(
        "max_unresolved_accounting_incidents",
        g.max_unresolved_accounting_incidents,
        bad,
        g.max_unresolved_accounting_incidents is not None
        and bad <= g.max_unresolved_accounting_incidents,
    )
    worst = min(s["stress_net_usd"].values())
    gate(
        "min_conservative_net_pnl_usd",
        g.min_conservative_net_pnl_usd,
        worst,
        g.min_conservative_net_pnl_usd is not None and worst > g.min_conservative_net_pnl_usd,
    )
    return out


def rank_accounts(entries: list[dict], cfg: LedgerConfig) -> list[dict]:
    """Best net result first. `promotion_qualified` needs every evaluated gate to pass and at
    least one gate to be evaluated; `leader_not_qualified` makes the AC-12 case explicit."""
    rows = []
    for e in entries:
        gates = gate_results(e, cfg)
        evaluated = [v for v in gates.values() if v["status"] != "not_evaluated"]
        qualified = bool(evaluated) and all(v["status"] == "pass" for v in evaluated)
        rows.append(
            {
                "account": e["summary"]["account"],
                "net_pnl_est_usd": e["summary"]["net_pnl_est_usd"],
                "gates": gates,
                "promotion_qualified": qualified,
            }
        )
    rows.sort(key=lambda r: r["net_pnl_est_usd"], reverse=True)
    for i, r in enumerate(rows, 1):
        r["rank"] = i
        r["leader_not_qualified"] = i == 1 and not r["promotion_qualified"]
    return rows
