"""BRT: break-and-retest continuation (specs/strategies/brt.md v0.4).

Oriented per direction: s=+1 tests a zone's upper edge (zone_high) from below; s=-1 tests the
lower edge (zone_low) from above. One setup per zone edge and direction at a time; a setup binds
to the zone version current at its break. Edge identity is (member levels, side), so the same
level keeps its quota across zone versions.
"""

from __future__ import annotations

from dataclasses import dataclass

from tournament.features.engine import Zone
from tournament.strategies.base import CandidateSpec, Decision, Outcome, SlotContext
from tournament.strategies.common import SlotLog, WorkerBase, direction, obar


@dataclass
class BrtSetup:
    s: int
    edge_key: tuple
    edge: float  # oriented
    zones: list[Zone]  # the bound zone version, for obstacles
    zone_version: str
    a: float
    break_slot: int
    setup_id: str
    ordinal: int
    state: str = "BROKEN"
    r0: int | None = None
    rl: float | None = None  # oriented retest low
    rl_bar: int | None = None
    rh: float | None = None  # oriented high of the RL bar


def oriented_edges(z: Zone, s: int) -> tuple[float, float]:
    """(oriented upper edge, oriented lower edge) of a zone."""
    return (z.high, z.low) if s > 0 else (-z.low, -z.high)


