"""One live session of the shared SPY feed (SD-01, SD-03, MD-02, MD-03).

Inputs are pushed in arrival order with their receive time. Before handling anything received
after a slot's `scheduled_finalization_at`, that slot is finalized, so a snapshot contains
exactly the input that arrived by then (the ingestion boundary). `tick(now)` finalizes due
slots when no input arrives. All times come from the caller, so tests drive a synthetic clock.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

import numpy as np
import pandas as pd

from tournament.live.bars import LiveBarBuilder
from tournament.live.events import (
    HALT_CODES,
    RESUME_CODES,
    cancel_from,
    correction_from,
    status_from,
    trade_from,
)
from tournament.live.health import FeedHealth
from tournament.marketdata.bars import PremarketLevels, premarket_levels
from tournament.marketdata.calendar import SessionDay
from tournament.marketdata.eligibility import EligibilityConfig, Usage


class LiveSession:
    def __init__(
        self,
        day: SessionDay,
        cfg: EligibilityConfig,
        *,
        symbol: str = "SPY",
        rules_symbol: str | None = None,
        usage: Usage = Usage.LIVE,
        finalize_delay: timedelta = timedelta(seconds=2),
        stale_after: timedelta = timedelta(seconds=5),
        on_bar: Callable[[dict], None] | None = None,
    ) -> None:
        self.day, self.cfg, self.symbol, self.usage = day, cfg, symbol, usage
        self.delay = pd.Timedelta(finalize_delay)
        # rules_symbol: whose listing market the SIP state uses (the test stream's synthetic
        # symbol borrows SPY's)
        self.rules_symbol = rules_symbol or symbol
        self.builder = LiveBarBuilder(day, cfg, usage, self.rules_symbol)
        self.health = FeedHealth(pd.Timedelta(stale_after))
        self.on_bar = on_bar
        self.bars: list[dict] = []
        self.premarket: list[dict] = []
        self.premarket_levels: PremarketLevels | None = None
        self._delays: list[float] = []  # arrival delay (s) of trades in the slot being built
        self.counts = {"trades": 0, "duplicates": 0, "out_of_order": 0, "outside": 0}
        self._seen: set[tuple[str, int, int]] = set()
        self._max_ts: pd.Timestamp | None = None

    def scheduled_finalization(self, m: int) -> pd.Timestamp:
        return self.builder.bar_end(m) + self.delay

    @property
    def done(self) -> bool:
        return self.builder.finalized >= self.builder.n_slots

    # ---- inputs -------------------------------------------------------------------------

    def on_connected(self, at: pd.Timestamp) -> None:
        self._finalize_due(at)
        self.health.on_connected(at)

    def on_disconnected(self, at: pd.Timestamp) -> None:
        self._finalize_due(at)
        self.health.on_disconnected(at)

    def on_messages(self, msgs: list[dict], received: pd.Timestamp) -> None:
        self._finalize_due(received)
        self.health.on_message(received)
        for msg in msgs:
            if msg.get("S") not in (None, self.symbol):
                continue
            kind = msg.get("T")
            if kind == "t":
                self._trade(trade_from(msg), received)
            elif kind == "c":
                self.builder.correct(correction_from(msg))
            elif kind == "x":
                self.builder.correct(cancel_from(msg))
            elif kind == "s":
                st = status_from(msg)
                self.health.on_status(st.code, st.ts, HALT_CODES, RESUME_CODES)

    def tick(self, now: pd.Timestamp) -> None:
        self.health.check_stale(now, pd.Timestamp(self.day.open_utc))
        self._finalize_due(now)

    def _trade(self, tr, received: pd.Timestamp) -> None:
        if tr.key is not None:  # MD-03 deduplication by source identity (S-2 event_key)
            k = (*tr.key, tr.ts.value)
            if k in self._seen:
                self.counts["duplicates"] += 1
                return
            self._seen.add(k)
        self.counts["trades"] += 1
        if self._max_ts is not None and tr.ts < self._max_ts:
            self.counts["out_of_order"] += 1
        self._max_ts = tr.ts if self._max_ts is None else max(self._max_ts, tr.ts)
        if tr.ts < pd.Timestamp(self.day.open_utc):
            if tr.ts >= pd.Timestamp(self.day.extended_start_utc):
                self.premarket.append(tr.as_row())
            return
        if self.builder.add_trade(tr) == "outside":
            self.counts["outside"] += 1
            return
        self._delays.append((received - tr.ts).total_seconds())

    # ---- finalization -------------------------------------------------------------------

    def _finalize_due(self, now: pd.Timestamp) -> None:
        while not self.done:
            m = self.builder.finalized + 1
            due = self.scheduled_finalization(m)
            if now < due:
                return
            if m == 1:
                self._premarket()
            start = self.builder.bar_start(m)
            row = self.builder.finalize(
                m,
                uncertain=self.health.uncertain(start, due),
                halted=self.health.halted(start, self.builder.bar_end(m)),
            )
            d = np.array(self._delays)
            row.update(
                scheduled_finalization_at=due,
                finalized_at=max(now, due),
                arrival_delay_p50_s=float(np.percentile(d, 50)) if d.size else None,
                arrival_delay_max_s=float(d.max()) if d.size else None,
                **{f"{k}_total": v for k, v in self.counts.items()},
            )
            self._delays = []
            self.bars.append(row)
            if self.on_bar:
                self.on_bar(row)

    def _premarket(self) -> None:
        cols = ["ts", "price", "size", "exchange", "conditions", "trade_id", "tape"]
        pm = pd.DataFrame(self.premarket, columns=cols)
        pm["ts"] = pd.to_datetime(pm["ts"], utc=True)
        levels = premarket_levels(pm, self.day, self.cfg, self.usage, self.rules_symbol)
        # PMH/PML need the whole pre-market: the feed must have been healthy from 04:00 ET
        if self.health.uncertain(
            pd.Timestamp(self.day.extended_start_utc), pd.Timestamp(self.day.open_utc)
        ):
            levels = PremarketLevels(
                None, None, levels.eligible_trades, levels.quarantined_trades,
                (*levels.flags, "premarket_feed_uncertain"),
            )  # fmt: skip
        self.premarket_levels = levels
