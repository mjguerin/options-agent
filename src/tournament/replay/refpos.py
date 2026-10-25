"""Underlying reference positions and their exits (specs/shared.md SD-11; schemas.md S-8).

The reference position is the strategy's own deterministic state, identical in replay and
paper. It opens when a candidate is emitted (entry = signal-bar close: a bookkeeping anchor, not
a fill) and closes only on reference exits. Execution exits never touch it (D-31, D-33).

Every exit is close-based and evaluated on finalized slots after the signal slot e. Timers count
scheduled slots from e (D-05); a deadline's final slot is evaluated for progress first.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import pandas as pd

from tournament.strategies.base import CandidateSpec

SESSION_CUTOFF, INVALIDATION, TARGET, TRAIL = "SESSION_CUTOFF", "INVALIDATION", "TARGET", "TRAIL"
FOLLOW_THROUGH, STAGNATION, MAX_HOLD = "FOLLOW_THROUGH", "STAGNATION", "MAX_HOLD"


@dataclass
class ReferencePosition:
    reference_position_id: str
    candidate_id: str
    spec: CandidateSpec
    entry_slot: int
    entry_time: pd.Timestamp
    precedence: tuple[str, ...]
    r_u: float = field(init=False)
    sign: int = field(init=False)  # +1 bullish, -1 bearish
    best_close: float = field(init=False)
    slots_since_best: int = 0
    reached_ft: bool = False
    trail_active: bool = False
    trail_stop: float | None = None
    last_close: float | None = None
    trail_history: list[tuple[int, float]] = field(default_factory=list)
    post_entry_swings: list[dict] = field(default_factory=list)
    exit_slot: int | None = None
    exit_time: pd.Timestamp | None = None
    exit_price: float | None = None
    exit_price_stale: bool = False
    primary_reason: str | None = None
    triggered_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        s = self.spec
        self.sign = 1 if s.underlying_direction == "bullish" else -1
        self.r_u = abs(s.entry_reference - s.invalidation_level)
        self.best_close = s.entry_reference
        self.last_close = s.entry_reference

    @property
    def open(self) -> bool:
        return self.exit_slot is None

    def _beyond(self, price: float, level: float) -> bool:
        """At or beyond `level` in the trade's favour (bullish: >=)."""
        return self.sign * (price - level) >= 0

    def evaluate(self, slot: int, row: pd.Series, cutoff_slot: int) -> bool:
        """Check reference exits on one finalized slot after entry. Returns True if it closed."""
        s, t = self.spec, self.spec.timers
        close = (
            row["close"] if row["interval_status"] == "PRESENT" and pd.notna(row["close"]) else None
        )
        held = slot - self.entry_slot
        triggered = []
        if slot >= cutoff_slot:
            triggered.append(SESSION_CUTOFF)
        if close is not None:
            if not self._beyond(close, s.invalidation_level) or close == s.invalidation_level:
                triggered.append(INVALIDATION)
            if self._beyond(close, s.target["price"]):
                triggered.append(TARGET)
            if (
                self.trail_active
                and self.trail_stop is not None
                and (not self._beyond(close, self.trail_stop) or close == self.trail_stop)
            ):
                triggered.append(TRAIL)
            ft_r = t.get("ft_r")
            if ft_r is not None and self._beyond(
                close, s.entry_reference + self.sign * ft_r * self.r_u
            ):
                self.reached_ft = True  # the deadline slot itself counts as progress
            if self.sign * (close - self.best_close) > 0:
                self.best_close, self.slots_since_best = close, 0
            else:
                self.slots_since_best += 1
            self.last_close = close
        else:
            self.slots_since_best += 1
        if t.get("ft_bars") is not None and held == t["ft_bars"] and not self.reached_ft:
            triggered.append(FOLLOW_THROUGH)
        if t.get("stag_bars") is not None and self.slots_since_best >= t["stag_bars"]:
            triggered.append(STAGNATION)
        if t.get("max_hold") is not None and held >= t["max_hold"]:
            triggered.append(MAX_HOLD)

        if triggered:
            self.triggered_reasons = tuple(r for r in self.precedence if r in triggered)
            self.primary_reason = self.triggered_reasons[0]
            self.exit_slot, self.exit_time = slot, row["bar_end"]
            self.exit_price = close if close is not None else self.last_close
            self.exit_price_stale = close is None
            return True
        self._update_trail(slot, row, close)
        return False

    def _update_trail(self, slot: int, row: pd.Series, close: float | None) -> None:
        """After the slot's checks (SD-11): record post-entry swings confirmed on this slot;
        activate after the first close beyond +activate_r; once active, ratchet the stop to the
        latest post-entry confirmed swing minus the buffer. Monotonic: it never loosens."""
        tr = self.spec.trail
        if tr.get("type") != "swing_trail":
            return
        want = "low" if self.sign > 0 else "high"
        for sw in row["swings_new"] if isinstance(row["swings_new"], list) else []:
            if sw["side"] == want and sw["pivot_slot"] > self.entry_slot:
                self.post_entry_swings.append(sw)
        if (
            not self.trail_active
            and close is not None
            and self._beyond(
                close, self.spec.entry_reference + self.sign * tr["activate_r"] * self.r_u
            )
        ):
            self.trail_active = True
        if not self.trail_active or not self.post_entry_swings:
            return
        latest = max(
            self.post_entry_swings, key=lambda sw: (sw["confirmed_slot"], sw["pivot_slot"])
        )
        level = latest["price"] - self.sign * tr["buffer"]
        if self.trail_stop is None or self.sign * (level - self.trail_stop) > 0:
            self.trail_stop = level
            self.trail_history.append((slot, level))


def geometry_ok(spec: CandidateSpec) -> tuple[bool, str | None]:
    """schemas.md S-7: bullish invalidation < entry < target; bearish the reverse; r_u > 0."""
    e, inv, tgt = spec.entry_reference, spec.invalidation_level, spec.target.get("price")
    if any(v is None or not math.isfinite(v) for v in (e, inv, tgt)):
        return False, "NON_FINITE"
    if spec.underlying_direction == "bullish":
        ok = inv < e < tgt
    elif spec.underlying_direction == "bearish":
        ok = tgt < e < inv
    else:
        return False, "BAD_DIRECTION"
    return (True, None) if ok else (False, "BAD_GEOMETRY")
