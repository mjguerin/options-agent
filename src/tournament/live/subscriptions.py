"""Which option contracts to hold quotes for (MD-08, MD-10).

Priority: contracts of open positions (their exits must never lose quote coverage), then
contracts of live candidates, then the watch set. Only SPY options are accepted (MD-10). The
total is capped at the provider's symbol limit; held contracts are never cut, so if they alone
exceed the cap the plan says so and the engine records an incident.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from tournament.appconfig import app_config


def parse_occ(symbol: str) -> tuple[date, str, float] | None:
    """(expiration, right, strike) of an OCC symbol of the configured underlying, or None for
    any other symbol (MD-10)."""
    m = re.match(rf"^{re.escape(app_config().underlying)}(\d{{6}})([CP])(\d{{8}})$", symbol)
    if not m:
        return None
    d = m.group(1)
    return date(2000 + int(d[:2]), int(d[2:4]), int(d[4:])), m.group(2), int(m.group(3)) / 1000


@dataclass(frozen=True)
class Plan:
    symbols: frozenset[str]
    rejected: tuple[str, ...] = ()  # not SPY options
    dropped: tuple[str, ...] = ()  # cut by the cap
    held_overflow: bool = False  # held contracts alone exceed the cap: an incident


@dataclass
class SubscriptionManager:
    max_symbols: int
    history: list[Plan] = field(default_factory=list)

    def plan(
        self,
        held: Iterable[str] = (),
        candidates: Iterable[str] = (),
        watch: Iterable[str] = (),
    ) -> Plan:
        chosen: list[str] = []
        rejected: list[str] = []
        dropped: list[str] = []
        seen: set[str] = set()
        overflow = False
        for tier, symbols in (("held", held), ("candidates", candidates), ("watch", watch)):
            for s in symbols:
                if s in seen:
                    continue
                seen.add(s)
                if parse_occ(s) is None:
                    rejected.append(s)
                elif tier == "held":
                    chosen.append(s)  # never cut
                    overflow |= len(chosen) > self.max_symbols
                elif len(chosen) < self.max_symbols:
                    chosen.append(s)
                else:
                    dropped.append(s)
        plan = Plan(frozenset(chosen), tuple(rejected), tuple(dropped), overflow)
        self.history.append(plan)
        return plan


@dataclass
class OptionUniverse:
    """Today's SPY contracts and the strikes around the underlying (the pre-D06 watch set)."""

    contracts: pd.DataFrame  # symbol, expiration, strike, right

    def window(self, spot: float, expirations: set[date], each_side: int) -> list[str]:
        """The `each_side` strikes at or below and above spot, calls and puts, for the given
        expirations, nearest strikes first. Deterministic: ties go to the lower strike."""
        c = self.contracts[self.contracts["expiration"].isin(expirations)]
        strikes = sorted(c["strike"].unique())
        below = [k for k in strikes if k <= spot][-each_side:]
        above = [k for k in strikes if k > spot][:each_side]
        keep = sorted({*below, *above}, key=lambda k: (abs(k - spot), k))
        out = c[c["strike"].isin(keep)].copy()
        out["_d"] = (out["strike"] - spot).abs()
        out = out.sort_values(["_d", "strike", "right", "symbol"], kind="stable")
        return out["symbol"].tolist()
