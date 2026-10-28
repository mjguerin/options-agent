"""Where the live loop spends its time: a measuring aid that never changes behaviour.

On 2026-10-28 option quotes arrived up to 201 s late for minutes at a time while the stock feed in
the same process stayed within 3 s. These meters say, second by second, whether a feed is waiting
for data (the delay is upstream) or busy working (the delay is ours), how large its frames are,
how late its newest quote was, and how long the event loop itself was blocked. They add a few
clock reads per frame.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pandas as pd


class LoadMeter:
    """Per-second totals for one feed's read loop."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.rows: list[dict] = []
        self._sec: int | None = None
        self._reset()

    def _reset(self) -> None:
        self._frames = self._msgs = self._bytes = 0
        self._busy = self._wait = self._max_frame = 0.0
        self._max_lag: float | None = None

    def _roll(self, sec: int) -> None:
        if self._sec is not None and sec != self._sec:
            self.rows.append(
                {
                    "second": self._sec,
                    "feed": self.name,
                    "frames": self._frames,
                    "msgs": self._msgs,
                    "bytes": self._bytes,
                    "busy_s": round(self._busy, 6),
                    "wait_s": round(self._wait, 6),
                    "max_frame_ms": round(self._max_frame * 1000, 3),
                    "max_lag_s": self._max_lag,
                }
            )
            self._reset()
        self._sec = sec

    def frame(
        self, at: pd.Timestamp, nbytes: int, nmsgs: int, busy_s: float, lag_s: float | None
    ) -> None:
        self._roll(int(at.timestamp()))
        self._frames += 1
        self._msgs += nmsgs
        self._bytes += nbytes
        self._busy += busy_s
        self._max_frame = max(self._max_frame, busy_s)
        if lag_s is not None and (self._max_lag is None or lag_s > self._max_lag):
            self._max_lag = lag_s

    def waited(self, at: pd.Timestamp, wait_s: float) -> None:
        self._roll(int(at.timestamp()))
        self._wait += wait_s

    def finish(self) -> None:
        if self._sec is not None:
            self._roll(self._sec + 1)


class LoopProbe:
    """How late the event loop wakes a 100 ms timer. A blocked loop shows up here whatever blocked
    it: strategy steps, database calls, publishing."""

    def __init__(self, interval_s: float) -> None:
        self.interval_s = interval_s
        self.rows: list[dict] = []

    async def run(self, until: pd.Timestamp, clock) -> None:
        sec, worst, total, n = None, 0.0, 0.0, 0
        expected = time.perf_counter() + self.interval_s
        while clock() < until:
            await asyncio.sleep(self.interval_s)
            now = time.perf_counter()
            late = max(now - expected, 0.0)
            expected = now + self.interval_s
            s = int(clock().timestamp())
            if sec is not None and s != sec:
                self.rows.append(
                    {
                        "second": sec,
                        "loop_lag_max_ms": round(worst * 1000, 3),
                        "loop_lag_mean_ms": round(total / max(n, 1) * 1000, 3),
                    }
                )
                worst, total, n = 0.0, 0.0, 0
            sec = s
            worst, total, n = max(worst, late), total + late, n + 1


def write_load(out: Path, meters: list[LoadMeter], probe: LoopProbe | None) -> dict:
    """Save the per-second tables and return a short summary for summary.json."""
    for m in meters:
        m.finish()
    feeds = pd.DataFrame([r for m in meters for r in m.rows])
    loop = pd.DataFrame(probe.rows if probe else [])
    if not feeds.empty:
        feeds.to_parquet(out / "feed_load.parquet", index=False)
    if not loop.empty:
        loop.to_parquet(out / "loop_load.parquet", index=False)
    summary: dict = {}
    for name, g in feeds.groupby("feed") if not feeds.empty else []:
        summary[name] = {
            "frames": int(g["frames"].sum()),
            "msgs": int(g["msgs"].sum()),
            "busiest_second_msgs": int(g["msgs"].max()),
            "busiest_second_frames": int(g["frames"].max()),
            "busy_share": round(float(g["busy_s"].sum() / max(len(g), 1)), 4),
            "worst_second_busy_s": round(float(g["busy_s"].max()), 3),
            "worst_frame_ms": float(g["max_frame_ms"].max()),
            "worst_lag_s": None if g["max_lag_s"].dropna().empty else float(g["max_lag_s"].max()),
        }
    if not loop.empty:
        summary["loop"] = {
            "worst_block_ms": float(loop["loop_lag_max_ms"].max()),
            "seconds_blocked_over_250ms": int((loop["loop_lag_max_ms"] > 250).sum()),
            "seconds_blocked_over_1s": int((loop["loop_lag_max_ms"] > 1000).sum()),
        }
    return summary
