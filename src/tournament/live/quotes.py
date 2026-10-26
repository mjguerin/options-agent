"""Option quote book and entry gates (MD-02, MD-04, EX-06; D-52).

Keeps the latest quote per contract with its exchange time and our receive time, and answers one
question for any contract: may a strategy rely on this quote now? A quote is unusable when the
option feed is down or silent, when no quote exists, when it is older than `max_age`, when it
reached us more than `max_delivery_lag` after its own timestamp (a delayed feed: BRD section 17,
the August 7 OPRA incident), or when the bid or ask is not a valid, uncrossed price. The system
never assumes a stale quote is executable.

Quote age is measured against the exchange timestamp. SPY options near the money quote
continuously, so a contract that has not updated for `max_age` is not priced reliably. Far
out-of-the-money contracts can fail this gate while quiet, which is the safe direction.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from tournament.live.events import ts_of
from tournament.live.health import FeedHealth

NO_QUOTE, FEED_DOWN = "NO_QUOTE", "FEED_DOWN"
STALE_QUOTE, DELAYED_DELIVERY = "STALE_QUOTE", "DELAYED_DELIVERY"
NO_BID, NO_ASK, CROSSED = "NO_BID", "NO_ASK", "CROSSED"


@dataclass(frozen=True)
class OptionQuote:
    symbol: str
    ts: pd.Timestamp  # exchange (source) time
    received: pd.Timestamp
    bid: float
    bid_size: int
    ask: float
    ask_size: int
    bid_exchange: str | None
    ask_exchange: str | None
    condition: str | None

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def delivery_lag_s(self) -> float:
        return (self.received - self.ts).total_seconds()


@dataclass(frozen=True)
class QuoteView:
    symbol: str
    quote: OptionQuote | None
    usable: bool
    reasons: tuple[str, ...]
    age_s: float | None


def quote_from(msg: dict, received: pd.Timestamp) -> OptionQuote:
    return OptionQuote(
        msg["S"],
        ts_of(msg["t"]),
        received,
        float(msg.get("bp") or 0.0),
        int(msg.get("bs") or 0),
        float(msg.get("ap") or 0.0),
        int(msg.get("as") or 0),
        msg.get("bx"),
        msg.get("ax"),
        msg.get("c"),
    )


@dataclass
class QuoteBook:
    max_age: pd.Timedelta
    max_delivery_lag: pd.Timedelta
    quotes: dict[str, OptionQuote] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=lambda: {"quotes": 0, "out_of_order": 0})
    _lags: deque = field(default_factory=lambda: deque(maxlen=5000))

    def on_quote(self, q: OptionQuote) -> None:
        self.counts["quotes"] += 1
        self._lags.append(q.delivery_lag_s)
        old = self.quotes.get(q.symbol)
        if old is not None and q.ts < old.ts:
            self.counts["out_of_order"] += 1  # a late older quote never replaces a newer one
            return
        self.quotes[q.symbol] = q

    def lag_percentiles(self) -> dict[str, float] | None:
        """Delivery lag (receive minus exchange time) over the last 5000 quotes (NFR-10)."""
        if not self._lags:
            return None
        a = np.array(self._lags)
        return {"p50": float(np.percentile(a, 50)), "p99": float(np.percentile(a, 99))}

    def view(self, symbol: str, now: pd.Timestamp, feed_ok: bool = True) -> QuoteView:
        q = self.quotes.get(symbol)
        if q is None:
            return QuoteView(symbol, None, False, (NO_QUOTE,), None)
        age = (now - q.ts).total_seconds()
        reasons = []
        if not feed_ok:
            reasons.append(FEED_DOWN)
        if age > self.max_age.total_seconds():
            reasons.append(STALE_QUOTE)
        if q.delivery_lag_s > self.max_delivery_lag.total_seconds():
            reasons.append(DELAYED_DELIVERY)
        if q.bid <= 0:
            reasons.append(NO_BID)
        if q.ask <= 0:
            reasons.append(NO_ASK)
        if q.bid > 0 and q.ask > 0 and q.bid > q.ask:
            reasons.append(CROSSED)
        return QuoteView(symbol, q, not reasons, tuple(reasons), age)


class OptionFeed:
    """Quote book plus connection health. `entries_allowed` is the feed-level gate (AC-08).
    Also keeps a one-second snapshot of every contract that updated (the full stream is
    thousands of quotes a second, too much to store): the last quote per contract per second."""

    def __init__(
        self, max_age: pd.Timedelta, max_delivery_lag: pd.Timedelta, silent_after: pd.Timedelta
    ) -> None:
        self.book = QuoteBook(max_age, max_delivery_lag)
        self.health = FeedHealth(silent_after)
        self.snapshots: list[dict] = []
        self._dirty: set[str] = set()
        self._second: int | None = None

    def on_connected(self, at: pd.Timestamp) -> None:
        self.health.on_connected(at)

    def on_disconnected(self, at: pd.Timestamp) -> None:
        self.health.on_disconnected(at)

    def on_messages(self, msgs: list[dict], received: pd.Timestamp) -> None:
        self.health.on_message(received)
        for m in msgs:
            if m.get("T") == "q":
                try:
                    q = quote_from(m, received)
                except (KeyError, TypeError, ValueError):
                    self.book.counts["malformed"] = self.book.counts.get("malformed", 0) + 1
                    continue  # one bad message never stops the feed
                self.book.on_quote(q)
                self._dirty.add(q.symbol)

    def _snapshot(self, second: int) -> None:
        for s in sorted(self._dirty):
            q = self.book.quotes[s]
            self.snapshots.append(
                {
                    "second": pd.Timestamp(second, unit="s", tz="UTC"),
                    "symbol": s,
                    "ts": q.ts,
                    "received": q.received,
                    "bid": q.bid,
                    "bid_size": q.bid_size,
                    "ask": q.ask,
                    "ask_size": q.ask_size,
                    "condition": q.condition,
                }
            )
        self._dirty = set()

    def tick(self, now: pd.Timestamp, regular_open: pd.Timestamp, expecting_data: bool) -> None:
        """Silence while connected and subscribed means a stuck feed. With nothing subscribed
        there is nothing to wait for."""
        sec = int(now.timestamp())
        if self._second is not None and sec > self._second:
            self._snapshot(self._second)
        self._second = sec
        if expecting_data:
            self.health.check_stale(now, regular_open)
        else:
            self.health.last_message_at = now

    def entries_allowed(self, now: pd.Timestamp) -> bool:
        h = self.health
        return h.connected and not (h.unhealthy and h.unhealthy[-1][1] is None)

    def view(self, symbol: str, now: pd.Timestamp) -> QuoteView:
        return self.book.view(symbol, now, self.entries_allowed(now))
