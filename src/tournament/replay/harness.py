"""Replay harness (BRD 5.3; specs/shared.md SD-01, SD-10 to SD-13; schemas.md S-1 to S-8).

Walks sessions slot by slot through the same feature pipeline the engine will use. Per slot and
per worker, in this order:
1. Reference exits for an open reference position (from the slot after entry).
2. Common rules: floor, last entry, one reference position, cooldown, data status.
3. The worker's decision. A candidate is checked against the common rules and its geometry,
   then opens a reference position.
Records: one StrategyEvaluation per worker per slot, a StrategyTransition per state change, a
Candidate and a ReferencePosition per emission, and a run manifest.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from tournament.features.engine import FeatureConfig
from tournament.features.pipeline import SessionOutput, run_sessions
from tournament.marketdata.bars import DATA_MISSING_OR_UNCERTAIN, KNOWN_NO_ELIGIBLE_TRADES
from tournament.marketdata.eligibility import CONFIG_DIR as ELIGIBILITY_DIR
from tournament.marketdata.eligibility import Usage
from tournament.marketdata.store import HistoryStore
from tournament.replay.ids import canonical_json, digest, run_scoped, signal_key
from tournament.replay.refpos import ReferencePosition, geometry_ok
from tournament.strategies.base import (
    Block,
    Decision,
    Outcome,
    ReferenceExitEvent,
    SlotContext,
    StrategyWorker,
)

COMMON_CONFIG = Path(__file__).resolve().parents[3] / "config" / "strategies" / "common-v1.yaml"
REPO = Path(__file__).resolve().parents[3]
HALTED = "HALTED"
SNAPSHOT_FIELDS = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "session_vwap",
    "vwap_uncertain",
    "interval_status",
    "a_cur",
    "atr_source",
    "disp_30_atr",
    "occ_up_30",
    "side",
    "crosses_60",
    "regime",
    "rvol",
    "zone_version",
    "swings_new",
)
PRICE_FIELDS = frozenset(
    {
        "open",
        "high",
        "low",
        "close",
        "session_vwap",
        "price",
        "entry_reference",
        "trigger_level",
        "invalidation_level",
        "reference_entry_price",
        "reference_exit_price",
    }
)


class ContractViolation(RuntimeError):
    """A worker broke the strategy contract (e.g. emitted while blocked). Always a bug."""


def _clean(v: Any) -> Any:
    if v is None or isinstance(v, (str, bool, int, list, dict)):
        return v
    try:
        return None if pd.isna(v) else v
    except (TypeError, ValueError):
        return v


def code_commit() -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(REPO), "describe", "--always", "--dirty", "--abbrev=7"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


@dataclass
class _WorkerState:
    ref: ReferencePosition | None = None
    cooldown_from: int = 0
    cooldown_to: int = -1
    state: str = "IDLE"


@dataclass
class SessionRun:
    """Per-session constants and per-worker state held between steps."""

    n: int
    floor: int
    last_entry: int
    cutoff: int
    states: dict


@dataclass
class RunResult:
    manifest: dict
    evaluations: pd.DataFrame
    transitions: pd.DataFrame
    candidates: pd.DataFrame
    reference_positions: pd.DataFrame
    snapshots: pd.DataFrame = field(default_factory=pd.DataFrame)
    sessions: list[str] = field(default_factory=list)

    def write(self, root: Path) -> Path:
        out = root / self.manifest["run_id"]
        out.mkdir(parents=True, exist_ok=True)
        for name in (
            "evaluations",
            "transitions",
            "candidates",
            "reference_positions",
            "snapshots",
        ):
            getattr(self, name).to_parquet(out / f"{name}.parquet", index=False)
        (out / "manifest.json").write_text(json.dumps(self.manifest, indent=1, default=str))
        return out


class ReplayRun:
    def __init__(
        self,
        workers: Iterable[StrategyWorker],
        usage: Usage = Usage.DEVELOPMENT,
        common_config: Path = COMMON_CONFIG,
        features_cfg: FeatureConfig | None = None,
        run_id: str | None = None,
        dataset_id: str = "unknown",
    ) -> None:
        self.workers = list(workers)
        ids = [w.strategy_id for w in self.workers]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate strategy_id")
        self.usage = usage
        self.common = yaml.safe_load(common_config.read_text())
        self.common_hash = digest(self.common)
        self.features_cfg = features_cfg or FeatureConfig.load()
        self.run_id = run_id or str(uuid.uuid4())
        self.dataset_id = dataset_id
        self._ev: list[dict] = []
        self._tr: list[dict] = []
        self._cand: list[dict] = []
        self._ref: list[dict] = []
        self._snap: list[dict] = []
        self._sessions: list[str] = []
        self._finalized_at: pd.Timestamp | None = None

    # ---- public -------------------------------------------------------------------------
    def run_store(
        self, store: HistoryStore, start: date | None = None, end: date | None = None
    ) -> RunResult:
        sessions = run_sessions(store, self.usage, start, end, features_cfg=self.features_cfg)
        return self.run_outputs(sessions)

    def run_outputs(self, sessions: Iterable[SessionOutput]) -> RunResult:
        for out in sessions:
            self._session(out)
        return self._result()

    # ---- per session --------------------------------------------------------------------
    def _session(self, out: SessionOutput) -> None:
        run = self.begin_session(out.day, out.session_id)
        f = out.features.reset_index(drop=True)
        for i in range(len(f)):
            self.step(run, out, f, i)

    def begin_session(self, day, session_id: str) -> SessionRun:
        """Start a session: reset the workers and fix the session's clock-derived slots."""
        c = self.common
        # Session length comes from the CALENDAR, never from how many rows are available: a live
        # or truncated session must see the same last-entry and cutoff slots as a full replay.
        n = int((day.close_utc - day.open_utc) / timedelta(minutes=1))
        for w in self.workers:
            w.reset_session(session_id)
        self._sessions.append(session_id)
        return SessionRun(
            n,
            c["session"]["floor_slot"],
            n - c["session"]["last_entry_min_before_close"],
            n - c["session"]["cutoff_min_before_close"],
            {w.strategy_id: _WorkerState() for w in self.workers},
        )

    def step(
        self,
        run: SessionRun,
        out: SessionOutput,
        f: pd.DataFrame,
        i: int,
        finalized_at: pd.Timestamp | None = None,
    ) -> None:
        """Process row i of `f` (the features of slots 1..i+1). `finalized_at` is the actual
        snapshot time in live runs; replay leaves it None and models it (SD-01)."""
        self._finalized_at = finalized_at
        row = f.iloc[i]
        slot = int(row["slot"])
        identity = {
            "dataset_id": self.dataset_id,
            "stream_id": "spy_sip_bars",
            "session_id": out.session_id,
            "slot": slot,
            "feature_version": self.features_cfg.version,
            "features_config_hash": self.features_cfg.config_hash,
            "values": {k: _clean(row[k]) for k in SNAPSHOT_FIELDS},
        }
        snapshot_id = digest(identity, PRICE_FIELDS)
        self._snap.append(
            {
                "snapshot_id": snapshot_id,
                "session_id": out.session_id,
                "slot": slot,
                "identity": canonical_json(identity, PRICE_FIELDS),
            }
        )
        history = f.iloc[: i + 1]
        status = row["interval_status"]
        cancel = status if status in (DATA_MISSING_OR_UNCERTAIN, HALTED) else None
        for w in self.workers:
            self._worker_slot(
                w,
                run.states[w.strategy_id],
                out,
                row,
                slot,
                run.n,
                run.floor,
                run.last_entry,
                run.cutoff,
                history,
                cancel,
                snapshot_id,
            )

    def _worker_slot(
        self, w, st, out, row, slot, n, floor, last_entry, cutoff, history, cancel, snapshot_id
    ):
        c = self.common
        transitions: list[dict] = []
        ref_open_at_start = st.ref is not None and st.ref.open
        if ref_open_at_start and slot > st.ref.entry_slot and st.ref.evaluate(slot, row, cutoff):
            ref = st.ref
            self._ref.append(self._ref_record(w, out, ref))
            st.cooldown_from, st.cooldown_to = slot + 1, slot + c["session"]["cooldown_slots"]
            event = ReferenceExitEvent(
                ref.candidate_id,
                ref.spec.setup_id,
                ref.spec.underlying_direction,
                slot,
                ref.primary_reason,
                ref.triggered_reasons,
            )
            transitions.append(
                self._transition(
                    w,
                    out,
                    slot,
                    None,
                    "reference_exit",
                    ref.reference_position_id,
                    "IN_POSITION",
                    "IDLE",
                    Outcome.WATCHING,
                    (f"REF_EXIT_{ref.primary_reason}",),
                    ref.spec.setup_id,
                    {},
                )
            )
            w.on_reference_exit(event)

        blocks = []
        if slot < floor:
            blocks.append(Block.BEFORE_FLOOR)
        if slot > last_entry:
            blocks.append(Block.AFTER_LAST_ENTRY)
        if ref_open_at_start:
            blocks.append(Block.REF_POSITION_OPEN)
        if st.cooldown_from <= slot <= st.cooldown_to:
            blocks.append(Block.COOLDOWN)
        if row["interval_status"] == KNOWN_NO_ELIGIBLE_TRADES:
            blocks.append(Block.NO_PRICE)
        missing = [x for x in w.required_features if x in (row["not_ready"] or [])]
        ctx = SlotContext(
            out.session_id,
            slot,
            n,
            floor,
            last_entry,
            cutoff,
            history,
            out.levels,
            tuple(blocks),
            cancel,
        )
        d: Decision = w.evaluate(ctx)

        candidate_id = None
        if d.candidate is not None:
            if not ctx.may_emit or missing:
                raise ContractViolation(
                    f"{w.strategy_id} emitted at slot {slot} while blocked: {blocks} {missing}"
                )
            ok, why = geometry_ok(d.candidate)
            if not ok:
                raise ContractViolation(
                    f"{w.strategy_id} emitted invalid geometry ({why}) at slot {slot}"
                )
            candidate_id = self._open(w, st, out, row, slot, d, snapshot_id)

        for t in d.transitions:
            transitions.append(
                self._transition(
                    w,
                    out,
                    slot,
                    snapshot_id,
                    "snapshot",
                    snapshot_id,
                    t.from_state,
                    t.to_state,
                    t.outcome,
                    t.reason_codes,
                    t.setup_id,
                    t.anchors,
                )
            )
        if d.candidate is not None:
            transitions.append(
                self._transition(
                    w,
                    out,
                    slot,
                    snapshot_id,
                    "snapshot",
                    snapshot_id,
                    d.setup_state,
                    "IN_POSITION",
                    Outcome.CANDIDATE,
                    d.candidate.reason_codes,
                    d.candidate.setup_id,
                    {},
                )
            )
        st.state = "IN_POSITION" if (st.ref is not None and st.ref.open) else d.setup_state
        self._tr.extend(transitions)
        self._ev.append(
            {
                "run_id": self.run_id,
                "evaluation_id": run_scoped(
                    self.run_id,
                    "evaluation",
                    strategy=w.strategy_id,
                    session=out.session_id,
                    slot=slot,
                ),
                "strategy_id": w.strategy_id,
                "strategy_version": w.version,
                "config_hash": w.config_hash,
                "account_binding": f"acct:{w.strategy_id}",
                "session_id": out.session_id,
                "slot": slot,
                "snapshot_id": snapshot_id,
                "readiness_status": "NOT_READY" if missing else "READY",
                "missing_features": missing,
                "eligibility_blocks": [b.value for b in blocks]
                + ([f"CANCEL_{cancel}"] if cancel else []),
                "setup_state": st.state,
                "outcome": d.outcome.value,
                "reason_codes": list(d.reason_codes),
                "setup_id": d.setup_id,
                "candidate_id": candidate_id,
                "transition_ids": [t["transition_id"] for t in transitions],
            }
        )

    def _open(self, w, st, out, row, slot, d, snapshot_id) -> str:
        c = self.common
        spec = d.candidate
        skey = signal_key(
            w.strategy_id,
            w.version,
            w.config_hash,
            self.dataset_id,
            out.session_id,
            spec.setup_ordinal,
            slot,
        )
        cid = run_scoped(self.run_id, "candidate", signal_key=skey)
        bar_end = pd.Timestamp(row["bar_end"])
        scheduled = bar_end + timedelta(seconds=c["clock"]["finalize_delay_s"])
        # replay: modeled availability = scheduled finalization; live: the actual snapshot time,
        # so a late snapshot is never submission-eligible (D-37, SD-01)
        available = scheduled if self._finalized_at is None else max(scheduled, self._finalized_at)
        expires = scheduled + timedelta(seconds=c["clock"]["candidate_ttl_s"])
        self._cand.append(
            {
                "run_id": self.run_id,
                "candidate_id": cid,
                "signal_key": skey,
                "setup_id": spec.setup_id,
                "strategy_id": w.strategy_id,
                "strategy_version": w.version,
                "config_hash": w.config_hash,
                "account_binding": f"acct:{w.strategy_id}",
                "session_id": out.session_id,
                "underlying_direction": spec.underlying_direction,
                "trigger_slot": slot,
                "signal_bar_end": bar_end,
                "scheduled_finalization_at": scheduled,
                "signal_available_at": available,
                "expires_at": expires,
                "submission_eligible": bool(available < expires),
                "snapshot_id": snapshot_id,
                "entry_reference": spec.entry_reference,
                "trigger_level": spec.trigger_level,
                "invalidation_level": spec.invalidation_level,
                "r_u": abs(spec.entry_reference - spec.invalidation_level),
                "target": json.dumps(spec.target, default=str),
                "trail": json.dumps(spec.trail, default=str),
                "timers": json.dumps(spec.timers, default=str),
                "features": json.dumps(spec.features, default=str),
                "reason_codes": list(spec.reason_codes),
            }
        )
        st.ref = ReferencePosition(
            run_scoped(self.run_id, "reference_position", candidate_id=cid),
            cid,
            spec,
            slot,
            bar_end,
            tuple(c["exit_precedence"]),
        )
        return cid

    def _transition(
        self,
        w,
        out,
        slot,
        snapshot_id,
        cause_type,
        cause_id,
        frm,
        to,
        outcome,
        reasons,
        setup_id,
        anchors,
    ) -> dict:
        tid = run_scoped(
            self.run_id,
            "transition",
            strategy=w.strategy_id,
            session=out.session_id,
            slot=slot,
            n=len(self._tr),
            frm=frm,
            to=to,
        )
        return {
            "run_id": self.run_id,
            "transition_id": tid,
            "strategy_id": w.strategy_id,
            "session_id": out.session_id,
            "slot": slot,
            "cause_type": cause_type,
            "cause_record_id": cause_id,
            "snapshot_id": snapshot_id,
            "from_state": frm,
            "to_state": to,
            "outcome": outcome.value,
            "reason_codes": list(reasons),
            "setup_id": setup_id,
            "anchors": json.dumps(anchors, default=str),
        }

    def _ref_record(self, w, out, ref: ReferencePosition) -> dict:
        return {
            "run_id": self.run_id,
            "reference_position_id": ref.reference_position_id,
            "candidate_id": ref.candidate_id,
            "strategy_id": w.strategy_id,
            "session_id": out.session_id,
            "underlying_direction": ref.spec.underlying_direction,
            "reference_entry_price": ref.spec.entry_reference,
            "reference_entry_time": ref.entry_time,
            "entry_slot": ref.entry_slot,
            "initial_invalidation": ref.spec.invalidation_level,
            "r_u": ref.r_u,
            "target_policy": json.dumps(ref.spec.target, default=str),
            "trail_policy": json.dumps(ref.spec.trail, default=str),
            "trail_history": json.dumps(ref.trail_history),
            "exit_slot": ref.exit_slot,
            "reference_exit_time": ref.exit_time,
            "reference_exit_price": ref.exit_price,
            "stale_reference_price": ref.exit_price_stale,
            "exit_primary_reason": ref.primary_reason,
            "exit_triggered_reasons": list(ref.triggered_reasons),
        }

    def _result(self) -> RunResult:
        manifest = {
            "run_id": self.run_id,
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "code_commit": code_commit(),
            "dataset_id": self.dataset_id,
            "usage": self.usage.value,
            "clock_model": self.common["clock"]["clock_model"],
            "common_config_version": self.common["version"],
            "common_config_hash": self.common_hash,
            "feature_version": self.features_cfg.version,
            "features_config_hash": self.features_cfg.config_hash,
            "eligibility_config_hash": digest(
                {p.name: p.read_text() for p in sorted(ELIGIBILITY_DIR.glob("*")) if p.is_file()}
            ),
            "workers": [
                {"strategy_id": w.strategy_id, "version": w.version, "config_hash": w.config_hash}
                for w in self.workers
            ],
            "sessions": {
                "count": len(self._sessions),
                "first": self._sessions[0] if self._sessions else None,
                "last": self._sessions[-1] if self._sessions else None,
            },
            "counts": {
                "evaluations": len(self._ev),
                "transitions": len(self._tr),
                "candidates": len(self._cand),
                "reference_positions": len(self._ref),
                "snapshots": len(self._snap),
            },
        }
        return RunResult(
            manifest,
            pd.DataFrame(self._ev),
            pd.DataFrame(self._tr),
            pd.DataFrame(self._cand),
            pd.DataFrame(self._ref),
            pd.DataFrame(self._snap),
            list(self._sessions),
        )


def dataset_id_for(
    store: HistoryStore, datasets: tuple[str, ...] = ("spy_trades", "spy_trades_premarket")
) -> str:
    """Identify the stored inputs by the code commits that wrote them (S-1 dataset_id)."""
    stamps = set()
    for name in datasets:
        for d in store.days(name):
            stamps.add((name, HistoryStore.provenance(store.path(name, d)).get("code_commit")))
    return "history:" + digest(sorted(stamps))[:16]
