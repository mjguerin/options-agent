"""The option-quote side of a live session: universe, subscriptions, quote book, snapshots.

Runs beside the stock stream in the same process (one connection per feed). Before the
contract-selection policy (D06) exists, the watch set is today's expiries within a window of
strikes around the underlying, re-centred at every finalized bar. A failure here (entitlement,
connection) never stops the SPY recording: entries are simply blocked (AC-08), and the summary
says why.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from tournament.appconfig import app_config
from tournament.live.option_stream import AlpacaOptionStream
from tournament.live.quotes import OptionFeed
from tournament.live.stream import FatalStreamError, FeedGuardError
from tournament.live.subscriptions import OptionUniverse, SubscriptionManager

log = logging.getLogger(__name__)


class OptionsCollector:
    def __init__(
        self, cfg: dict, stream_cfg: dict, creds, day, mode: str, spot: float, history
    ) -> None:
        self.cfg, self.day, self.mode = cfg, day, mode
        self.held_symbols = lambda: ()  # replaced once the accounts exist
        self.error: str | None = None
        d = day.session_date
        self.expirations = {d + timedelta(days=n) for n in cfg["watch_expiry_days"]}
        lo, hi = min(self.expirations), max(self.expirations)
        span = cfg["contract_fetch_range_pct"]
        raw = history.option_contracts(
            app_config().underlying, lo, hi, spot * (1 - span), spot * (1 + span)
        ).to_pandas()
        self.universe = OptionUniverse(raw[["symbol", "expiration", "strike", "right"]])
        self.manager = SubscriptionManager(cfg["max_symbols"])
        self.feed = OptionFeed(
            pd.Timedelta(seconds=cfg["quote_max_age_s"]),
            pd.Timedelta(seconds=cfg["quote_max_delivery_lag_s"]),
            pd.Timedelta(seconds=cfg["feed_silent_after_s"]),
            cfg["quote_lag_window"],
            pd.Timedelta(seconds=cfg["quote_clock_tolerance_s"]),
            cfg["quote_history_per_symbol"],
        )
        self.open_ts = pd.Timestamp(day.open_utc)
        self.stream = AlpacaOptionStream(
            cfg["feed"],
            creds.key_id,
            creds.secret_key,
            mode=mode,
            max_symbols=cfg["max_symbols"],
            tick_s=stream_cfg["tick_s"],
            backoff_s=tuple(stream_cfg["reconnect_backoff_s"]),
            ping_interval_s=stream_cfg["ping_interval_s"],
            handshake_timeout_s=stream_cfg["handshake_timeout_s"],
            on_messages=self.feed.on_messages,
            on_connected=self.feed.on_connected,
            on_disconnected=self.feed.on_disconnected,
            on_tick=lambda now: self.feed.tick(now, self.open_ts, bool(self.stream.confirmed)),
        )
        self.refresh(spot)

    def refresh(self, spot: float) -> None:
        # held positions and working orders come first and are never cut, wherever spot is
        plan = self.manager.plan(
            held=self.held_symbols(),
            watch=self.universe.window(spot, self.expirations, self.cfg["watch_strikes_each_side"]),
        )
        self.stream.set_symbols(plan.symbols)

    def on_bar(self, row: dict) -> None:
        """Re-centre the watch window on each finalized SPY close."""
        close = row.get("close")
        if close is not None and not pd.isna(close):
            self.refresh(float(close))

    async def run(self, until: pd.Timestamp) -> None:
        try:
            await self.stream.run(until)
        except (FatalStreamError, FeedGuardError) as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            log.error("option feed stopped: %s", self.error)

    def write(self, out: Path) -> dict:
        snaps = pd.DataFrame(self.feed.snapshots)
        if not snaps.empty:
            snaps.to_parquet(out / "option_quotes_1s.parquet", index=False)
        h = self.feed.health
        return {
            "feed": self.cfg["feed"],
            "error": self.error,
            "connects": self.stream.connects,
            "quotes": self.feed.book.counts,
            "delivery_lag_s": self.feed.book.lag_percentiles(),
            "symbols_confirmed": len(self.stream.confirmed),
            "stream_errors": [[str(t), c, m] for t, c, m in self.stream.errors][:20],
            "unhealthy": [
                [str(s) if s.year > 1 else None, None if e is None else str(e), r]
                for s, e, r in h.unhealthy
            ],
            "snapshot_rows": len(snaps),
            "date": date.isoformat(self.day.session_date),
        }
