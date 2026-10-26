"""Alpaca stock-stream messages as typed inputs (MD-02).

Alpaca sends JSON arrays of messages. Kinds used here: `t` trade, `c` trade correction,
`x` trade cancel or error, `s` trading status. Timestamps keep the nanosecond precision of the
RFC 3339 strings.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

# Trading status codes that stop trading until a `T` (trading resumption) arrives.
# `H` halt, `P` volatility (LULD) pause. `Q` is a quotation resumption: still no trading.
HALT_CODES = frozenset({"H", "P"})
RESUME_CODES = frozenset({"T"})


@dataclass(frozen=True)
class TradeIn:
    ts: pd.Timestamp
    price: float
    size: int
    exchange: str
    conditions: tuple[str, ...]
    trade_id: int | None
    tape: str | None

    @property
    def key(self) -> tuple[str, int] | None:
        """Source identity: SIP trade IDs are per exchange (S-2 `event_key`)."""
        return None if self.trade_id is None else (self.exchange, self.trade_id)

    def as_row(self) -> dict:
        return {
            "ts": self.ts,
            "price": self.price,
            "size": self.size,
            "exchange": self.exchange,
            "conditions": list(self.conditions),
            "trade_id": self.trade_id,
            "tape": self.tape,
        }


@dataclass(frozen=True)
class Correction:
    """A cancel (`corrected` is None) or a correction of an earlier trade."""

    original: tuple[str, int]
    corrected: TradeIn | None
    ts: pd.Timestamp


@dataclass(frozen=True)
class Status:
    code: str
    ts: pd.Timestamp


def ts_of(value: str) -> pd.Timestamp:
    return pd.Timestamp(value).tz_convert("UTC")


def trade_from(msg: dict) -> TradeIn:
    return TradeIn(
        ts=ts_of(msg["t"]),
        price=float(msg["p"]),
        size=int(msg["s"]),
        exchange=msg["x"],
        conditions=tuple(msg.get("c") or ()),
        trade_id=msg.get("i"),
        tape=msg.get("z"),
    )


def correction_from(msg: dict) -> Correction:
    ts = ts_of(msg["t"])
    corrected = TradeIn(
        ts=ts,
        price=float(msg["cp"]),
        size=int(msg["cs"]),
        exchange=msg["x"],
        conditions=tuple(msg.get("cc") or ()),
        trade_id=msg.get("ci"),
        tape=msg.get("z"),
    )
    return Correction((msg["x"], msg["oi"]), corrected, ts)


def cancel_from(msg: dict) -> Correction:
    return Correction((msg["x"], msg["i"]), None, ts_of(msg["t"]))


def status_from(msg: dict) -> Status:
    return Status(msg["sc"], ts_of(msg["t"]))
