"""Daily loss limit and the premium stop (RK-01, RK-02, EX-02; D-56, D-58, D-95).

The limit counts realized loss plus, if configured, the marked loss of open positions at the
bid. A position with no usable bid counts as a total loss of its premium only once it has had no
usable bid for `unmarked_grace_s` (D-95): a quote that is crossed or late for a few seconds is
a data glitch, not a loss. The first time the total reaches the limit, entries halt for the
rest of the session and the halt never lifts when marks recover. A halt is not an instruction
to close (RK-02) unless `on_breach` says halt_and_flatten. The premium stop is an execution-only
exit (D-31): it never changes strategy state.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from tournament.risk.config import DailyLoss


@dataclass(frozen=True)
class OpenPosition:
    symbol: str
    quantity: int
    entry_price: float  # per share
    bid: float | None  # latest usable bid, None if there is none


@dataclass(frozen=True)
class LossStatus:
    loss: float  # positive = money lost today
    limit: float
    breached: bool
    entries_allowed: bool
    flatten: bool
    unmarked: int  # open positions valued by the unmarked rule
    unmarked_for_s: float = 0.0  # how long some position has had no usable bid, continuously


class DailyLossGuard:
    def __init__(self, cfg: DailyLoss, multiplier: int) -> None:
        self.cfg, self.multiplier = cfg, multiplier
        self.reset_session()

    def reset_session(self) -> None:
        self._unmarked_since: pd.Timestamp | None = None
        self.breached_at: pd.Timestamp | None = None
        self.breach_loss: float | None = (
            None  # the loss, by the configured measure, when it tripped
        )

    def risk_view(self, realized_pnl: float, positions: list[OpenPosition]) -> dict:
        """Three numbers kept apart, none of which touches the accounting: realized profit and
        loss; the estimate with open positions at their latest usable bid, leaving unmarked ones
        out; and the conservative risk figure that values an unmarked position at zero (the
        premium counted as lost). The last is what the daily limit reads."""
        estimated_open, risk_open, unmarked = 0.0, 0.0, 0
        for p in positions:
            if p.bid is None:
                unmarked += 1
                risk_open -= p.entry_price * p.quantity * self.multiplier  # zero liquidation value
            else:
                v = (p.bid - p.entry_price) * p.quantity * self.multiplier
                estimated_open += v
                risk_open += v
        return {
            "realized_usd": realized_pnl,
            "estimated_usd": realized_pnl + estimated_open,
            "risk_usd": realized_pnl + risk_open,
            "unmarked_positions": unmarked,
        }

    def _loss(
        self, realized_pnl: float, positions: list[OpenPosition], count_unmarked: bool = True
    ) -> tuple[float, int]:
        """The day's loss by the configured measure (positive = money lost), and how many open
        positions had no usable bid. With `count_unmarked` False those positions add nothing: the
        loss that is actually priced. Reads only."""
        open_pnl, unmarked = 0.0, 0
        if self.cfg.measure == "realized_plus_open_at_bid":
            for p in positions:
                if p.bid is None:
                    unmarked += 1
                    if count_unmarked and self.cfg.unmarked_position == "full_loss":
                        open_pnl -= p.entry_price * p.quantity * self.multiplier
                else:
                    open_pnl += (p.bid - p.entry_price) * p.quantity * self.multiplier
        return -(realized_pnl + open_pnl), unmarked

    def headroom(self, realized_pnl: float, positions: list[OpenPosition]) -> float:
        """How much more the day can lose before the limit trips. Changes nothing; a gain today
        adds room, a loss uses it (D-92)."""
        loss, _ = self._loss(realized_pnl, positions)
        return self.cfg.limit_usd - loss

    def update(
        self, now: pd.Timestamp, realized_pnl: float, positions: list[OpenPosition]
    ) -> LossStatus:
        conservative, unmarked = self._loss(realized_pnl, positions)
        if unmarked:
            if self._unmarked_since is None:
                self._unmarked_since = now
        else:
            self._unmarked_since = None  # a usable bid resets the clock
        blind_for = (
            0.0 if self._unmarked_since is None else (now - self._unmarked_since).total_seconds()
        )
        # until the grace has run out, only what is actually priced can trip the limit
        loss = (
            conservative
            if blind_for >= self.cfg.unmarked_grace_s
            else self._loss(realized_pnl, positions, count_unmarked=False)[0]
        )
        if self.breached_at is None and loss >= self.cfg.limit_usd - 1e-9:
            self.breached_at = now
            self.breach_loss = loss
        breached = self.breached_at is not None
        return LossStatus(
            loss,
            self.cfg.limit_usd,
            breached,
            not breached,
            breached and self.cfg.on_breach == "halt_and_flatten",
            unmarked,
            blind_for,
        )


def premium_stop_hit(entry_price: float, bid: float | None, loss_pct: float) -> bool:
    """True when the bid is at or below entry x (1 - loss_pct). No usable bid never fires it:
    the data-failure policy for exits is separate (EX-06)."""
    return bid is not None and bid <= entry_price * (1 - loss_pct) + 1e-9
