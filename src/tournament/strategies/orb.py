"""ORB: opening-range breakout (specs/strategies/orb.md v0.4).

Per direction (oriented: s=+1 breaks ORH, s=-1 breaks ORL). The range is frozen at slot 15 with
W_A = W / A_OR; the day is INELIGIBLE if the OR is incomplete or W_A is outside [w_min, w_max].
A candidate needs a CROSSING close beyond ORH + brk_buffer*A_OR on slots 16..last entry, confirmed
on the same bar (rvol, nonzero range, close in the upper half). One candidate per direction.
"""

from __future__ import annotations

from tournament.strategies.base import CandidateSpec, Decision, Outcome, SlotContext
from tournament.strategies.common import SlotLog, WorkerBase, at, direction, obar

OR_SLOTS = 15


class ORB(WorkerBase):
    strategy_id = "orb"

    def reset(self) -> None:
        self.day = "WAITING_OR"  # WAITING_OR | ARMED | INELIGIBLE_DAY
        self.day_reason: str | None = None
        self.a_or = self.w = self.w_a = None
        self.orh = self.orl = None
        self.done = {1: False, -1: False}

    def _freeze_range(self, ctx: SlotContext, log: SlotLog) -> None:
        lv, p = ctx.levels, self.p
        r15 = at(ctx.history, OR_SLOTS)
        if not lv.or_complete:
            self.day, self.day_reason = "INELIGIBLE_DAY", "OR_INCOMPLETE"
        elif r15["atr_source"] != "CURRENT_SESSION":
            self.day, self.day_reason = "INELIGIBLE_DAY", "A_OR_NOT_READY"
        else:
            self.orh, self.orl, self.a_or = lv.orh, lv.orl, float(r15["a_cur"])
            self.w = self.orh - self.orl
            self.w_a = self.w / self.a_or
            if self.w_a < p["w_min"]:
                self.day, self.day_reason = "INELIGIBLE_DAY", "OR_TOO_NARROW"
            elif self.w_a > p["w_max"]:
                self.day, self.day_reason = "INELIGIBLE_DAY", "OR_TOO_WIDE"
            else:
                self.day = "ARMED"
        log.move(
            "WAITING_OR",
            self.day,
            Outcome.WATCHING if self.day == "ARMED" else Outcome.INELIGIBLE,
            (self.day_reason or "OR_FROZEN",),
            None,
            {"orh": self.orh, "orl": self.orl, "a_or": self.a_or, "w_a": self.w_a},
        )

    def _state(self) -> str:
        sides = {s: "DONE" if self.done[s] else "ARMED" for s in (1, -1)}
        return f"{self.day}|bull:{sides[1]}|bear:{sides[-1]}"

    def evaluate(self, ctx: SlotContext) -> Decision:
        log, p, m = SlotLog(), self.p, ctx.slot
        if m >= OR_SLOTS and self.day == "WAITING_OR":
            self._freeze_range(ctx, log)
        if self.missing(ctx):
            return Decision(
                Outcome.NOT_READY,
                self._state(),
                tuple(self.missing(ctx)),
                transitions=tuple(log.transitions),
            )
        if self.day != "ARMED":
            reason = self.day_reason or "BEFORE_RANGE"
            return Decision(
                Outcome.INELIGIBLE, self._state(), (reason,), transitions=tuple(log.transitions)
            )
        if m > self.last_entry(ctx) or m <= OR_SLOTS:
            return Decision(
                Outcome.INELIGIBLE,
                self._state(),
                ("OUTSIDE_WINDOW",),
                transitions=tuple(log.transitions),
            )

        row = ctx.row
        candidate = None
        for s in (1, -1):
            if self.done[s]:
                continue
            b = obar(row, s)
            if not b.priced:
                continue
            edge = self.orh if s > 0 else -self.orl
            thr = edge + p["brk_buffer"] * self.a_or
            prev = self._prev_close(ctx, s)
            if not (b.close > thr and prev is not None and prev <= thr):
                continue
            rvol = row["rvol"]
            confirmed = (
                rvol is not None
                and rvol >= p["rvol_min"]
                and b.high > b.low
                and b.close >= (b.high + b.low) / 2
            )
            if not confirmed:
                log.reasons.append(f"{direction(s).upper()}_CROSSING_UNCONFIRMED")
                continue
            if not ctx.may_emit:
                log.reasons.append("TRIGGER_BLOCKED")
                continue
            entry, inv, target = (
                b.close,
                edge - p["inv_frac"] * self.w,
                edge + p["tgt_mult"] * self.w,
            )
            r_u = entry - inv
            sid, ordinal = self.new_setup()
            if target <= entry:
                log.move(
                    "ARMED",
                    "ARMED",
                    Outcome.INVALIDATED,
                    ("TARGET_EXHAUSTED",),
                    sid,
                    {"entry": s * entry, "target": s * target},
                )
                continue
            if (target - entry) / r_u < p["rr_min"]:
                log.move(
                    "ARMED",
                    "ARMED",
                    Outcome.INVALIDATED,
                    ("RR_TOO_LOW",),
                    sid,
                    {"rr": (target - entry) / r_u},
                )
                continue
            a_setup = float(row["a_cur"])
            candidate = CandidateSpec(
                underlying_direction=direction(s),
                entry_reference=s * entry,
                trigger_level=s * thr,
                invalidation_level=s * inv,
                target={
                    "type": "fixed_level",
                    "price": s * target,
                    "anchor": "ORH + tgt_mult*W" if s > 0 else "ORL - tgt_mult*W",
                },
                trail={
                    "type": "swing_trail",
                    "activate_r": p["trail_activate_r"],
                    "buffer": p["stop_buffer"] * a_setup,
                },
                timers={
                    "ft_bars": p["ft_bars"],
                    "ft_r": p["ft_r"],
                    "stag_bars": None,
                    "max_hold": p["max_hold"],
                },
                features={
                    "orh": self.orh,
                    "orl": self.orl,
                    "a_or": self.a_or,
                    "w_a": self.w_a,
                    "rvol": rvol,
                    "a_setup": a_setup,
                },
                reason_codes=(f"ORB_{direction(s).upper()}_BREAK", "RVOL_OK"),
                setup_id=sid,
                setup_ordinal=ordinal,
            )
            self.done[s] = True  # quota consumed at emission
            break
        if candidate is not None:
            return Decision(
                Outcome.CANDIDATE,
                self._state(),
                tuple(log.reasons),
                candidate.setup_id,
                tuple(log.transitions),
                candidate,
            )
        outcome = Outcome.INVALIDATED if log.invalidated else Outcome.WATCHING
        return Decision(
            outcome, self._state(), tuple(log.reasons), transitions=tuple(log.transitions)
        )

    @staticmethod
    def _prev_close(ctx: SlotContext, s: int) -> float | None:
        """close(m - 1), or the most recent valid close before m if slot m - 1 had no price."""
        h = ctx.history
        for j in range(len(h) - 2, -1, -1):
            b = obar(h.iloc[j], s)
            if b.priced:
                return b.close
        return None
