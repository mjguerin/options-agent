"""VMR: VWAP mean reversion (specs/strategies/vmr.md v0.4).

Oriented by f: f=+1 is a stretch ABOVE VWAP (a bearish trade); f=-1 a stretch below (bullish).
d'(m) = f * (close - VWAP) / A_cur. RANGE only; leaving RANGE cancels. The
directional_invalidation_brake stops a direction for the session after a reference exit whose
TRIGGERED reasons include INVALIDATION (D-11).
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from tournament.strategies.base import (
    CandidateSpec,
    Decision,
    Outcome,
    ReferenceExitEvent,
    SlotContext,
)
from tournament.strategies.common import (
    BEARISH,
    BULLISH,
    SlotLog,
    WorkerBase,
    at,
    obar,
    oriented_swings,
)

RANGE = "RANGE"


def trade_direction(f: int) -> str:
    return BEARISH if f > 0 else BULLISH


@dataclass
class VmrSetup:
    f: int
    ep_start: int
    a: float
    setup_id: str
    ordinal: int
    state: str = "EXTENDED"
    s_high: float | None = None  # oriented S
    s_low: float | None = None  # oriented low of the pivot bar
    c: int | None = None


class VMR(WorkerBase):
    strategy_id = "vmr"

    def reset(self) -> None:
        self.setups: dict[int, VmrSetup | None] = {1: None, -1: None}
        self.braked = {1: False, -1: False}
        self.count = {1: 0, -1: 0}

    def on_reference_exit(self, event: ReferenceExitEvent) -> None:
        if "INVALIDATION" in event.triggered_reasons:  # triggered, not just primary
            self.braked[1 if event.underlying_direction == BEARISH else -1] = True

    def _d(self, row: pd.Series, f: int) -> float | None:
        c, v, a = row["close"], row["session_vwap"], row["a_cur"]
        if row["interval_status"] != "PRESENT" or any(pd.isna(z) for z in (c, v, a)) or a <= 0:
            return None
        return f * (c - v) / a

    def _state(self) -> str:
        return "|".join(
            f"{trade_direction(f)}:{x.state if x else 'IDLE'}"
            + (":BRAKED" if self.braked[f] else "")
            for f, x in self.setups.items()
        )

    def _end(self, log: SlotLog, f: int, reason: str) -> None:
        x = self.setups[f]
        log.move(
            x.state, "IDLE", Outcome.INVALIDATED, (reason,), x.setup_id, {"ep_start": x.ep_start}
        )
        self.setups[f] = None

    def evaluate(self, ctx: SlotContext) -> Decision:
        log, p, m, row = SlotLog(), self.p, ctx.slot, ctx.row
        miss = self.missing(ctx)
        regime = row["regime"]
        if ctx.cancel_reason or miss or m > self.last_entry(ctx) or regime != RANGE:
            reason = (
                f"DATA_{ctx.cancel_reason}"
                if ctx.cancel_reason
                else "NOT_READY"
                if miss
                else "WINDOW_CLOSED"
                if m > self.last_entry(ctx)
                else "REGIME_EXCLUDED"
            )
            for f in (1, -1):
                if self.setups[f]:
                    self._end(log, f, reason)
            outcome = (
                Outcome.NOT_READY
                if miss
                else Outcome.INVALIDATED
                if log.invalidated
                else Outcome.INELIGIBLE
            )
            return Decision(
                outcome,
                self._state(),
                tuple(log.reasons) or (reason,),
                transitions=tuple(log.transitions),
            )

        candidate = None
        for f in (1, -1):
            x = self.setups[f]
            if x is None:
                continue
            b = obar(row, f)
            if m > x.ep_start + p["ep_max_age"]:
                self._end(log, f, "SETUP_AGE")
                continue
            if x.state == "EXTENDED":
                for sw in oriented_swings(row, f, upper=True):
                    s = sw["pivot_slot"]
                    ds = self._d(at(ctx.history, s), f)
                    if s >= x.ep_start and ds is not None and ds >= p["ext_min"] - p["exh_tol"]:
                        x.state, x.s_high, x.c = "EXHAUSTED", sw["price"], m
                        x.s_low = obar(at(ctx.history, s), f).low
                        log.move(
                            "EXTENDED",
                            "EXHAUSTED",
                            Outcome.WATCHING,
                            ("TURN_CONFIRMED",),
                            x.setup_id,
                            {"pivot_slot": s, "S": f * x.s_high, "S_low": f * x.s_low},
                        )
                        break
            if x.state == "EXHAUSTED":  # also on the confirmation slot itself (explicit exception)
                if b.priced and b.high > x.s_high and m > x.c:
                    x.state, x.s_high, x.s_low, x.c = "EXTENDED", None, None, None
                    log.move(
                        "EXHAUSTED", "EXTENDED", Outcome.WATCHING, ("NEW_EXTREME",), x.setup_id
                    )
                    continue
                if b.priced and b.close < x.s_low and ctx.may_emit and candidate is None:
                    candidate = self._candidate(ctx, x, b.close, log)
                    if candidate is not None or self.setups[f] is None:
                        continue
                if m >= x.c + p["trig_window"]:
                    self._end(log, f, "TRIGGER_TIMEOUT")

        if candidate is not None:
            for f in (1, -1):
                if self.setups[f]:
                    self._end(log, f, "POSITION_OPENED")
            return Decision(
                Outcome.CANDIDATE,
                self._state(),
                tuple(log.reasons),
                candidate.setup_id,
                tuple(log.transitions),
                candidate,
            )

        if not ctx.blocks and m >= p["first_slot"]:
            for f in (1, -1):
                if (
                    self.setups[f] is None
                    and not self.braked[f]
                    and self.count[f] < p["max_cand_dir"]
                ):
                    d_now, d_prev = self._d(row, f), self._prev_d(ctx, f)
                    if d_now is not None and d_prev is not None and d_now >= p["ext_min"] > d_prev:
                        sid, ordinal = self.new_setup()
                        self.setups[f] = VmrSetup(f, m, float(row["a_cur"]), sid, ordinal)
                        log.move(
                            "IDLE",
                            "EXTENDED",
                            Outcome.WATCHING,
                            ("STRETCH",),
                            sid,
                            {"d": d_now, "a_setup": float(row["a_cur"])},
                        )
        outcome = (
            Outcome.INVALIDATED
            if log.invalidated
            else (Outcome.WATCHING if not ctx.blocks else Outcome.INELIGIBLE)
        )
        return Decision(
            outcome,
            self._state(),
            tuple(log.reasons) or tuple(b.value for b in ctx.blocks),
            transitions=tuple(log.transitions),
        )

    def _candidate(
        self, ctx: SlotContext, x: VmrSetup, entry: float, log: SlotLog
    ) -> CandidateSpec | None:
        p, f, row = self.p, x.f, ctx.row
        inv = x.s_high + p["stop_buffer"] * x.a
        r_u = inv - entry
        vwap = f * float(row["session_vwap"])
        if entry <= vwap:
            self._end(log, f, "NO_ROOM")
            return None
        target = vwap + (1 - p["tgt_frac"]) * (entry - vwap)
        if r_u <= 0 or entry - target < p["rr_min"] * r_u:
            self._end(log, f, "RR_TOO_LOW")
            return None
        self.count[f] += 1
        self.setups[f] = None
        return CandidateSpec(
            underlying_direction=trade_direction(f),
            entry_reference=f * entry,
            trigger_level=f * x.s_low,
            invalidation_level=f * inv,
            target={
                "type": "fixed_level",
                "price": f * target,
                "anchor": "tgt_frac toward signal-time VWAP",
                "vwap_sig": f * vwap,
            },
            trail={"type": "none"},
            timers={
                "ft_bars": p["ft_bars"],
                "ft_r": p["ft_r"],
                "stag_bars": None,
                "max_hold": p["max_hold"],
            },
            features={
                "a_setup": x.a,
                "S": f * x.s_high,
                "S_low": f * x.s_low,
                "ep_start": x.ep_start,
                "regime": row["regime"],
            },
            reason_codes=("VMR_REVERSAL",),
            setup_id=x.setup_id,
            setup_ordinal=x.ordinal,
        )

    def _prev_d(self, ctx: SlotContext, f: int) -> float | None:
        h = ctx.history
        for j in range(len(h) - 2, -1, -1):
            d = self._d(h.iloc[j], f)
            if d is not None:
                return d
        return None
