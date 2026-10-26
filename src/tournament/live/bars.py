"""Live minute bars, one slot at a time, identical to the batch builder (SD-01, SD-03, SD-04).

Trades wait in their slot until it is finalized. Finalization runs stage 1 on that slot's
trades with the SIP last state carried from the previous slot, then the shared `slot_fields`
aggregation, so a session whose trades all arrive on time yields exactly the batch bars.

Live-only rules (SD-01 "Late inputs", SD-03 "Corrections and cancels"):
- A trade for an already finalized slot is `late`. It never changes that bar. Its volume and
  VWAP contribution enter the cumulative state at the next finalization. It is resolved
  without the SIP last state of its own time, so only a plain-YES last rule counts toward
  VWAP; note-dependent rules are excluded and flagged. It doesn't update the last state.
- An unknown condition discovered late makes the next finalized slot DATA_MISSING_OR_UNCERTAIN
  (the guard applies from discovery onward, D-36).
- A cancel or correction of a pending trade edits the pending slot. Of a finalized trade, it
  removes or replaces that trade's contribution at the next finalization.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

import numpy as np
import pandas as pd

from tournament.appconfig import app_config
from tournament.live.events import Correction, TradeIn
from tournament.marketdata.bars import (
    DATA_MISSING_OR_UNCERTAIN,
    KNOWN_NO_ELIGIBLE_TRADES,
    PRESENT,
    _stage1,
    slot_fields,
)
from tournament.marketdata.calendar import SessionDay
from tournament.marketdata.eligibility import EligibilityConfig, LastState, Usage

HALTED = "HALTED"
TRADE_COLS = ["ts", "price", "size", "exchange", "conditions", "trade_id", "tape"]


class LiveBarBuilder:
    def __init__(
        self, day: SessionDay, cfg: EligibilityConfig, usage: Usage, symbol: str | None = None
    ) -> None:
        symbol = symbol or app_config().underlying
        self.day, self.cfg, self.usage = day, cfg, usage
        self.n_slots = int((day.close_utc - day.open_utc) / timedelta(minutes=1))
        self.open_ns = pd.Timestamp(day.open_utc).value
        self.state = LastState(cfg.listing_market[symbol])
        self.pending: dict[int, list[TradeIn]] = defaultdict(list)
        self.finalized = 0
        self.cum_n, self.cum_v, self.cum_tot = 0.0, 0, 0
        self.vwap_uncertain = False
        # applied at the next finalization: [notional, vwap volume, volume]
        self._adjust = [0.0, 0, 0]
        self._late_unknown = False
        self._counts = {"late": 0, "late_unresolved": 0, "corrections": 0, "unmatched": 0}
        # finalized trades by key -> slot, and each slot's per-trade contributions
        self._slot_of: dict[tuple[str, int], int] = {}
        self._contrib: dict[int, pd.DataFrame] = {}

    def slot_of(self, ts: pd.Timestamp) -> int:
        return int((ts.value - self.open_ns) // 60_000_000_000 + 1)

    def bar_start(self, m: int) -> pd.Timestamp:
        return pd.Timestamp(self.day.open_utc + timedelta(minutes=m - 1))

    def bar_end(self, m: int) -> pd.Timestamp:
        return pd.Timestamp(self.day.open_utc + timedelta(minutes=m))

    # ---- inputs -------------------------------------------------------------------------

    def add_trade(self, tr: TradeIn) -> str:
        m = self.slot_of(tr.ts)
        if not 1 <= m <= self.n_slots:
            return "outside"
        if m > self.finalized:
            self.pending[m].append(tr)
            return "pending"
        self._counts["late"] += 1
        self._apply(self._late_contribution(tr), +1)
        return "late"

    def correct(self, c: Correction) -> str:
        self._counts["corrections"] += 1
        for m in sorted(self.pending):
            trades = self.pending[m]
            for i, tr in enumerate(trades):
                if tr.key == c.original:
                    if c.corrected is None:
                        del trades[i]
                    else:  # keeps the original's time and place in the order
                        trades[i] = TradeIn(tr.ts, *_fields(c.corrected))
                    return "pending"
        m = self._slot_of.get(c.original)
        if m is None:
            self._counts["unmatched"] += 1
            return "unmatched"
        f = self._contrib[m]
        row = f[(f["exchange"] == c.original[0]) & (f["trade_id"] == c.original[1])]
        if row.empty:
            self._counts["unmatched"] += 1
            return "unmatched"
        r = row.iloc[0]
        self._apply((float(r["n"]), int(r["v"]), int(r["vol"])), -1)
        if c.corrected is not None:
            self._apply(self._late_contribution(c.corrected), +1)
        return "finalized"

    def _late_contribution(self, tr: TradeIn) -> tuple[float, int, int]:
        cls = self.cfg.classify(tuple(tr.conditions), self.day.session_date, self.usage)
        if cls.unknown:
            self._late_unknown = True
            return 0.0, 0, 0
        if cls.informational:
            return 0.0, 0, 0
        vol = tr.size if cls.volume == "YES" else 0
        if cls.last == "YES":
            return tr.price * tr.size, tr.size, vol
        if cls.last != "NO":
            self._counts["late_unresolved"] += 1
        return 0.0, 0, vol

    def _apply(self, c: tuple[float, int, int], sign: int) -> None:
        self._adjust[0] += sign * c[0]
        self._adjust[1] += sign * c[1]
        self._adjust[2] += sign * c[2]

    # ---- finalization -------------------------------------------------------------------

    def finalize(self, m: int, *, uncertain: bool = False, halted: bool = False) -> dict:
        """Freeze slot m. `uncertain`: the feed may have missed input for it; `halted`: a halt
        covered it. Returns one row with the batch bar columns plus live counters."""
        if m != self.finalized + 1:
            raise ValueError(f"slot {m} finalized out of order (last {self.finalized})")
        trades = self.pending.pop(m, [])
        if trades:
            df = pd.DataFrame([t.as_row() for t in trades], columns=TRADE_COLS)
            df["ts"] = pd.to_datetime(df["ts"], utc=True)
            df = df.sort_values("ts", kind="stable").reset_index(drop=True)
            df["slot"] = m
            st = _stage1(
                df,
                self.day.session_date,
                self.cfg,
                self.usage,
                self.state.listing_market,
                state=self.state,
            )
            agg = slot_fields(st, pd.RangeIndex(m, m + 1, name="slot")).iloc[0]
            usable = ~st["unknown"] & ~st["info"]
            px, vol = usable & st["last_ok"], usable & st["vol_ok"]
            self._contrib[m] = pd.DataFrame(
                {
                    "exchange": st["exchange"],
                    "trade_id": st["trade_id"],
                    "n": np.where(px, st["price"] * st["size"], 0.0),
                    "v": np.where(px, st["size"], 0),
                    "vol": np.where(vol, st["size"], 0),
                }
            )
            for k in zip(st["exchange"], st["trade_id"], strict=True):
                if not pd.isna(k[1]):
                    self._slot_of[(k[0], int(k[1]))] = m
            row = {c: agg[c] for c in agg.index}
        else:
            row = {
                "open": np.nan,
                "close": np.nan,
                "high": np.nan,
                "low": np.nan,
                "volume": 0,
                "trade_count": 0,
                "vwap_notional": 0.0,
                "vwap_volume": 0,
                "quarantined_trades": 0,
                "flags": [],
            }
        unc = uncertain or row["quarantined_trades"] > 0 or self._late_unknown
        if halted:
            status = HALTED
        elif unc:
            status = DATA_MISSING_OR_UNCERTAIN
        else:
            status = PRESENT if not pd.isna(row["open"]) else KNOWN_NO_ELIGIBLE_TRADES
        self.vwap_uncertain |= bool(unc)
        self.cum_n = self.cum_n + row["vwap_notional"]
        self.cum_v = self.cum_v + int(row["vwap_volume"])
        self.cum_tot = self.cum_tot + int(row["volume"])
        if self._adjust != [0.0, 0, 0]:
            self.cum_n += self._adjust[0]
            self.cum_v += self._adjust[1]
            self.cum_tot += self._adjust[2]
        flags = list(row["flags"])
        if self._late_unknown:
            flags.append("late_unknown_condition")
        row.update(
            slot=m,
            bar_start=self.bar_start(m),
            bar_end=self.bar_end(m),
            interval_status=status,
            cum_vwap_notional=self.cum_n,
            cum_vwap_volume=self.cum_v,
            cum_total_volume=self.cum_tot,
            session_vwap=self.cum_n / self.cum_v if self.cum_v > 0 else np.nan,
            vwap_uncertain=self.vwap_uncertain,
            flags=flags,
            late_adjustment=tuple(self._adjust),
            **{f"{k}_total": v for k, v in self._counts.items()},
        )
        self._adjust, self._late_unknown = [0.0, 0, 0], False
        self.finalized = m
        return row


def _fields(t: TradeIn) -> tuple:
    return t.price, t.size, t.exchange, t.conditions, t.trade_id, t.tape
