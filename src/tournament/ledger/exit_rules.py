"""What declared exit rules would have done on a trade's recorded path (reading aid).

Option rules run on the one-second bid path while the position was held and fill at the bid one
configured delay after they fire, so they are not flattered by selling at the exact trigger. A rule
that would fire only after the real exit never acts: the real exit came first. The rules in R run
on the strategy's own position on SPY (closes, as in D-16) for every signal, traded or not.
Every figure is a what-if. Nothing here changes a trade."""

from __future__ import annotations

import numpy as np
import pandas as pd

from tournament.ledger.config import ExitRules

AS_EXECUTED = "As executed"
AS_SPECIFIED = "As specified"


def pct(x: float) -> str:
    return f"{x * 100:g}%"


def option_rule_names(r: ExitRules) -> list[str]:
    return (
        [AS_EXECUTED]
        + [f"Breakeven stop after +{pct(x)}" for x in r.breakeven_after_pct]
        + [f"Sell half at +{pct(x)}" for x in r.half_at_pct]
        + [f"Trail {pct(r.trail_give_back_pct)} after +{pct(r.trail_activate_pct)}"]
    )


def r_rule_names(r: ExitRules) -> list[str]:
    names = [AS_SPECIFIED]
    for t in r.r_tiers:
        names += [f"Sell half at +{t:g}R", f"Sell half at +{t:g}R, rest stopped at entry"]
    return names


def _fill(path: pd.Series, at: pd.Timestamp, delay_s: float, end: pd.Timestamp) -> float | None:
    """The bid delay seconds after `at`, or None if the real exit happened first."""
    when = at + pd.Timedelta(seconds=delay_s)
    if when > end:
        return None
    known = path[path.index <= when]
    return float(known.iloc[-1]) if len(known) else None


def option_rules(
    path: pd.Series, entry: float, exit_px: float, qty: int, mult: int, rules: ExitRules,
    end: pd.Timestamp,
) -> dict[str, float]:  # fmt: skip
    """Dollars under each rule. `path` is the bid by second from entry to the real exit."""
    m = mult * qty
    base = (exit_px - entry) * m
    out = {AS_EXECUTED: base}
    items = list(path.items())
    for x in rules.breakeven_after_pct:
        pnl, locked = base, False
        for t, b in items:
            if not locked and b >= entry * (1 + x):
                locked = True
            elif locked and b <= entry:
                f = _fill(path, t, rules.fill_delay_s, end)
                if f is not None:
                    pnl = (f - entry) * m
                break
        out[f"Breakeven stop after +{pct(x)}"] = pnl
    for x in rules.half_at_pct:
        pnl = base
        for t, b in items:
            if b >= entry * (1 + x):
                f = _fill(path, t, rules.fill_delay_s, end)
                if f is not None:
                    pnl = 0.5 * m * (f - entry) + 0.5 * m * (exit_px - entry)
                break
        out[f"Sell half at +{pct(x)}"] = pnl
    pnl, peak, active = base, 0.0, False
    for t, b in items:
        peak = max(peak, b)
        if not active and b >= entry * (1 + rules.trail_activate_pct):
            active = True
        if active and b <= peak * (1 - rules.trail_give_back_pct):
            f = _fill(path, t, rules.fill_delay_s, end)
            if f is not None:
                pnl = (f - entry) * m
            break
    out[f"Trail {pct(rules.trail_give_back_pct)} after +{pct(rules.trail_activate_pct)}"] = pnl
    return out


def r_rules(ref: pd.Series, bars: pd.DataFrame, rules: ExitRules) -> dict[str, float] | None:
    """Results in R of the strategy's own position under each rule, from its entry to its exit."""
    if pd.isna(ref.get("reference_exit_price")):
        return None
    sign = 1 if ref["underlying_direction"] == "bullish" else -1
    entry, ru = float(ref["reference_entry_price"]), float(ref["r_u"])
    base = sign * (float(ref["reference_exit_price"]) - entry) / ru
    held = bars[(bars["slot"] > int(ref["entry_slot"])) & (bars["slot"] <= int(ref["exit_slot"]))]
    closes = sign * (held["close"].to_numpy() - entry) / ru if len(held) else np.array([])
    out = {AS_SPECIFIED: base}
    for t in rules.r_tiers:
        hit = next((i for i, c in enumerate(closes[:-1]) if c >= t), None)  # not the exit bar
        if hit is None:
            out[f"Sell half at +{t:g}R"] = base
            out[f"Sell half at +{t:g}R, rest stopped at entry"] = base
            continue
        half = float(closes[hit])
        out[f"Sell half at +{t:g}R"] = 0.5 * half + 0.5 * base
        rest = next((float(c) for c in closes[hit + 1 : -1] if c <= 0), base)
        out[f"Sell half at +{t:g}R, rest stopped at entry"] = 0.5 * half + 0.5 * rest
    return out
