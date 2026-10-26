"""Shadow engine: finalized live bars -> shared features -> the five workers (FR-03).

The same feature engine and the same harness step as replay, one finalized slot at a time. The
features of slots 1..m are recomputed from the bars so far at each slot, because the engine is
causal (a truncated input gives identical values: the Step 2 truncation gate), so the newest row
is exactly what a full-session run would produce for that slot. One snapshot per slot is shared
by all workers. No orders exist in shadow mode: candidates open reference positions only.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

import numpy as np
import pandas as pd

from tournament.features.engine import FeatureConfig, PriorContext, compute_session_features
from tournament.features.pipeline import SessionOutput
from tournament.live.session import LiveSession
from tournament.marketdata.bars import BAR_SCHEMA
from tournament.marketdata.eligibility import Usage
from tournament.replay.harness import ReplayRun, RunResult
from tournament.strategies.base import StrategyWorker

log = logging.getLogger(__name__)
BAR_COLUMNS = BAR_SCHEMA.names


class ShadowEngine:
    def __init__(
        self,
        session: LiveSession,
        workers: list[StrategyWorker],
        prior: PriorContext,
        rvol_baseline: np.ndarray | None,
        *,
        usage: Usage = Usage.LIVE,
        features_cfg: FeatureConfig | None = None,
        run_id: str | None = None,
        dataset_id: str = "live",
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.session, self.prior, self.baseline = session, prior, rvol_baseline
        self.fcfg = features_cfg or FeatureConfig.load()
        self.harness = ReplayRun(
            workers, usage, features_cfg=self.fcfg, run_id=run_id, dataset_id=dataset_id
        )
        self.session_id = f"XNYS-{session.day.session_date}"
        self.run = self.harness.begin_session(session.day, self.session_id)
        self.clock = clock
        self.latencies: list[float] = []  # seconds from finalization to decisions (NFR-02)
        self.error: str | None = None
        self.stepped = 0
        session.on_bar = self.on_bar

    def on_bar(self, row: dict) -> None:
        """Called by the session for every finalized slot, in order. A failure here is recorded
        and stops decisions for the rest of the session, but never the feed recording."""
        if self.error is not None:
            return
        t0 = self.clock()
        try:
            self._step(row)
        except Exception as exc:  # noqa: BLE001 - recorded; the feed must keep recording
            self.error = f"slot {row['slot']}: {type(exc).__name__}: {exc}"
            log.exception("shadow engine stopped: %s", self.error)
            return
        self.latencies.append(self.clock() - t0)

    def _step(self, row: dict) -> None:
        s = self.session
        bars = pd.DataFrame(s.bars)[BAR_COLUMNS]
        pm = s.premarket_levels
        features, levels = compute_session_features(
            bars,
            self.session_id,
            self.prior,
            pm.high if pm else None,
            pm.low if pm else None,
            self.baseline,
            self.fcfg,
        )
        out = SessionOutput(
            s.day,
            self.session_id,
            bars,
            features,
            levels,
            pm.flags if pm else ("no_premarket_data",),
            self.prior,
            self.baseline,
            pm.high if pm else None,
            pm.low if pm else None,
        )
        self.harness.step(
            self.run, out, features, len(features) - 1, finalized_at=row["finalized_at"]
        )
        self.stepped += 1

    def result(self) -> RunResult:
        return self.harness._result()
