"""VPC: VWAP pullback continuation (specs/strategies/vpc.md v0.4, incl. D-38).

Oriented by s: s=+1 needs TREND_UP (H = swing high, L = swing low); s=-1 needs TREND_DOWN and is
the exact mirror (H = swing low, L = swing high, in oriented prices).

H retirement (D-38): SUPPORT_UNDERCUT keeps H usable for a replacement setup; every other
setup end, and every reference exit, retires H. A new H must be strictly above every retired H.
"""

from __future__ import annotations

from dataclasses import dataclass

from tournament.strategies.base import (
    CandidateSpec,
    Decision,
    Outcome,
    ReferenceExitEvent,
    SlotContext,
)
from tournament.strategies.common import SlotLog, WorkerBase, at, direction, obar, oriented_swings

REGIME = {1: "TREND_UP", -1: "TREND_DOWN"}


@dataclass
class VpcSetup:
    s: int
    h: float  # oriented H
    p_h: int
    a: float
    setup_id: str
    ordinal: int
    state: str = "EXTENDED"
    first_pullback: int | None = None
    low: float | None = None  # oriented L
    q: int | None = None
    c: int | None = None
    p: float | None = None  # oriented P


@dataclass
class Replacement:
    h: float
    p_h: int
    undercut_slot: int
    a_old: float


