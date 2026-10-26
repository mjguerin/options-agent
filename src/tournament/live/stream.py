"""Alpaca stock market-data websocket client (MD-01, MD-09, NFR-10).

Our own thin client instead of alpaca-py's, because the session needs every connect and
disconnect time (they decide interval status), trade corrections and cancels, and the exact
nanosecond timestamps. One connection per login and feed is allowed by Alpaca, so the engine
holds exactly one and fans the data out (FR-03).

Feed guard: paper and shadow modes accept only the SIP URL. The test URL (a synthetic symbol,
available around the clock) is accepted only in test mode.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime

import pandas as pd
import websockets

log = logging.getLogger(__name__)

SIP_URL = "wss://stream.data.alpaca.markets/v2/sip"
TEST_URL = "wss://stream.data.alpaca.markets/v2/test"
MODES = ("shadow", "paper", "test")


class FeedGuardError(RuntimeError):
    pass


class FatalStreamError(RuntimeError):
    """Authentication or entitlement failures: retrying can't help."""


def check_feed(url: str, mode: str) -> None:
    if mode not in MODES:
        raise FeedGuardError(f"unknown mode {mode!r}")
    expected = TEST_URL if mode == "test" else SIP_URL
    if url != expected:
        raise FeedGuardError(f"{mode} mode requires {expected}, got {url}")


def now_utc() -> pd.Timestamp:
    return pd.Timestamp(datetime.now(UTC))


class AlpacaStockStream:
    def __init__(
        self,
        url: str,
        key_id: str,
        secret: str,
        symbols: list[str],
        *,
        mode: str,
        on_messages: Callable[[list[dict], pd.Timestamp], None],
        on_connected: Callable[[pd.Timestamp], None],
        on_disconnected: Callable[[pd.Timestamp], None],
        on_tick: Callable[[pd.Timestamp], None],
        on_raw: Callable[[str, pd.Timestamp], None] | None = None,
        tick_s: float = 0.25,
        backoff_s: tuple[float, ...] = (1, 2, 4, 8, 15, 30),
        clock: Callable[[], pd.Timestamp] = now_utc,
    ) -> None:
        check_feed(url, mode)
        self.url, self._key, self._secret, self.symbols = url, key_id, secret, symbols
        self.on_messages, self.on_connected = on_messages, on_connected
        self.on_disconnected, self.on_tick, self.on_raw = on_disconnected, on_tick, on_raw
        self.tick_s, self.backoff_s, self.clock = tick_s, backoff_s, clock
        self.connects = 0

    async def run(self, until: pd.Timestamp) -> None:
        attempt = 0
        while self.clock() < until:
            try:
                async with websockets.connect(self.url, max_size=None, ping_interval=10) as ws:
                    await self._handshake(ws)
                    self.connects += 1
                    attempt = 0
                    self.on_connected(self.clock())
                    await self._pump(ws, until)
                    return
            except FatalStreamError:
                raise
            except (OSError, websockets.ConnectionClosed, TimeoutError) as exc:
                at = self.clock()
                log.warning("stream disconnected: %s", type(exc).__name__)
                self.on_disconnected(at)
            delay = self.backoff_s[min(attempt, len(self.backoff_s) - 1)]
            attempt += 1
            await self._wait(delay, until)

    async def _wait(self, seconds: float, until: pd.Timestamp) -> None:
        end = self.clock() + pd.Timedelta(seconds=seconds)
        while self.clock() < min(end, until):
            self.on_tick(self.clock())
            await asyncio.sleep(self.tick_s)

    async def _expect(self, ws, ok: Callable[[dict], bool], what: str) -> None:
        msgs = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        for m in msgs:
            if m.get("T") == "error":
                # 401/402 auth, 403 already authenticated, 405 symbol limit, 406 connection
                # limit, 409 insufficient subscription: none is fixed by reconnecting
                raise FatalStreamError(f"{what}: {m.get('code')} {m.get('msg')}")
            if ok(m):
                return
        raise FatalStreamError(f"{what}: unexpected reply {msgs}")

    async def _handshake(self, ws) -> None:
        await self._expect(ws, lambda m: m.get("msg") == "connected", "connect")
        await ws.send(json.dumps({"action": "auth", "key": self._key, "secret": self._secret}))
        await self._expect(ws, lambda m: m.get("msg") == "authenticated", "auth")
        sub = {"action": "subscribe", "trades": self.symbols, "statuses": self.symbols}
        await ws.send(json.dumps(sub))
        await self._expect(ws, lambda m: m.get("T") == "subscription", "subscribe")

    async def _pump(self, ws, until: pd.Timestamp) -> None:
        last_tick = self.clock()
        while True:
            now = self.clock()
            if now >= until:
                return
            if (now - last_tick).total_seconds() >= self.tick_s:
                self.on_tick(now)
                last_tick = now
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=self.tick_s)
            except TimeoutError:
                continue
            received = self.clock()
            if self.on_raw:
                self.on_raw(raw, received)
            msgs = json.loads(raw)
            for m in msgs:
                if m.get("T") == "error":
                    log.error("stream error %s %s", m.get("code"), m.get("msg"))
            self.on_messages(msgs, received)
