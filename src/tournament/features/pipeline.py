"""Walk sessions in order: trades -> bars -> features, carrying prior-session context.

The same pipeline serves replay, and later shadow and paper (with a live bar source). Context
carried across sessions: final ATR (SD-05 fallback), prior-day high/low/close (SD-07), and the
per-slot volume history of full sessions for the RVOL baseline (SD-08).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from tournament.features.engine import (
    FeatureConfig,
    PriorContext,
    SessionLevels,
    compute_session_features,
    prior_context_from,
)
from tournament.marketdata.bars import (
    DATA_MISSING_OR_UNCERTAIN,
    build_session_bars,
    premarket_levels,
)
from tournament.marketdata.calendar import SessionDay
from tournament.marketdata.eligibility import EligibilityConfig, Usage
from tournament.marketdata.store import HistoryStore


@dataclass
class SessionOutput:
    day: SessionDay
    session_id: str
    bars: pd.DataFrame
    features: pd.DataFrame
    levels: SessionLevels
    premarket_flags: tuple[str, ...]
    # the exact inputs carried in from earlier sessions (audit, and the Step 2 truncation gate)
    prior: PriorContext | None = None
    rvol_baseline: np.ndarray | None = None
    premarket_high: float | None = None
    premarket_low: float | None = None


class RvolHistory:
    """Per-slot volumes of past full-length sessions; NaN where a slot was missing data."""

    def __init__(self, sessions: int, full_slots: int) -> None:
        self.sessions, self.full_slots = sessions, full_slots
        self.rows: list[np.ndarray] = []

    def baseline(self) -> np.ndarray:
        """Mean of the last `sessions` valid values per slot; NaN if fewer exist."""
        out = np.full(self.full_slots, np.nan)
        if not self.rows:
            return out
        m = np.vstack(self.rows[-4 * self.sessions :])  # ample depth for occasional gaps
        for s in range(self.full_slots):
            col = m[:, s]
            col = col[~np.isnan(col)][-self.sessions :]
            if len(col) == self.sessions:
                out[s] = col.mean()
        return out

    def add(self, bars: pd.DataFrame) -> None:
        if len(bars) != self.full_slots:
            return  # early closes never enter the baseline
        vol = bars["volume"].astype(float).to_numpy().copy()
        vol[(bars["interval_status"] == DATA_MISSING_OR_UNCERTAIN).to_numpy()] = np.nan
        self.rows.append(vol)


def calendar_days(store: HistoryStore) -> list[SessionDay]:
    cal = pq.read_table(store.root / "calendar.parquet").to_pandas()
    return [
        SessionDay(
            r.session_date if isinstance(r.session_date, date) else r.session_date.date(),
            pd.Timestamp(r.open_utc).to_pydatetime(),
            pd.Timestamp(r.close_utc).to_pydatetime(),
        )
        for r in cal.itertuples()
    ]


def carry_context(
    store: HistoryStore,
    usage: Usage,
    before: date,
    eligibility: EligibilityConfig | None = None,
    features_cfg: FeatureConfig | None = None,
) -> tuple[PriorContext, np.ndarray]:
    """What a session on `before` inherits: the prior-session context and the RVOL baseline,
    built from every stored session earlier than it. Equals the `prior` and `rvol_baseline` that
    `run_sessions` attaches to that session (tested), so live and replay start from the same
    inputs. Slow (it rebuilds every earlier session): run it before the open."""
    elig = eligibility or EligibilityConfig()
    fcfg = features_cfg or FeatureConfig.load()
    rv = RvolHistory(fcfg["rvol"]["reference_sessions"], fcfg["rvol"]["full_session_slots"])
    prior = PriorContext()
    for day in calendar_days(store):
        if day.session_date >= before:
            break
        if not store.has("spy_trades", day.session_date):
            continue
        bars = build_session_bars(
            store.read("spy_trades", day.session_date), day, elig, usage
        ).to_pandas()
        pm_high = pm_low = None
        if store.has("spy_trades_premarket", day.session_date):
            pm = premarket_levels(
                store.read("spy_trades_premarket", day.session_date), day, elig, usage
            )
            pm_high, pm_low = pm.high, pm.low
        features, _ = compute_session_features(
            bars, f"XNYS-{day.session_date}", prior, pm_high, pm_low, rv.baseline(), fcfg
        )
        prior = prior_context_from(bars, features)
        rv.add(bars)
    return prior, rv.baseline()


def run_sessions(
    store: HistoryStore,
    usage: Usage,
    start: date | None = None,
    end: date | None = None,
    eligibility: EligibilityConfig | None = None,
    features_cfg: FeatureConfig | None = None,
) -> Iterator[SessionOutput]:
    """Yield sessions in [start, end]. Context always builds from the first stored session,
    so a later `start` gets the same features as a full run (deterministic warm-up)."""
    elig = eligibility or EligibilityConfig()
    fcfg = features_cfg or FeatureConfig.load()
    rv = RvolHistory(fcfg["rvol"]["reference_sessions"], fcfg["rvol"]["full_session_slots"])
    prior = PriorContext()
    for day in calendar_days(store):
        if end and day.session_date > end:
            break
        if not store.has("spy_trades", day.session_date):
            continue
        sid = f"XNYS-{day.session_date}"
        bars = build_session_bars(
            store.read("spy_trades", day.session_date), day, elig, usage
        ).to_pandas()
        pm_high = pm_low = None
        pm_flags: tuple[str, ...] = ("no_premarket_data",)
        if store.has("spy_trades_premarket", day.session_date):
            pm = premarket_levels(
                store.read("spy_trades_premarket", day.session_date), day, elig, usage
            )
            pm_high, pm_low, pm_flags = pm.high, pm.low, pm.flags
        baseline = rv.baseline()
        features, levels = compute_session_features(
            bars, sid, prior, pm_high, pm_low, baseline, fcfg
        )
        if start is None or day.session_date >= start:
            yield SessionOutput(
                day, sid, bars, features, levels, pm_flags, prior, baseline, pm_high, pm_low
            )
        prior = prior_context_from(bars, features)
        rv.add(bars)
