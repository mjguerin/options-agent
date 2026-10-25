"""Helpers shared by the five strategy workers.

Orientation: each worker writes its logic once, for the bullish case, on ORIENTED prices.
For orientation s = +1 prices are unchanged; for s = -1 every price x becomes -x, so a bar's
oriented high is -low and its oriented low is -high, swing lows become oriented swing highs,
and "above" means "below". Converting a result back is just multiplying by s again. This makes
every bearish mirror exact by construction (specs: "bearish is the exact mirror image").
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from tournament.marketdata.bars import PRESENT
from tournament.strategies.base import Outcome, SlotContext, StrategyWorker, Transition

CONFIG_DIR = Path(__file__).resolve().parents[3] / "config" / "strategies"
BULLISH, BEARISH = "bullish", "bearish"


def load_config(strategy_id: str) -> dict[str, Any]:
    return yaml.safe_load((CONFIG_DIR / f"{strategy_id}-v1.yaml").read_text())


@dataclass(frozen=True)
class OBar:
    """One slot in oriented prices. Price fields are None when the slot has no price."""

    slot: int
    high: float | None
    low: float | None
    close: float | None
    vwap: float | None
    a: float | None
    priced: bool


def obar(row: pd.Series, s: int) -> OBar:
    priced = row["interval_status"] == PRESENT and pd.notna(row["close"])

    def val(x):
        return None if x is None or pd.isna(x) else float(x)

    hi, lo, c = val(row["high"]), val(row["low"]), val(row["close"])
    vw = val(row["session_vwap"])
    if s < 0:
        hi, lo = (None if lo is None else -lo), (None if hi is None else -hi)
        c = None if c is None else -c
        vw = None if vw is None else -vw
    return OBar(
        int(row["slot"]),
        hi if priced else None,
        lo if priced else None,
        c if priced else None,
        vw,
        val(row["a_cur"]),
        priced,
    )


def at(history: pd.DataFrame, slot: int) -> pd.Series:
    """The row for `slot` (history holds slots 1..m in order)."""
    return history.iloc[slot - 1]


def oriented_swings(row: pd.Series, s: int, upper: bool) -> list[dict]:
    """Swings confirmed on this row, oriented. upper=True: swing highs for s=+1, swing lows for
    s=-1 (as oriented highs). Each item: pivot_slot, price (oriented), confirmed_slot."""
    want = ("high" if s > 0 else "low") if upper else ("low" if s > 0 else "high")
    out = []
    for sw in row["swings_new"] if isinstance(row["swings_new"], list) else []:
        if sw["side"] == want:
            out.append(
                {
                    "pivot_slot": sw["pivot_slot"],
                    "price": s * sw["price"],
                    "confirmed_slot": sw["confirmed_slot"],
                    "swing_id": sw["swing_id"],
                }
            )
    return out


def direction(s: int) -> str:
    return BULLISH if s > 0 else BEARISH


class WorkerBase(StrategyWorker):
    """Config loading, setup ids, readiness, and per-slot outcome/transition bookkeeping."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or load_config(self.strategy_id)
        super().__init__(cfg)
        self.version = cfg["version"]
        self.required_features = tuple(cfg["required_features"])
        self.p = cfg["params"]
        self.session_id = ""
        self._ordinal = 0

    def reset_session(self, session_id: str) -> None:
        self.session_id = session_id
        self._ordinal = 0
        self.reset()

    def reset(self) -> None:  # per-session state, overridden by each worker
        pass

    def new_setup(self) -> tuple[str, int]:
        self._ordinal += 1
        return f"{self.session_id}:{self.strategy_id}:{self._ordinal}", self._ordinal

    def missing(self, ctx: SlotContext) -> list[str]:
        nr = ctx.row["not_ready"]
        return [f for f in self.required_features if f in (nr if isinstance(nr, list) else [])]

    def last_entry(self, ctx: SlotContext) -> int:
        own = self.p.get("last_entry_slot")
        return min(own, ctx.last_entry_slot) if own is not None else ctx.last_entry_slot


class SlotLog:
    """Collects one slot's transitions and decides the slot's outcome (SD-12)."""

    def __init__(self) -> None:
        self.transitions: list[Transition] = []
        self.invalidated = False
        self.reasons: list[str] = []

    def move(
        self,
        frm: str,
        to: str,
        outcome: Outcome,
        reasons: tuple[str, ...],
        setup_id: str | None,
        anchors: dict | None = None,
    ) -> None:
        self.transitions.append(Transition(frm, to, outcome, reasons, setup_id, anchors or {}))
        if outcome == Outcome.INVALIDATED:
            self.invalidated = True
        self.reasons.extend(reasons)
