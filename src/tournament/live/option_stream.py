"""Alpaca OPRA option-quote websocket client (MD-01, MD-08, MD-09, MD-10).

The option stream speaks MessagePack only (error 412 for JSON) and refuses wildcard quote
subscriptions (413), so every symbol is named. The desired symbol set is changed at any time
with `set_symbols`; the pump sends the difference, and the server's confirmation tells us what is
really subscribed. After a reconnect the whole set is subscribed again.

Feed guard (MD-09, AC-23): paper and shadow modes require the `opra` feed. The free `indicative`
feed is for code testing only and is rejected at startup in those modes. alpaca-py defaults to
`indicative`, which is exactly why the guard is explicit here.
"""

from __future__ import annotations

import logging

import msgpack

from tournament.appconfig import app_config
from tournament.live.stream import MODES, FeedGuardError, MarketDataStream

log = logging.getLogger(__name__)

FEEDS = ("opra", "indicative")
SYMBOL_LIMIT_CODE = 405  # "symbol limit exceeded"
WILDCARD_CODE = 413


def option_url(feed: str) -> str:
    if feed not in FEEDS:
        raise FeedGuardError(f"unknown option feed {feed!r}")
    return f"{app_config().endpoints.option_stream_base}/{feed}"


def check_option_feed(feed: str, mode: str) -> None:
    """Fail closed at startup: only real-time OPRA may drive shadow or paper decisions."""
    if mode not in MODES:
        raise FeedGuardError(f"unknown mode {mode!r}")
    if feed not in FEEDS:
        raise FeedGuardError(f"unknown option feed {feed!r}")
    if mode in ("shadow", "paper") and feed != "opra":
        raise FeedGuardError(
            f"{mode} mode requires the real-time OPRA option feed; {feed!r} is for code testing"
        )


class AlpacaOptionStream(MarketDataStream):
    content_type = "application/msgpack"

    def __init__(
        self,
        feed: str,
        key_id: str,
        secret: str,
        *,
        mode: str,
        max_symbols: int,
        **kw,
    ) -> None:
        check_option_feed(feed, mode)
        super().__init__(option_url(feed), key_id, secret, **kw)
        self.feed, self.max_symbols = feed, max_symbols
        self.desired: set[str] = set()
        self.confirmed: set[str] = set()
        self._sent: set[str] = set()

    def encode(self, obj: dict) -> bytes:
        return msgpack.packb(obj)

    def decode(self, raw: bytes | str) -> list[dict]:
        return msgpack.unpackb(raw, timestamp=2)  # MessagePack timestamps as integer ns

    def set_symbols(self, symbols: set[str] | frozenset[str]) -> None:
        """The full set of contracts to hold quotes for. Capped here as a last line of defence;
        the subscription manager (subscriptions.py) decides what makes the cut."""
        if len(symbols) > self.max_symbols:
            raise FeedGuardError(f"{len(symbols)} symbols exceeds the limit of {self.max_symbols}")
        if "*" in symbols:
            raise FeedGuardError("wildcard option quote subscriptions are refused")
        self.desired = set(symbols)

    async def on_authenticated(self, ws) -> None:
        self._sent, self.confirmed = set(), set()  # a new connection starts empty
        await self.sync(ws)

    async def sync(self, ws) -> None:
        add, drop = self.desired - self._sent, self._sent - self.desired
        if drop:
            await ws.send(self.encode({"action": "unsubscribe", "quotes": sorted(drop)}))
            self._sent -= drop
        if add:
            await ws.send(self.encode({"action": "subscribe", "quotes": sorted(add)}))
            self._sent |= add

    def on_subscription(self, msg: dict) -> None:
        self.confirmed = set(msg.get("quotes") or ())

    @property
    def limit_errors(self) -> int:
        return sum(1 for _, code, _ in self.errors if code == SYMBOL_LIMIT_CODE)