class BRT(WorkerBase):
    strategy_id = "brt"

    def reset(self) -> None:
        self.setups: list[BrtSetup] = []
        self.used_edges: set[tuple] = set()

    def _state(self) -> str:
        if not self.setups:
            return "IDLE"
        return ";".join(f"{direction(x.s)}@{x.edge_key[0]}:{x.state}" for x in self.setups)

    def _end(self, log: SlotLog, x: BrtSetup, reason: str, outcome=Outcome.INVALIDATED) -> None:
        log.move(
            x.state,
            "IDLE",
            outcome,
            (reason,),
            x.setup_id,
            {"edge": x.s * x.edge, "rl": None if x.rl is None else x.s * x.rl},
        )
        self.setups.remove(x)

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

        triggers: list[tuple[BrtSetup, float]] = []
        for x in list(self.setups):
            b = obar(row, x.s)
            a = x.a
            if x.state == "BROKEN" and m > x.break_slot:
                if b.priced:
                    if b.close < x.edge - p["retest_depth"] * a:
                        self._end(log, x, "RETEST_FAILED")
                        continue
                    if b.low < x.edge - p["retest_wick_max"] * a:
                        self._end(log, x, "RETEST_TOO_DEEP")
                        continue
                    if b.high >= x.edge + p["chase_max"] * a:  # wins over a same-slot retest
                        self._end(log, x, "NO_RETEST")
                        continue
                    if b.low <= x.edge + p["retest_tol"] * a:
                        x.state, x.r0, x.rl, x.rl_bar, x.rh = "RETESTING", m, b.low, m, b.high
                        log.move(
                            "BROKEN",
                            "RETESTING",
                            Outcome.WATCHING,
                            ("RETEST",),
                            x.setup_id,
                            {"r0": m, "rl": x.s * b.low},
                        )
                        continue
                if m >= x.break_slot + p["retest_window"]:
                    self._end(log, x, "NO_RETEST")
            elif x.state == "RETESTING":
                if b.priced:
                    if b.low < x.rl:  # equal lows keep the earlier bar
                        x.rl, x.rl_bar, x.rh = b.low, m, b.high
                    if b.close < x.edge - p["retest_depth"] * a:
                        self._end(log, x, "RETEST_FAILED")
                        continue
                    if b.low < x.edge - p["retest_wick_max"] * a:
                        self._end(log, x, "RETEST_TOO_DEEP")
                        continue
                    if m > x.rl_bar and b.close > x.rh and ctx.may_emit:
                        triggers.append((x, b.close))
                        continue
                if m >= x.r0 + p["hold_window"]:
                    self._end(log, x, "HOLD_TIMEOUT")

        candidate = None
        triggers.sort(key=lambda t: (t[0].break_slot, -t[0].s))  # earliest setup, bullish first
        for x, entry in triggers:
            if candidate is not None:
                self._end(log, x, "TRIGGER_PREEMPTED")
                continue
            inv = x.rl - p["stop_buffer"] * x.a
            r_u = entry - inv
            lowers = sorted(lo for z in x.zones for _, lo in [oriented_edges(z, x.s)] if lo > entry)
            if lowers and lowers[0] - entry <= p["next_level_max"] * x.a:
                target, source = lowers[0], "next_level"
            else:
                target, source = entry + p["fallback_r"] * r_u, "fallback_r"
            if r_u <= 0 or target - entry < p["rr_min"] * r_u:
                self._end(log, x, "RR_TOO_LOW")
                continue
            candidate = CandidateSpec(
                underlying_direction=direction(x.s),
                entry_reference=x.s * entry,
                trigger_level=x.s * x.rh,
                invalidation_level=x.s * inv,
                target={
                    "type": "fixed_level" if source == "next_level" else "r_multiple",
                    "price": x.s * target,
                    "target_source": source,
                    "r": None if source == "next_level" else p["fallback_r"],
                    "zone_version": x.zone_version,
                },
                trail={
                    "type": "swing_trail",
                    "activate_r": p["trail_activate_r"],
                    "buffer": p["stop_buffer"] * x.a,
                },
                timers={
                    "ft_bars": p["ft_bars"],
                    "ft_r": p["ft_r"],
                    "stag_bars": None,
                    "max_hold": p["max_hold"],
                },
                features={
                    "edge": x.s * x.edge,
                    "edge_key": list(x.edge_key),
                    "a_setup": x.a,
                    "rl": x.s * x.rl,
                    "rh": x.s * x.rh,
                    "break_slot": x.break_slot,
                    "r0": x.r0,
                },
                reason_codes=(f"BRT_{direction(x.s).upper()}_HOLD",),
                setup_id=x.setup_id,
                setup_ordinal=x.ordinal,
            )
            self.used_edges.add((x.edge_key, x.s))
            self.setups.remove(x)
        if candidate is not None:
            for x in list(self.setups):  # one position at a time: the rest can never emit
                self._end(log, x, "POSITION_OPENED")
            return Decision(
                Outcome.CANDIDATE,
                self._state(),
                tuple(log.reasons),
                candidate.setup_id,
                tuple(log.transitions),
                candidate,
            )

        if not ctx.blocks and obar(row, 1).priced and row["zone_version"] in ctx.levels.zones:
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

    def _start(self, ctx: SlotContext, log: SlotLog) -> None:
        p, m, row = self.p, ctx.slot, ctx.row
        version = row["zone_version"]
        zones = ctx.levels.zones[version]
        a = float(row["a_cur"])
        rvol = row["rvol"]
        if rvol is None or not rvol >= p["rvol_min"]:
            return
        for z in zones:
            for s in (1, -1):
                key = (tuple(sorted(z.members)), "upper" if s > 0 else "lower")
                if (key, s) in self.used_edges or any(
                    x.edge_key == key and x.s == s for x in self.setups
                ):
                    continue
                edge, _ = oriented_edges(z, s)
                b = obar(row, s)
                thr = edge + p["brk_min"] * a
                prev = self._prev_close(ctx, s)
                if b.close > thr and prev is not None and prev <= thr:
                    sid, ordinal = self.new_setup()
                    self.setups.append(BrtSetup(s, key, edge, zones, version, a, m, sid, ordinal))
                    log.move(
                        "IDLE",
                        "BROKEN",
                        Outcome.WATCHING,
                        ("BREAK",),
                        sid,
                        {
                            "edge": s * edge,
                            "a_setup": a,
                            "zone_version": version,
                            "members": list(z.members),
                        },
                    )

    @staticmethod
    def _prev_close(ctx: SlotContext, s: int) -> float | None:
        h = ctx.history
        for j in range(len(h) - 2, -1, -1):
            b = obar(h.iloc[j], s)
            if b.priced:
                return b.close
        return None
