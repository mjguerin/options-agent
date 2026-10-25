"""Synthetic feature frames for strategy acceptance tests (specs/acceptance_tests.md).

Defaults follow the acceptance-test conventions: A = 1.00 (CURRENT), RVOL ready and high, guards
pass, full-length session. Tests then set exact bars, VWAP, regime, swings and zones.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pandas as pd

from tournament.features.engine import SessionLevels, Zone
from tournament.features.pipeline import SessionOutput
from tournament.marketdata.calendar import SessionDay
from tournament.replay.harness import ReplayRun

OPEN = datetime(2024, 6, 3, 13, 30, tzinfo=UTC)
DAY = date(2024, 6, 3)
SID = f"XNYS-{DAY}"
MIRROR = 1000.0
LEVEL_SWAP = {"PDH": "PDL", "PDL": "PDH", "PMH": "PML", "PML": "PMH", "ORH": "ORL", "ORL": "ORH"}


def frame(
    n: int = 390, price: float = 500.0, vwap: float = 500.0, regime: str = "NEUTRAL"
) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "slot": range(1, n + 1),
            "bar_end": [OPEN + timedelta(minutes=i + 1) for i in range(n)],
            "interval_status": ["PRESENT"] * n,
            "open": [price] * n,
            "high": [price + 0.05] * n,
            "low": [price - 0.05] * n,
            "close": [price] * n,
            "volume": [1000] * n,
            "session_vwap": [vwap] * n,
            "vwap_uncertain": [False] * n,
            "entry_data_eligible": [True] * n,
            "a_cur": [1.0] * n,
            "atr_source": ["CURRENT_SESSION"] * n,
            "disp_30_atr": [0.0] * n,
            "occ_up_30": [0.5] * n,
            "side": [1] * n,
            "crosses_60": [5] * n,
            "regime": [regime] * n,
            "rvol": [2.0] * n,
            "zone_version": ["zv2"] * n,
            "swings_new": [[] for _ in range(n)],
            "not_ready": [[] for _ in range(n)],
        }
    )


def bar(df: pd.DataFrame, slot: int, high: float, low: float, close: float, **extra) -> None:
    i = slot - 1
    df.at[i, "high"], df.at[i, "low"], df.at[i, "close"], df.at[i, "open"] = high, low, close, close
    for k, v in extra.items():
        df.at[i, k] = v


def setcol(df: pd.DataFrame, col: str, value, start: int = 1, end: int | None = None) -> None:
    end = end or len(df)
    for s in range(start, end + 1):
        df.at[s - 1, col] = value


def swing(df: pd.DataFrame, side: str, pivot: int, price: float, k: int = 2) -> None:
    lst = list(df.at[pivot + k - 1, "swings_new"])
    lst.append(
        {
            "swing_id": f"{SID}:{side[0].upper()}:{pivot}",
            "side": side,
            "pivot_slot": pivot,
            "price": price,
            "confirmed_slot": pivot + k,
        }
    )
    df.at[pivot + k - 1, "swings_new"] = lst


def zone(
    low: float, high: float, members: tuple[str, ...], zid: str = "z", version: str = "zv2"
) -> Zone:
    return Zone(f"{SID}:{version}:{zid}", version, low, high, members, 15, 1.0)


def levels(zones: list[Zone], **lv) -> SessionLevels:
    base = dict(
        pdh=None, pdl=None, pdc=None, pmh=None, pml=None, orh=None, orl=None, or_complete=False
    )
    base.update(lv)
    out = SessionLevels(**base)
    out.zones = {"zv2": zones, "zv1": zones}
    return out


def run(worker, df: pd.DataFrame, lv: SessionLevels | None = None, run_id: str = "test-run"):
    out = SessionOutput(
        SessionDay(DAY, OPEN, OPEN + timedelta(minutes=len(df))),
        SID,
        None,
        df,
        lv or levels([]),
        (),
    )
    return ReplayRun([worker], run_id=run_id, dataset_id="test").run_outputs([out])


# ---- mirror (S8): reflect prices around MIRROR; swap sides, regimes and level names --------
def mirror_frame(df: pd.DataFrame) -> pd.DataFrame:
    m = df.copy()
    m["high"], m["low"] = MIRROR - df["low"], MIRROR - df["high"]
    m["open"], m["close"] = MIRROR - df["open"], MIRROR - df["close"]
    m["session_vwap"] = MIRROR - df["session_vwap"]
    m["disp_30_atr"] = -df["disp_30_atr"]
    m["side"] = -df["side"]
    m["regime"] = df["regime"].map(
        lambda r: {"TREND_UP": "TREND_DOWN", "TREND_DOWN": "TREND_UP"}.get(r, r)
    )
    m["swings_new"] = df["swings_new"].map(
        lambda lst: [
            {**sw, "side": "low" if sw["side"] == "high" else "high", "price": MIRROR - sw["price"]}
            for sw in lst
        ]
    )
    return m


def mirror_levels(lv: SessionLevels) -> SessionLevels:
    def f(x):
        return None if x is None else MIRROR - x

    out = SessionLevels(
        pdh=f(lv.pdl),
        pdl=f(lv.pdh),
        pdc=f(lv.pdc),
        pmh=f(lv.pml),
        pml=f(lv.pmh),
        orh=f(lv.orl),
        orl=f(lv.orh),
        or_complete=lv.or_complete,
    )
    out.zones = {
        v: [
            replace(
                z,
                low=MIRROR - z.high,
                high=MIRROR - z.low,
                members=tuple(LEVEL_SWAP.get(n, n) for n in z.members),
            )
            for z in zs
        ]
        for v, zs in lv.zones.items()
    }
    return out


def outcome_at(result, slot: int) -> str:
    return result.evaluations.set_index("slot").loc[slot, "outcome"]


def reasons_at(result, slot: int) -> list[str]:
    ev = result.evaluations.set_index("slot").loc[slot]
    tr = result.transitions
    t = tr[tr["slot"] == slot] if not tr.empty else tr
    return list(ev["reason_codes"]) + (
        [r for rs in t["reason_codes"] for r in rs] if not t.empty else []
    )
