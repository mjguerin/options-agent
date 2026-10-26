"""Contract selection (CS-01 to CS-03, CS-07; D-57).

One fixed rule from the config, applied before and independently of size (CS-01A): the right
follows the signal direction, the expiry is `expiry_days` ahead, the strike is the one nearest
the underlying (shifted by `strike_offset` strikes toward in-the-money, if set). That one
contract either passes the quote and spread checks or the signal is rejected with reasons.
There is no search for a cheaper or wider alternative: that would let the budget choose the
contract.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd

from tournament.live.quotes import OptionQuote, QuoteView
from tournament.risk.config import ContractSelection

NO_EXPIRY, NO_STRIKE = "NO_EXPIRY", "NO_STRIKE"
SPREAD_TOO_WIDE_ABS, SPREAD_TOO_WIDE_PCT = "SPREAD_TOO_WIDE_ABS", "SPREAD_TOO_WIDE_PCT"
PREMIUM_TOO_LOW, ASK_SIZE_TOO_SMALL = "PREMIUM_TOO_LOW", "ASK_SIZE_TOO_SMALL"


@dataclass(frozen=True)
class Selection:
    symbol: str | None
    right: str
    strike: float | None
    expiration: date | None
    quote: OptionQuote | None
    reasons: tuple[str, ...]  # empty when selected; otherwise why the signal has no contract

    @property
    def selected(self) -> bool:
        return not self.reasons


def select_contract(
    direction: str,
    spot: float,
    today: date,
    contracts: pd.DataFrame,  # symbol, expiration, strike, right
    view: Callable[[str], QuoteView],
    cfg: ContractSelection,
) -> Selection:
    right = "C" if direction == "bullish" else "P"
    expiration = today + timedelta(days=cfg.expiry_days)
    chain = contracts[(contracts["expiration"] == expiration) & (contracts["right"] == right)]
    if chain.empty:
        return Selection(None, right, None, expiration, None, (NO_EXPIRY,))
    chain = chain.sort_values("strike", kind="stable").reset_index(drop=True)
    strikes = chain["strike"].to_numpy()
    base = int(min(range(len(strikes)), key=lambda i: (abs(strikes[i] - spot), strikes[i])))
    # in the money is lower strikes for calls, higher for puts
    idx = base - cfg.strike_offset if right == "C" else base + cfg.strike_offset
    if not 0 <= idx < len(chain):
        return Selection(None, right, None, expiration, None, (NO_STRIKE,))
    row = chain.iloc[idx]
    v = view(row["symbol"])
    reasons = list(v.reasons)
    q = v.quote
    if q is not None and q.bid > 0 and q.ask > 0:
        if q.spread > cfg.max_spread_abs + 1e-9:
            reasons.append(SPREAD_TOO_WIDE_ABS)
        if q.spread / q.mid > cfg.max_spread_pct + 1e-9:
            reasons.append(SPREAD_TOO_WIDE_PCT)
    if q is not None:
        if q.ask < cfg.min_ask:
            reasons.append(PREMIUM_TOO_LOW)
        if q.ask_size < cfg.min_ask_size:
            reasons.append(ASK_SIZE_TOO_SMALL)
    return Selection(
        row["symbol"], right, float(row["strike"]), expiration, q, tuple(dict.fromkeys(reasons))
    )
