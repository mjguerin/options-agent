"""Shared feature engine (specs/shared.md SD-04 to SD-09; schemas.md S-4).

A pure, causal computation over one session's bars: the value at slot m uses only bars 1..m
(and prior-session context). Every value that isn't ready is None, and the slot's `not_ready`
list says which features are missing. Features are computed once and shared by all workers.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from tournament.marketdata.bars import DATA_MISSING_OR_UNCERTAIN, PRESENT

FEATURES_CONFIG = Path(__file__).resolve().parents[3] / "config" / "features" / "features-v1.yaml"

FALLBACK = "PRIOR_SESSION_FALLBACK"
CURRENT = "CURRENT_SESSION"
TREND_UP, TREND_DOWN, RANGE, NEUTRAL, INCOMPLETE = (
    "TREND_UP",
    "TREND_DOWN",
    "RANGE",
    "NEUTRAL",
    "INCOMPLETE",
)
UPPER_LEVELS = ("PDH", "PMH", "ORH")
LOWER_LEVELS = ("PDL", "PML", "ORL")
PAIRS = {"PDH": "PD", "PDL": "PD", "PMH": "PM", "PML": "PM", "ORH": "OR", "ORL": "OR"}


@dataclass(frozen=True)
class FeatureConfig:
    raw: dict
    config_hash: str

    @classmethod
    def load(cls, path: Path = FEATURES_CONFIG) -> FeatureConfig:
        raw = yaml.safe_load(path.read_text())
        digest = hashlib.sha256(json.dumps(raw, sort_keys=True, default=str).encode()).hexdigest()
        return cls(raw, digest)

    def __getitem__(self, key: str) -> dict:
        return self.raw[key]

    @property
    def version(self) -> str:
        return self.raw["feature_version"]


@dataclass(frozen=True)
class PriorContext:
    """What the session carries over from the previous one (None when there is none)."""

    final_atr: float | None = None
    pdh: float | None = None
    pdl: float | None = None
    pdc: float | None = None


@dataclass(frozen=True)
class Zone:
    zone_id: str
    version: str  # zv1 or zv2
    low: float
    high: float
    members: tuple[str, ...]
    created_slot: int  # 0 = the open
    a_zone: float


@dataclass
class SessionLevels:
    pdh: float | None
    pdl: float | None
    pdc: float | None
    pmh: float | None
    pml: float | None
    orh: float | None = None
    orl: float | None = None
    or_complete: bool = False
    zones: dict[str, list[Zone]] = field(default_factory=dict)  # zv1, zv2

    def level(self, name: str) -> float | None:
        return getattr(self, name.lower())

    def pair_midpoint(self, member: str) -> float | None:
        """SD-07 midpoint pairs (FBR targets): PD, PM, OR; None if either half is absent."""
        pair = PAIRS[member]
        hi, lo = self.level(f"{pair}H"), self.level(f"{pair}L")
        return None if hi is None or lo is None else (hi + lo) / 2


def cluster(
    levels: dict[str, float], gap: float, version: str, session: str, slot: int, a_zone: float
):
    """SD-07 single-linkage clustering: a new zone starts when the gap exceeds `gap`."""
    items = sorted(levels.items(), key=lambda kv: (kv[1], kv[0]))
    zones, cur = [], []
    for name, px in items:
        if cur and px - cur[-1][1] > gap:
            zones.append(cur)
            cur = []
        cur.append((name, px))
    if cur:
        zones.append(cur)
    return [
        Zone(
            f"{session}:{version}:{i}",
            version,
            z[0][1],
            z[-1][1],
            tuple(n for n, _ in z),
            slot,
            a_zone,
        )
        for i, z in enumerate(zones)
    ]


def _atr(bars: pd.DataFrame, cfg: FeatureConfig, prior_atr: float | None):
    period, need = cfg["atr"]["period"], cfg["atr"]["min_valid_tr_to_init"]
    a_cur, source = [], []
    trs, a, last_close = [], None, None
    for h, low, c, ok in zip(bars["high"], bars["low"], bars["close"], bars["priced"], strict=True):
        if ok:
            tr = (
                h - low
                if last_close is None
                else max(h - low, abs(h - last_close), abs(low - last_close))
            )
            last_close = c
            if a is None:
                trs.append(tr)
                if len(trs) == need:
                    a = float(np.mean(trs))
            else:
                a = ((period - 1) * a + tr) / period
        if a is not None:
            a_cur.append(a)
            source.append(CURRENT)
        elif prior_atr is not None:
            a_cur.append(prior_atr)
            source.append(FALLBACK)
        else:
            a_cur.append(None)
            source.append(None)
    return a_cur, source


def _swings(bars: pd.DataFrame, k: int, session: str):
    """SD-06: strict pivots, all 2k+1 bars priced and in-session; confirmed at p + k."""
    n = len(bars)
    hi, lo, ok = bars["high"].to_numpy(), bars["low"].to_numpy(), bars["priced"].to_numpy()
    new = [[] for _ in range(n)]
    for p in range(k, n - k):
        window = range(p - k, p + k + 1)
        if not all(ok[j] for j in window):
            continue
        others = [j for j in window if j != p]
        slot = int(bars["slot"].iloc[p])
        if all(hi[p] > hi[j] for j in others):
            new[p + k].append(
                {
                    "swing_id": f"{session}:H:{slot}",
                    "side": "high",
                    "pivot_slot": slot,
                    "price": float(hi[p]),
                    "confirmed_slot": slot + k,
                }
            )
        if all(lo[p] < lo[j] for j in others):
            new[p + k].append(
                {
                    "swing_id": f"{session}:L:{slot}",
                    "side": "low",
                    "pivot_slot": slot,
                    "price": float(lo[p]),
                    "confirmed_slot": slot + k,
                }
            )
    return new


def compute_session_features(
    bars: pd.DataFrame,
    session_id: str,
    prior: PriorContext,
    premarket_high: float | None,
    premarket_low: float | None,
    rvol_baseline: np.ndarray | None,
    cfg: FeatureConfig,
) -> tuple[pd.DataFrame, SessionLevels]:
    """`bars` holds slots 1..m in order (m may be less than the session's length: the engine is
    causal, so a truncated input yields the same values for those slots). `rvol_baseline` is an
    array of per-slot mean volumes (NaN where fewer than the required reference sessions)."""
    b = bars.reset_index(drop=True).copy()
    b["priced"] = (b["interval_status"] == PRESENT) & b["close"].notna()
    n = len(b)
    out = pd.DataFrame(
        {"slot": b["slot"], "bar_end": b["bar_end"], "interval_status": b["interval_status"]}
    )
    for col in ("open", "high", "low", "close", "volume", "session_vwap", "vwap_uncertain"):
        out[col] = b[col]
    out["entry_data_eligible"] = b["interval_status"] == PRESENT

    a_cur, source = _atr(b, cfg, prior.final_atr)
    out["a_cur"], out["atr_source"] = a_cur, source

    # VWAP readiness: defined after the first eligible trade and before any uncertainty (SD-04)
    vwap = b["session_vwap"].where(~b["vwap_uncertain"].astype(bool))
    w = cfg["vwap"]["displacement_window"]
    disp = [None] * n
    for i in range(n):
        if i >= w and a_cur[i] is not None and source[i] == CURRENT:
            v_now, v_then = vwap.iloc[i], vwap.iloc[i - w]
            if pd.notna(v_now) and pd.notna(v_then):
                disp[i] = float((v_now - v_then) / a_cur[i])
    out["disp_30_atr"] = disp

    # occupancy, side, crosses (SD-09)
    rc = cfg["regime"]
    above = np.where(b["priced"] & vwap.notna(), (b["close"] > vwap).astype(float), np.nan)
    occ = [None] * n
    for i in range(n):
        if i + 1 >= rc["occupancy_window"]:
            win = above[i + 1 - rc["occupancy_window"] : i + 1]
            valid = win[~np.isnan(win)]
            if len(valid) >= rc["min_valid_closes"]:
                occ[i] = float(valid.mean())
    out["occ_up_30"] = occ
    side, prev = [], 0
    for c, v, ok in zip(b["close"], vwap, b["priced"], strict=True):
        if ok and pd.notna(v) and c != v:
            prev = 1 if c > v else -1
        side.append(prev)
    out["side"] = side
    flips = [0] + [
        int(side[i] != side[i - 1] and side[i - 1] != 0 and side[i] != 0) for i in range(1, n)
    ]
    cw = rc["crosses_window"]
    out["crosses_60"] = [
        int(sum(flips[i + 1 - cw : i + 1])) if i + 1 >= cw else None for i in range(n)
    ]

    regime = []
    for d, o, x in zip(out["disp_30_atr"], out["occ_up_30"], out["crosses_60"], strict=True):
        if d is None or o is None or pd.isna(d) or pd.isna(o):
            regime.append(None)
        elif d >= rc["trend_disp"] and o >= rc["trend_occ"]:
            regime.append(TREND_UP)
        elif d <= -rc["trend_disp"] and o <= 1 - rc["trend_occ"]:
            regime.append(TREND_DOWN)
        elif x is None or pd.isna(x):
            regime.append(INCOMPLETE)
        elif abs(d) <= rc["range_disp"] and x >= rc["range_crosses"]:
            regime.append(RANGE)
        else:
            regime.append(NEUTRAL)
    out["regime"] = regime

    out["swings_new"] = _swings(b, cfg["swings"]["k"], session_id)

    # RVOL (SD-08): a missing-data slot has no value; a real zero volume is 0
    rv = [None] * n
    if rvol_baseline is not None:
        for i in range(n):
            s = int(b["slot"].iloc[i])
            base = rvol_baseline[s - 1] if s - 1 < len(rvol_baseline) else np.nan
            if (
                b["interval_status"].iloc[i] != DATA_MISSING_OR_UNCERTAIN
                and np.isfinite(base)
                and base > 0
            ):
                rv[i] = float(b["volume"].iloc[i] / base)
    out["rvol"] = rv

    # levels and zones (SD-07)
    levels = SessionLevels(prior.pdh, prior.pdl, prior.pdc, premarket_high, premarket_low)
    gap = cfg["levels"]["cluster_gap_atr"]
    zv1_members = {
        k: levels.level(k) for k in ("PDH", "PDL", "PMH", "PML") if levels.level(k) is not None
    }
    if prior.final_atr is not None and zv1_members:
        levels.zones["zv1"] = cluster(
            zv1_members, gap * prior.final_atr, "zv1", session_id, 0, prior.final_atr
        )
    orn = cfg["levels"]["opening_range_slots"]
    if n >= orn:
        first = b.iloc[:orn]
        if bool(first["priced"].all()):
            levels.orh, levels.orl, levels.or_complete = (
                float(first["high"].max()),
                float(first["low"].min()),
                True,
            )
        a15, s15 = a_cur[orn - 1], source[orn - 1]
        if s15 == CURRENT:
            members = dict(zv1_members)
            if levels.or_complete:
                members |= {"ORH": levels.orh, "ORL": levels.orl}
            levels.zones["zv2"] = cluster(members, gap * a15, "zv2", session_id, orn, a15)
    out["zone_version"] = [
        "zv2" if "zv2" in levels.zones and s >= orn else ("zv1" if "zv1" in levels.zones else None)
        for s in out["slot"]
    ]

    needed = ("a_cur", "disp_30_atr", "occ_up_30", "crosses_60", "regime", "rvol", "zone_version")
    out["not_ready"] = [
        [
            c
            for c in needed
            if out[c].iloc[i] is None
            or (not isinstance(out[c].iloc[i], str) and pd.isna(out[c].iloc[i]))
        ]
        + (["atr_current"] if source[i] != CURRENT else [])
        for i in range(n)
    ]
    return out, levels


def prior_context_from(bars: pd.DataFrame, features: pd.DataFrame) -> PriorContext:
    """Context the next session inherits: final ATR, and this session's high, low and last close."""
    priced = bars[(bars["interval_status"] == PRESENT) & bars["close"].notna()]
    final = features["a_cur"].iloc[-1] if features["atr_source"].iloc[-1] == CURRENT else None
    if priced.empty:
        return PriorContext(final_atr=final)
    return PriorContext(
        final,
        float(priced["high"].max()),
        float(priced["low"].min()),
        float(priced["close"].iloc[-1]),
    )
