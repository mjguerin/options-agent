"""FBR: failed-breakout reversal (specs/strategies/fbr.md v0.4).

Oriented by f: f=+1 is a failed UPSIDE excursion through zone_high (a bearish trade); f=-1 is a
failed downside excursion through zone_low (a bullish trade). In oriented prices both look like
the f=+1 case: excursion up through E, return below E, trigger below the return bar's low.
"""

from __future__ import annotations

from dataclasses import dataclass

from tournament.features.engine import LOWER_LEVELS, PAIRS, UPPER_LEVELS, Zone
from tournament.strategies.base import CandidateSpec, Decision, Outcome, SlotContext
from tournament.strategies.common import BEARISH, BULLISH, SlotLog, WorkerBase, obar


def trade_direction(f: int) -> str:
    return BEARISH if f > 0 else BULLISH


@dataclass
class FbrSetup:
    f: int
    edge_key: tuple
    edge: float  # oriented E
    zone: Zone
    zone_version: str
    a: float
    start: int
    x: float  # oriented excursion extreme
    setup_id: str
    ordinal: int
    state: str = "EXCURSION"
    ret_bar: int | None = None
    ret_low: float | None = None


class FBR(WorkerBase):
    strategy_id = "fbr"

    def reset(self) -> None:
        self.setups: list[FbrSetup] = []
        self.used_edges: set[tuple] = set()
        self.emitted = 0

    def _state(self) -> str:
        return (
            ";".join(f"{trade_direction(x.f)}@{x.edge_key[0]}:{x.state}" for x in self.setups)
            or "IDLE"
        )

    def _end(self, log: SlotLog, x: FbrSetup, reason: str) -> None:
        log.move(
            x.state,
            "IDLE",
            Outcome.INVALIDATED,
            (reason,),
            x.setup_id,
            {"edge": x.f * x.edge, "x": x.f * x.x},
        )
        self.setups.remove(x)

    def _blocked_by_trend(self, row, f: int) -> bool:
        d = row["disp_30_atr"]
        return d is not None and f * d >= self.p["trend_block"]

    def evaluate(self, ctx: SlotContext) -> Decision:
        log, p, m, row = SlotLog(), self.p, ctx.slot, ctx.row
        if ctx.cancel_reason or self.missing(ctx) or m > self.last_entry(ctx):
            reason = (
                f"DATA_{ctx.cancel_reason}"
                if ctx.cancel_reason
                else "NOT_READY"
                if self.missing(ctx)
                else "WINDOW_CLOSED"
            )
            for x in list(self.setups):
                self._end(log, x, reason)
            outcome = (
                Outcome.NOT_READY
                if self.missing(ctx)
                else (Outcome.INVALIDATED if log.invalidated else Outcome.INELIGIBLE)
            )
            return Decision(
                outcome,
                self._state(),
                tuple(log.reasons) or (reason,),
                transitions=tuple(log.transitions),
            )

        triggers = []
        for x in list(self.setups):
            if self._blocked_by_trend(row, x.f):
                self._end(log, x, "REGIME_STRONG_TREND")
                continue
            b = obar(row, x.f)
            if x.state == "EXCURSION":
                if self._excursion_step(x, b, m, log) == "ended":
                    continue
            elif x.state == "RETURNED" and m > x.ret_bar:
                if b.priced:
                    if b.close > x.edge:
                        self._end(log, x, "RECLAIMED")
                        continue
                    if b.close < x.ret_low and ctx.may_emit:
                        triggers.append((x, b.close))
                        continue
                if m >= x.ret_bar + p["trig_window"]:
                    self._end(log, x, "TRIGGER_TIMEOUT")

        candidate = None
        triggers.sort(key=lambda t: (t[0].start, -t[0].f))
        for x, entry in triggers:
            if candidate is not None:
                self._end(log, x, "TRIGGER_PREEMPTED")
                continue
            candidate = self._candidate(ctx, x, entry, log)
        if candidate is not None:
            for x in list(self.setups):
                self._end(log, x, "POSITION_OPENED")
            return Decision(
                Outcome.CANDIDATE,
                self._state(),
                tuple(log.reasons),
                candidate.setup_id,
                tuple(log.transitions),
                candidate,
            )

        if (
            not ctx.blocks
            and m >= p["first_slot"]
            and obar(row, 1).priced
            and row["zone_version"] in ctx.levels.zones
            and self.emitted < p["max_cand_session"]
        ):
            self._start(ctx, log)
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

    def _excursion_step(self, x: FbrSetup, b, m: int, log: SlotLog) -> str:
        """EXCURSION checks on slots start..start+return_window (the start slot included)."""
        p = self.p
        if b.priced:
            x.x = max(x.x, b.high)
            if x.x >= x.edge + p["exc_max"] * x.a:  # takes precedence over a same-bar return
                self._end(log, x, "EXCURSION_TOO_FAR")
                return "ended"
            if b.close < x.edge - p["return_buffer"] * x.a:
                x.state, x.ret_bar, x.ret_low = "RETURNED", m, b.low
                log.move(
                    "EXCURSION",
                    "RETURNED",
                    Outcome.WATCHING,
                    ("RETURN_INSIDE",),
                    x.setup_id,
                    {"ret_bar": m, "ret_low": x.f * b.low, "x_frozen": x.f * x.x},
                )
                return "returned"
        if m >= x.start + p["return_window"]:
            self._end(log, x, "NO_RETURN")
            return "ended"
        return "waiting"

    def _candidate(
        self, ctx: SlotContext, x: FbrSetup, entry: float, log: SlotLog
    ) -> CandidateSpec | None:
        p, row, f = self.p, ctx.row, x.f
        inv = x.x + p["stop_buffer"] * x.a
        r_u = inv - entry
        target = source = pair_id = None
        vwap = row["session_vwap"]
        if vwap is not None and f * vwap < entry:
            target, source = f * vwap, "vwap"
        else:
            members = UPPER_LEVELS if f > 0 else LOWER_LEVELS
            mids = []
            for name in x.zone.members:
                if name in members:
                    mid = ctx.levels.pair_midpoint(name)
                    if mid is not None and f * mid < entry:
                        mids.append((f * mid, PAIRS[name]))
            if mids:
                target, pair_id = max(mids)  # nearest below entry, oriented
                source = "range_midpoint"
        if target is None:
            target, source = entry - p["fallback_r"] * r_u, "fallback_r"
        if r_u <= 0 or entry - target < p["rr_min"] * r_u:
            self._end(log, x, "RR_TOO_LOW")
            return None
        self.used_edges.add((x.edge_key, f))
        self.emitted += 1
        self.setups.remove(x)
        return CandidateSpec(
            underlying_direction=trade_direction(f),
            entry_reference=f * entry,
            trigger_level=f * x.ret_low,
            invalidation_level=f * inv,
            target={
                "type": "r_multiple" if source == "fallback_r" else "fixed_level",
                "price": f * target,
                "target_source": source,
                "range_pair_id": pair_id,
                "zone_version": x.zone_version,
                "r": p["fallback_r"] if source == "fallback_r" else None,
            },
            trail={"type": "none"},
            timers={
                "ft_bars": p["ft_bars"],
                "ft_r": p["ft_r"],
                "stag_bars": p["stag_bars"],
                "max_hold": p["max_hold"],
            },
            features={
                "edge": f * x.edge,
                "edge_key": list(x.edge_key),
                "a_setup": x.a,
                "x": f * x.x,
                "ret_bar": x.ret_bar,
                "ret_low": f * x.ret_low,
                "disp_30_atr": row["disp_30_atr"],
            },
            reason_codes=("FBR_FAILED_BREAKOUT",),
            setup_id=x.setup_id,
            setup_ordinal=x.ordinal,
        )

    def _start(self, ctx: SlotContext, log: SlotLog) -> None:
        p, m, row = self.p, ctx.slot, ctx.row
        version = row["zone_version"]
        a = float(row["a_cur"])
        for z in ctx.levels.zones[version]:
            for f in (1, -1):
                key = (tuple(sorted(z.members)), "upper" if f > 0 else "lower")
                if (
                    (key, f) in self.used_edges
                    or self._blocked_by_trend(row, f)
                    or any(x.edge_key == key and x.f == f for x in self.setups)
                ):
                    continue
                edge = z.high if f > 0 else -z.low
                b = obar(row, f)
                prev = self._prev_close(ctx, f)
                if not (b.high >= edge + p["exc_min"] * a and prev is not None and prev <= edge):
                    continue
                if b.high >= edge + p["exc_max"] * a:
                    log.reasons.append("EXCURSION_TOO_FAR")
                    continue
                sid, ordinal = self.new_setup()
                x = FbrSetup(f, key, edge, z, version, a, m, b.high, sid, ordinal)
                self.setups.append(x)
                log.move(
                    "IDLE",
                    "EXCURSION",
                    Outcome.WATCHING,
                    ("EXCURSION",),
                    sid,
                    {"edge": f * edge, "a_setup": a, "members": list(z.members)},
                )
                self._excursion_step(x, b, m, log)  # same-slot return allowed (fbr.md)

    @staticmethod
    def _prev_close(ctx: SlotContext, s: int) -> float | None:
        h = ctx.history
        for j in range(len(h) - 2, -1, -1):
            b = obar(h.iloc[j], s)
            if b.priced:
                return b.close
        return None
