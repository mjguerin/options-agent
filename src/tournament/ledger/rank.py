"""Ranking and promotion gates (BRD 12.1, 12.5; AC-12; D-83).

Ranking is by net result on equal starting capital. Promotion qualification is separate: an
account can lead and still fail, and the report says so. Every gate is mandatory. A gate that is
not configured, or whose inputs are not trustworthy yet (costs unapproved, a position left without
a fresh mark, no approved confirmation policy), is NOT_EVALUABLE, and a NOT_EVALUABLE gate blocks
qualification: leaving a value out never removes a requirement.
"""

from __future__ import annotations

from tournament.ledger.config import LedgerConfig

PASS, FAIL, NOT_EVALUABLE = "pass", "fail", "not_evaluable"


def gate_results(entry: dict, cfg: LedgerConfig) -> dict[str, dict]:
    g = cfg.promotion_gates
    m, s = entry["metrics"], entry["summary"]
    costs_ok = cfg.costs.status == "approved"
    complete = s.get("primary_valuation_complete", True)
    out: dict[str, dict] = {}

    def gate(name: str, threshold, value, ok, blockers: list[str]):
        if threshold is None:
            blockers = ["threshold not set", *blockers]
        if blockers:
            status = NOT_EVALUABLE
        else:
            status = PASS if ok else FAIL
        out[name] = {
            "threshold": threshold,
            "value": value,
            "status": status,
            "why_not_evaluable": blockers,
        }

    net = s["net_pnl_est_usd"]
    base_blockers = ([] if costs_ok else ["costs not approved (D10)"]) + (
        [] if complete else ["open position without a fresh mark"]
    )
    gate(
        "min_net_pnl_usd",
        g.min_net_pnl_usd,
        net,
        g.min_net_pnl_usd is not None and net > g.min_net_pnl_usd,
        base_blockers,
    )
    bad = sum(1 for c in entry["reconciliation"]["checks"].values() if not c["ok"])
    gate(
        "max_unresolved_accounting_incidents",
        g.max_unresolved_accounting_incidents,
        bad,
        g.max_unresolved_accounting_incidents is not None
        and bad <= g.max_unresolved_accounting_incidents,
        [],
    )
    harsh = s["stress_net_usd"].get(cfg.harshest_scenario)
    gate(
        "min_harsh_net_pnl_usd",
        g.min_harsh_net_pnl_usd,
        harsh,
        harsh is not None
        and g.min_harsh_net_pnl_usd is not None
        and harsh > g.min_harsh_net_pnl_usd,
        base_blockers + ([] if harsh is not None else ["harshest scenario missing"]),
    )
    dd = m.get("max_drawdown_pct_of_start")
    gate(
        "max_drawdown_pct_of_start",
        g.max_drawdown_pct_of_start,
        dd,
        dd is not None
        and g.max_drawdown_pct_of_start is not None
        and dd >= -abs(g.max_drawdown_pct_of_start),
        [] if dd is not None else ["no equity points"],
    )
    gate(
        "min_trades",
        g.min_trades,
        m["closed_trades"],
        g.min_trades is not None and m["closed_trades"] >= g.min_trades,
        [],
    )
    gate(
        "min_active_days",
        g.min_active_days,
        m["active_days"],
        g.min_active_days is not None and m["active_days"] >= g.min_active_days,
        [],
    )
    approved = g.confirmation_policy == "approved"
    out["confirmation_policy"] = {
        "threshold": g.confirmation_policy,
        "value": g.confirmation_policy,
        "status": PASS if approved else NOT_EVALUABLE,
        "why_not_evaluable": []
        if approved
        else ["statistical and confirmation policy unset (D09)"],
    }
    return out


def rank_accounts(entries: list[dict], cfg: LedgerConfig) -> list[dict]:
    """Best net result first. `promotion_qualified` needs every gate to be evaluable and to pass;
    `leader_not_qualified` makes the AC-12 case explicit. Outages and incidents are carried
    beside every result, and no trade is dropped because of them (AC-17)."""
    rows = []
    for e in entries:
        gates = gate_results(e, cfg)
        qualified = all(v["status"] == PASS for v in gates.values())
        rows.append(
            {
                "account": e["summary"]["account"],
                "net_pnl_est_usd": e["summary"]["net_pnl_est_usd"],
                "result_label": e["summary"].get("result_label", cfg.costs.label),
                "gates": gates,
                "promotion_qualified": qualified,
                "not_evaluable": sorted(
                    k for k, v in gates.items() if v["status"] == NOT_EVALUABLE
                ),
                "operational": e["summary"].get("operational"),
            }
        )
    rows.sort(key=lambda r: r["net_pnl_est_usd"], reverse=True)
    for i, r in enumerate(rows, 1):
        r["rank"] = i
        r["leader_not_qualified"] = i == 1 and not r["promotion_qualified"]
    return rows