class VPC(WorkerBase):
    strategy_id = "vpc"

    def reset(self) -> None:
        self.setup: dict[int, VpcSetup | None] = {1: None, -1: None}
        self.context: dict[int, Replacement | None] = {1: None, -1: None}
        self.retired: dict[int, list[float]] = {1: [], -1: []}
        self.uppers: dict[int, list[dict]] = {1: [], -1: []}  # oriented upper swings seen so far
        self.count = {1: 0, -1: 0}
        self.open_h: dict[
            str, tuple[int, float]
        ] = {}  # setup_id -> (s, H) for post-trade retirement

    def on_reference_exit(self, event: ReferenceExitEvent) -> None:
        s, h = self.open_h.pop(event.setup_id, (None, None))
        if s is not None:
            self.retired[s].append(h)  # post-trade: retire

    # ---- helpers ---------------------------------------------------------------------------
    def _state(self) -> str:
        parts = []
        for s in (1, -1):
            x, ctxr = self.setup[s], self.context[s]
            parts.append(
                f"{direction(s)}:{x.state if x else ('AWAITING_REPLACEMENT_L' if ctxr else 'IDLE')}"
            )
        return "|".join(parts)

    def _end(self, log: SlotLog, s: int, reason: str, retire: bool = True) -> None:
        x = self.setup[s]
        log.move(
            x.state,
            "IDLE",
            Outcome.INVALIDATED,
            (reason,),
            x.setup_id,
            {"H": s * x.h, "L": None if x.low is None else s * x.low, "retire_h": retire},
        )
        if retire:
            self.retired[s].append(x.h)
        self.setup[s] = None

    def _drop_context(self, log: SlotLog, s: int, reason: str, retire: bool) -> None:
        r = self.context[s]
        if retire:
            self.retired[s].append(r.h)
        log.reasons.append(f"REPLACEMENT_DROPPED_{reason}")
        self.context[s] = None

    def _too_deep(self, b, a: float) -> bool:
        return b.priced and b.vwap is not None and b.close < b.vwap - self.p["pb_max_depth"] * a

    def _qualifying_h(self, ctx: SlotContext, s: int) -> dict | None:
        """Most recent oriented upper swing confirmed by now, recent enough, extended enough,
        and strictly above every retired H."""
        p, m = self.p, ctx.slot
        for sw in reversed(self.uppers[s]):
            if sw["pivot_slot"] < m - p["ext_lookback"]:
                return None  # older ones are older still
            pb = obar(at(ctx.history, sw["pivot_slot"]), s)
            if pb.vwap is None or pb.a is None:
                continue
            if sw["price"] - pb.vwap >= p["ext_min"] * pb.a and all(
                sw["price"] > r for r in self.retired[s]
            ):
                return sw  # most recent swing meeting all three conditions
        return None

    # ---- main ----------------------------------------------------------------------------
    def evaluate(self, ctx: SlotContext) -> Decision:
        log, p, m, row = SlotLog(), self.p, ctx.slot, ctx.row
        for s in (1, -1):
            self.uppers[s].extend(oriented_swings(row, s, upper=True))
        miss = self.missing(ctx)
        window_closed = m > self.last_entry(ctx)
        if ctx.cancel_reason or miss or window_closed:
            reason = (
                f"DATA_{ctx.cancel_reason}"
                if ctx.cancel_reason
                else "NOT_READY"
                if miss
                else "WINDOW_CLOSED"
            )
            for s in (1, -1):
                if self.setup[s]:
                    self._end(log, s, reason)
                if self.context[s]:
                    self._drop_context(log, s, reason, retire=True)
            outcome = (
                Outcome.NOT_READY
                if miss
                else (Outcome.INVALIDATED if log.invalidated else Outcome.INELIGIBLE)
            )
            return Decision(
                outcome,
                self._state(),
                tuple(log.reasons) or (reason,),
                transitions=tuple(log.transitions),
            )

        candidate = None
        for s in (1, -1):
            regime_ok = row["regime"] == REGIME[s]
            if self.setup[s] and not regime_ok:
                self._end(log, s, "REGIME_LOST")
            if self.context[s] and not regime_ok:
                self._drop_context(log, s, "REGIME_LOST", retire=True)
            if self.setup[s]:
                c = self._advance(ctx, s, log)
                if c is not None and candidate is None:
                    candidate = c
            elif self.context[s]:
                self._replacement(ctx, s, log)
            if (
                self.setup[s] is None
                and self.context[s] is None
                and regime_ok
                and not ctx.blocks
                and m >= p["first_slot"]
                and obar(row, s).priced
                and self.count[s] < p["max_cand_dir"]
            ):
                self._start(ctx, s, log)

        if candidate is not None:
            for s in (1, -1):
                if self.setup[s]:
                    self._end(log, s, "POSITION_OPENED")
                if self.context[s]:
                    self._drop_context(log, s, "POSITION_OPENED", retire=True)
            return Decision(
                Outcome.CANDIDATE,
                self._state(),
                tuple(log.reasons),
                candidate.setup_id,
                tuple(log.transitions),
                candidate,
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

    def _start(self, ctx: SlotContext, s: int, log: SlotLog) -> None:
        sw = self._qualifying_h(ctx, s)
        if sw is None:
            return
        a = float(ctx.row["a_cur"])
        sid, ordinal = self.new_setup()
        x = VpcSetup(s, sw["price"], sw["pivot_slot"], a, sid, ordinal)
        self.setup[s] = x
        log.move(
            "IDLE",
            "EXTENDED",
            Outcome.WATCHING,
            ("H_SELECTED",),
            sid,
            {"H": s * x.h, "p_H": x.p_h, "a_setup": a},
        )
        # pullback window reconstructed over p_H+1 .. m (vpc.md)
        for j in range(x.p_h + 1, ctx.slot + 1):
            b = obar(at(ctx.history, j), s)
            if b.priced and b.vwap is not None and b.low <= b.vwap + self.p["pb_zone"] * a:
                x.state, x.first_pullback = "PULLBACK", j
                log.move(
                    "EXTENDED",
                    "PULLBACK",
                    Outcome.WATCHING,
                    ("PULLBACK_RECONSTRUCTED",),
                    sid,
                    {"first_pullback": j},
                )
                break

    def _advance(self, ctx: SlotContext, s: int, log: SlotLog) -> CandidateSpec | None:
        p, m, x = self.p, ctx.slot, self.setup[s]
        b = obar(ctx.row, s)
        if (
            x.state == "SUPPORTED" and b.priced and m > x.c and b.low < x.low
        ):  # undercut, regardless of close
            self.context[s] = Replacement(x.h, x.p_h, m, x.a)
            self._end(log, s, "SUPPORT_UNDERCUT", retire=False)
            return None
        if self._too_deep(b, x.a):
            self._end(log, s, "PULLBACK_TOO_DEEP")
            return None
        if m > x.p_h + p["setup_max_age"]:
            self._end(log, s, "SETUP_AGE")
            return None
        if x.state == "EXTENDED":
            for sw in oriented_swings(ctx.row, s, upper=True):  # re-anchor to a higher H
                if sw["pivot_slot"] > x.p_h and sw["price"] > x.h:
                    log.move(
                        "EXTENDED",
                        "EXTENDED",
                        Outcome.WATCHING,
                        ("H_REANCHORED",),
                        x.setup_id,
                        {"old_H": s * x.h, "H": s * sw["price"], "p_H": sw["pivot_slot"]},
                    )
                    x.h, x.p_h = sw["price"], sw["pivot_slot"]
            if b.priced and b.vwap is not None and b.low <= b.vwap + p["pb_zone"] * x.a:
                x.state, x.first_pullback = "PULLBACK", m
                log.move(
                    "EXTENDED",
                    "PULLBACK",
                    Outcome.WATCHING,
                    ("PULLBACK",),
                    x.setup_id,
                    {"first_pullback": m},
                )
        elif x.state == "PULLBACK":
            for sw in oriented_swings(ctx.row, s, upper=False):
                if sw["pivot_slot"] > x.p_h and self._support_ok(ctx, s, sw, x.a):
                    self._support(ctx, s, x, sw, log, "PULLBACK")
                    break
        elif x.state == "SUPPORTED" and m > x.c:
            rvol = ctx.row["rvol"]
            rvol_ok = rvol is not None and rvol == rvol and rvol >= p["rvol_min"]
            if b.priced and b.close > x.p + p["trig_buffer"] * x.a and rvol_ok and ctx.may_emit:
                return self._candidate(ctx, s, x, b.close, log)
            if m >= x.c + p["trig_window"]:
                self._end(log, s, "TRIGGER_TIMEOUT")
        return None

    def _support_ok(self, ctx: SlotContext, s: int, sw: dict, a: float) -> bool:
        qb = obar(at(ctx.history, sw["pivot_slot"]), s)
        return qb.vwap is not None and sw["price"] >= qb.vwap - self.p["pb_max_depth"] * a

    def _support(self, ctx, s, x: VpcSetup, sw: dict, log: SlotLog, frm: str) -> None:
        x.low, x.q, x.c = sw["price"], sw["pivot_slot"], ctx.slot
        x.p = max(
            obar(at(ctx.history, j), s).high
            for j in range(x.q, x.c + 1)
            if obar(at(ctx.history, j), s).priced
        )
        x.state = "SUPPORTED"
        log.move(
            frm,
            "SUPPORTED",
            Outcome.WATCHING,
            ("SUPPORT_CONFIRMED",),
            x.setup_id,
            {"L": s * x.low, "q": x.q, "c": x.c, "P": s * x.p},
        )

    def _replacement(self, ctx: SlotContext, s: int, log: SlotLog) -> None:
        """IDLE with a replacement context (vpc.md, D-38), in the declared order."""
        p, m, r = self.p, ctx.slot, self.context[s]
        b = obar(ctx.row, s)
        if self._too_deep(b, r.a_old) or m > r.p_h + p["setup_max_age"]:
            self._drop_context(log, s, "STRUCTURAL", retire=True)
            return
        newer = self._qualifying_h(ctx, s)
        if newer is not None and newer["pivot_slot"] > r.p_h:
            self._drop_context(log, s, "NEWER_H", retire=False)
            return  # a normal setup on the newer H starts in evaluate()
        a_new = float(ctx.row["a_cur"])  # the replacement's own A_setup
        for sw in oriented_swings(ctx.row, s, upper=False):
            if sw["pivot_slot"] >= r.undercut_slot and self._support_ok(ctx, s, sw, a_new):
                sid, ordinal = self.new_setup()
                x = VpcSetup(s, r.h, r.p_h, a_new, sid, ordinal, state="REPLACEMENT")
                self.setup[s], self.context[s] = x, None
                self._support(ctx, s, x, sw, log, "AWAITING_REPLACEMENT_L")
                return

    def _candidate(
        self, ctx: SlotContext, s: int, x: VpcSetup, entry: float, log: SlotLog
    ) -> CandidateSpec | None:
        p = self.p
        inv = x.low - p["stop_buffer"] * x.a
        r_u = entry - inv
        if r_u <= 0 or x.h - entry < p["rr_min"] * r_u:
            self._end(log, s, "RR_TOO_LOW")
            return None
        self.count[s] += 1
        self.setup[s] = None
        self.open_h[x.setup_id] = (s, x.h)
        return CandidateSpec(
            underlying_direction=direction(s),
            entry_reference=s * entry,
            trigger_level=s * (x.p + p["trig_buffer"] * x.a),
            invalidation_level=s * inv,
            target={"type": "fixed_level", "price": s * x.h, "anchor": "H"},
            trail={
                "type": "swing_trail",
                "activate_r": p["trail_activate_r"],
                "buffer": p["stop_buffer"] * x.a,
            },
            timers={
                "ft_bars": p["ft_bars"],
                "ft_r": p["ft_r"],
                "stag_bars": p["stag_bars"],
                "max_hold": p["max_hold"],
            },
            features={
                "H": s * x.h,
                "p_H": x.p_h,
                "L": s * x.low,
                "q": x.q,
                "c": x.c,
                "P": s * x.p,
                "a_setup": x.a,
                "regime": ctx.row["regime"],
                "rvol": ctx.row["rvol"],
            },
            reason_codes=(f"VPC_{direction(s).upper()}_BREAK_PULLBACK_HIGH",),
            setup_id=x.setup_id,
            setup_ordinal=x.ordinal,
        )
