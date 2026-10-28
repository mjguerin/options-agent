"""Where each trade went: the option's price path and the underlying's, for review (D-80, D-81).

This is a reading aid written after the session's files are saved; it never changes a ledger or
a result. For every trade it records the option's bid at its best and worst during the hold, what
it did in the window after the exit, and where it ended the day, with the money each would have
meant. For the strategy's own reference position it records the outcome in units of its risk (R)
and how far price ran for and against it. Together they separate a signal that failed (price never
followed through) from an entry or an exit that cost money (price did, and the trade missed it).
The option path comes from the one-second snapshots, which keep the last quote of each second, so
a spike shorter than a second can be missed.
"""

from __future__ import annotations

import glob
import json
from pathlib import Path

import pandas as pd

from tournament.ledger.config import LedgerConfig


def _secs(a: pd.Timestamp, b: pd.Timestamp) -> float:
    return float((b - a).total_seconds())


def option_path(
    quotes: pd.DataFrame,
    symbol: str,
    entry_at: pd.Timestamp,
    exit_at: pd.Timestamp | None,
    entry: float,
    exit_: float | None,
    qty: int,
    mult: int,
    after_min: float,
) -> dict:
    q = quotes[quotes["symbol"] == symbol].sort_values("second")
    q = q[q["bid"] > 0]  # a zero bid is no market, not a price
    if q.empty:
        return {"available": False}
    end = exit_at if exit_at is not None else q["second"].iloc[-1]
    hold = q[(q["second"] >= entry_at) & (q["second"] <= end)]
    out: dict = {"available": not hold.empty}
    if not hold.empty:
        hi, lo = hold.loc[hold["bid"].idxmax()], hold.loc[hold["bid"].idxmin()]
        out["hold"] = {
            "peak_bid": float(hi["bid"]),
            "peak_after_s": _secs(entry_at, hi["second"]),
            "peak_vs_entry_pct": float(hi["bid"] / entry - 1),
            "pnl_at_peak_usd": float((hi["bid"] - entry) * qty * mult),
            "low_bid": float(lo["bid"]),
            "low_after_s": _secs(entry_at, lo["second"]),
            "low_vs_entry_pct": float(lo["bid"] / entry - 1),
        }
    last = q.iloc[-1]
    out["session_end"] = {
        "last_bid": float(last["bid"]),
        "at": last["second"].isoformat(),
        "held_to_end_pnl_usd": float((last["bid"] - entry) * qty * mult),
    }
    if exit_at is not None and exit_ is not None:
        out["exit_pnl_usd"] = float((exit_ - entry) * qty * mult)
        window = q[
            (q["second"] > exit_at) & (q["second"] <= exit_at + pd.Timedelta(minutes=after_min))
        ]
        if not window.empty:
            top = window.loc[window["bid"].idxmax()]
            out["after_exit"] = {
                "window_min": after_min,
                "peak_bid": float(top["bid"]),
                "peak_after_s": _secs(exit_at, top["second"]),
                "peak_vs_exit_pct": float(top["bid"] / exit_ - 1) if exit_ else None,
            }
        out["exit_vs_held_to_end_usd"] = (
            out["exit_pnl_usd"] - out["session_end"]["held_to_end_pnl_usd"]
        )
    return out


def reference_path(ref: pd.Series, bars: pd.DataFrame) -> dict:
    """The strategy's own position on the underlying, in units of its risk r_u."""
    sign = 1 if ref["underlying_direction"] == "bullish" else -1
    entry, r_u = float(ref["reference_entry_price"]), float(ref["r_u"])
    out = {
        "direction": ref["underlying_direction"],
        "entry_price": entry,
        "r_u": r_u,
        "exit_reason": ref.get("exit_primary_reason"),
        "exit_price": None,
        "result_r": None,
    }
    if pd.notna(ref.get("reference_exit_price")):
        out["exit_price"] = float(ref["reference_exit_price"])
        out["result_r"] = sign * (out["exit_price"] - entry) / r_u
        out["exit_time"] = pd.Timestamp(ref["reference_exit_time"]).isoformat()
    last_slot = int(ref["exit_slot"]) if pd.notna(ref.get("exit_slot")) else int(bars["slot"].max())
    held = bars[(bars["slot"] > int(ref["entry_slot"])) & (bars["slot"] <= last_slot)]
    if not held.empty:
        best = held["high"].max() if sign > 0 else held["low"].min()
        worst = held["low"].min() if sign > 0 else held["high"].max()
        out["best_excursion_r"] = sign * (best - entry) / r_u  # how far it ran for the trade
        out["worst_excursion_r"] = sign * (worst - entry) / r_u  # and against it (negative)
        out["slots_held"] = int(len(held))
    return out


def build_paths(session_dir: Path, cfg: LedgerConfig) -> dict:
    """Read a saved session folder and write ledger/paths.json beside the ledgers."""
    ledger_dir = session_dir / "ledger"
    quotes_file = session_dir / "option_quotes_1s.parquet"
    quotes = pd.read_parquet(quotes_file) if quotes_file.exists() else pd.DataFrame()
    if not quotes.empty:
        quotes["second"] = pd.to_datetime(quotes["second"], utc=True)
    bars_file = session_dir / "bars.parquet"
    bars = pd.read_parquet(bars_file) if bars_file.exists() else pd.DataFrame()
    refs_file = next(
        iter(sorted(glob.glob(str(session_dir / "records/*/reference_positions.parquet")))), None
    )
    refs = pd.read_parquet(refs_file) if refs_file else pd.DataFrame()
    mult = cfg.evaluation.contract_multiplier
    result: dict[str, list] = {}
    for f in sorted(ledger_dir.glob("acct_*.json")):
        body = json.loads(f.read_text())
        rows = []
        for t in body["trades"]:
            entry_at = pd.Timestamp(t["entry_at"]) if t["entry_at"] else None
            exit_at = pd.Timestamp(t["exit_at"]) if t["exit_at"] else None
            row = {
                "candidate_id": t["candidate_id"],
                "symbol": t["symbol"],
                "qty": t["qty"],
                "entry": t["broker_entry"],
                "exit": t["broker_exit"],
                "exit_reasons": t["exit_reasons"],
                "option": {"available": False},
                "reference": None,
            }
            if entry_at is not None and not quotes.empty:
                row["option"] = option_path(
                    quotes, t["symbol"], entry_at, exit_at, t["broker_entry"], t["broker_exit"],
                    t["qty"], mult, cfg.paths.after_exit_window_min,
                )  # fmt: skip
            if not refs.empty and not bars.empty:
                match = refs[refs["candidate_id"] == t["candidate_id"]]
                if not match.empty:
                    row["reference"] = reference_path(match.iloc[0], bars)
            rows.append(row)
        result[body["summary"]["account"]] = rows
    (ledger_dir / "paths.json").write_text(json.dumps(result, indent=1, default=str))
    return result
