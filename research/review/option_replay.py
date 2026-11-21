# ruff: noqa
"""Option-level replay (specs/option_level_replay_method.md, dated 2026-11-21): dollars per position
from stored one-minute option trade bars, with the premium stop, sizing, account rules and D10 costs.
Used identically for v1 and a variant."""

import math
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
H = 0.005  # half-spread haircut per side
STOP = 0.20
BUDGET, EQUITY, LIMIT, EXTRA, MULT = 0.20, 10_000.0, 500.0, 0.05, 100
FEE_PER_CONTRACT, SEC_RATE = 0.0403, 0.0000206
WINDOW = pd.Timedelta(minutes=3)

_CONTRACTS = None
_BARS: dict[str, pd.DataFrame] = {}


def contracts():
    global _CONTRACTS
    if _CONTRACTS is None:
        c = pd.read_parquet(ROOT / "data/history/option_contracts")
        c["expiration"] = pd.to_datetime(c["expiration"]).dt.date
        _CONTRACTS = c
    return _CONTRACTS


def day_bars(d: date) -> pd.DataFrame:
    k = d.isoformat()
    if k not in _BARS:
        p = ROOT / f"data/history/option_bars_1m/{d.year}/{k}.parquet"
        _BARS[k] = pd.read_parquet(p) if p.exists() else pd.DataFrame(columns=["symbol", "ts"])
    return _BARS[k]


def strike_symbol(d: date, direction: str, spot: float) -> str | None:
    c = contracts()
    right = "call" if direction == "bullish" else "put"
    ch = c[(c.expiration == d) & (c.right == right)].sort_values("strike")
    if ch.empty:
        return None
    i = min(range(len(ch)), key=lambda j: (abs(ch.strike.iloc[j] - spot), ch.strike.iloc[j]))
    return ch.symbol.iloc[i]


def first_bar_at_or_after(bars: pd.DataFrame, t: pd.Timestamp):
    b = bars[(bars.ts >= t.ceil("min")) & (bars.ts <= t.ceil("min") + WINDOW)]
    return None if b.empty else b.iloc[0]


def score_session(d: date, positions: pd.DataFrame) -> list[dict]:
    """positions: one session's reference positions (any order). Returns one row per signal."""
    rows, open_until, realized, bars_cache = [], None, 0.0, {}
    for _, p in positions.sort_values("reference_entry_time").iterrows():
        t_in = pd.Timestamp(p.reference_entry_time) + pd.Timedelta(seconds=1)
        row = {"key": p.key, "pnl": 0.0, "status": None}
        rows.append(row)
        if open_until is not None and t_in < open_until:
            row["status"] = "SKIP_OPEN"
            continue
        if realized <= -LIMIT:
            row["status"] = "SKIP_DAILY_LIMIT"
            continue
        sym = strike_symbol(d, p.underlying_direction, float(p.reference_entry_price))
        if sym is None:
            row["status"] = "UNPRICED"
            continue
        if sym not in bars_cache:
            b = day_bars(d)
            bars_cache[sym] = b[b.symbol == sym].sort_values("ts").reset_index(drop=True)
        bars = bars_cache[sym]
        eb = first_bar_at_or_after(bars, t_in)
        t_out = pd.Timestamp(p.reference_exit_time)
        if eb is None:
            row["status"] = "UNPRICED"
            continue
        entry = float(eb.open) * (1 + H)
        qty = math.floor(BUDGET * EQUITY / (entry * MULT))
        qty = min(qty, math.floor((LIMIT + realized) / (entry * MULT * (STOP + EXTRA))))
        if qty < 1:
            row["status"] = "SKIP_SIZE"
            continue
        stop_px = (1 - STOP) * entry
        exit_px = exit_at = None
        held = bars[(bars.ts > eb.ts) & (bars.ts < t_out.ceil("min"))]
        for _, b in held.iterrows():
            if float(b.low) * (1 - H) <= stop_px:
                exit_px, exit_at = min(float(b.open) * (1 - H), stop_px), b.ts
                break
        if exit_px is None:
            xb = first_bar_at_or_after(bars, t_out)
            if xb is None:
                after = bars[bars.ts >= eb.ts]
                if after.empty or (t_out - after.ts.iloc[-1]) > pd.Timedelta(minutes=30):
                    row["status"] = "UNPRICED"
                    continue
                exit_px, exit_at = float(after.close.iloc[-1]) * (1 - H), t_out
            else:
                exit_px, exit_at = float(xb.open) * (1 - H), xb.ts
        pnl = (exit_px - entry) * MULT * qty
        fees = FEE_PER_CONTRACT * 2 * qty + SEC_RATE * exit_px * MULT * qty
        row.update(pnl=pnl - fees, status="TRADED", qty=qty, entry=entry, exit=exit_px,
                   stopped=exit_px <= stop_px + 1e-9)
        realized += row["pnl"]
        open_until = pd.Timestamp(exit_at) + pd.Timedelta(seconds=1)
    return rows


def score(positions: pd.DataFrame, sessions: dict[str, date]) -> pd.DataFrame:
    out = []
    for sid, g in positions.groupby("session_id"):
        for r in score_session(sessions[sid], g):
            out.append({"session_id": sid, **r})
    return pd.DataFrame(out)
