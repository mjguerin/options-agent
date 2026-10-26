"""Performance metrics with their denominators written down (BRD 12.2).

Closed-trade figures use closed trades only. A position still open at the end is reported
separately, marked at an executable-side estimate, and enters the equity figures but never the
win rate or payoff ratio. Nothing is annualized. An undefined value is None, never zero, and the
profit factor's behaviour with no losing trades is a config choice.
"""

from __future__ import annotations

import pandas as pd

from tournament.ledger.config import LedgerConfig


def trade_metrics(trades: list[dict], cfg: LedgerConfig, start_equity: float) -> dict:
    closed = [t for t in trades if t["closed_qty"] > 0]
    nets = [t["net_eval_usd"] for t in closed]
    thr = cfg.report.win_threshold_usd
    wins = [n for n in nets if n > thr]
    losses = [n for n in nets if n < 0]  # a result between zero and the threshold is neither
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    if gross_loss > 0:
        profit_factor = gross_win / gross_loss
    elif gross_win > 0 and cfg.report.profit_factor_when_no_losses == "infinity":
        profit_factor = float("inf")
    else:
        profit_factor = None  # undefined: no losing trade to divide by
    avg_win = (gross_win / len(wins)) if wins else None
    avg_loss = (gross_loss / len(losses)) if losses else None
    held = [t["holding_s"] for t in closed if t["holding_s"] is not None]
    premium = sum(t["premium_usd"] for t in trades)
    notional = premium + sum(
        (t["broker_exit"] or 0.0) * t["closed_qty"] * cfg.evaluation.contract_multiplier
        for t in closed
    )
    return {
        "trades": len(trades),
        "closed_trades": len(closed),
        "open_trades": len(trades) - len(closed),
        "net_realized_usd": sum(nets),
        "gross_realized_usd": sum(t["eval_gross_usd"] for t in closed),
        "fees_usd": sum(t["fees_usd"] for t in trades),
        "win_rate": (len(wins) / len(closed)) if closed else None,  # wins / closed trades
        "avg_net_trade_usd": (sum(nets) / len(nets)) if nets else None,
        "payoff_ratio": (avg_win / avg_loss) if (avg_win is not None and avg_loss) else None,
        "profit_factor": profit_factor,
        "largest_loss_usd": min(losses, default=None),
        "avg_holding_s": (sum(held) / len(held)) if held else None,
        "premium_deployed_usd": premium,
        "turnover_x_start_equity": (notional / start_equity) if start_equity else None,
    }


def equity_metrics(equity: pd.DataFrame, start_equity: float) -> dict:
    """From a series of estimated equity (index = time, columns include estimated_usd and
    exposure_usd). Drawdown is the largest fall from a running peak of equity."""
    if equity.empty:
        return {
            "max_drawdown_usd": None,
            "max_drawdown_pct_of_start": None,
            "exposure_time_share": None,
        }
    eq = start_equity + equity["estimated_usd"]
    dd = (eq - eq.cummax()).min()
    return {
        "max_drawdown_usd": float(dd),
        "max_drawdown_pct_of_start": float(dd / start_equity) if start_equity else None,
        "exposure_time_share": float((equity["exposure_usd"] > 0).mean()),  # share of snapshots
    }


def daily_metrics(days: list[dict], start_equity: float) -> dict:
    """From one summary per session day: net result (realized net plus open estimate)."""
    if not days:
        return {"active_days": 0, "worst_day_usd": None, "daily_returns": []}
    nets = [d["net_pnl_est_usd"] for d in days]
    active = [d for d in days if d["trades"] > 0]
    return {
        "active_days": len(active),
        "worst_day_usd": min(nets),
        "daily_returns": [n / start_equity for n in nets] if start_equity else [],
    }
