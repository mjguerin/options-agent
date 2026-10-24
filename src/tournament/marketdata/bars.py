"""Minute bars from SIP trades (SD-01, SD-03, SD-04, SD-07; D-04 adapter).

Two stages per trade, in evaluation order (timestamp, then stored provider order: a
deterministic approximation of tape order, not a reconstruction of it):
1. SIP: classify conditions and resolve consolidated-last eligibility against LastState.
2. Application: open/close/high/low and VWAP use the resolved last-eligible set (E-1, E-2,
   E-4(a)); volume uses the SIP volume rule (E-3); informational records never count (E-7).
Every scheduled slot gets a row, priced or not; prices are never forward-filled.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import numpy as np
import pandas as pd
import pyarrow as pa

from tournament.marketdata.calendar import SessionDay
from tournament.marketdata.eligibility import EligibilityConfig, LastState, Usage

PRESENT = "PRESENT"
KNOWN_NO_ELIGIBLE_TRADES = "KNOWN_NO_ELIGIBLE_TRADES"
DATA_MISSING_OR_UNCERTAIN = "DATA_MISSING_OR_UNCERTAIN"

BAR_SCHEMA = pa.schema(
    [
        ("slot", pa.int16()),
        ("bar_start", pa.timestamp("ns", tz="UTC")),
        ("bar_end", pa.timestamp("ns", tz="UTC")),
        ("interval_status", pa.string()),
        ("open", pa.float64()),
        ("high", pa.float64()),
        ("low", pa.float64()),
        ("close", pa.float64()),
        ("volume", pa.int64()),
        ("trade_count", pa.int64()),
        ("vwap_notional", pa.float64()),
        ("vwap_volume", pa.int64()),
        ("cum_vwap_notional", pa.float64()),
        ("cum_vwap_volume", pa.int64()),
        ("cum_total_volume", pa.int64()),
        ("session_vwap", pa.float64()),
        ("vwap_uncertain", pa.bool_()),
        ("quarantined_trades", pa.int32()),
        ("flags", pa.list_(pa.string())),
    ]
)


def _stage1(
    trades: pd.DataFrame,
    day,
    cfg: EligibilityConfig,
    usage: Usage,
    listing_market: str,
    strip_marker: str | None = None,
) -> pd.DataFrame:
    """Classify every trade and resolve last eligibility in order. Returns the frame with
    columns: last_ok (SIP), vol_ok, info, unknown, flags."""
    keys = trades["conditions"].map(
        lambda c: tuple(x for x in c if x != strip_marker) if strip_marker else tuple(c)
    )
    classes = {k: cfg.classify(k, day, usage) for k in keys.unique()}
    cls = keys.map(classes)
    df = trades.assign(
        _last=cls.map(lambda c: c.last),
        vol_ok=cls.map(lambda c: c.volume == "YES"),
        info=cls.map(lambda c: c.informational),
        unknown=cls.map(lambda c: c.unknown),
        cflags=cls.map(lambda c: c.flags),
    )
    state = LastState(listing_market)
    last_ok = np.zeros(len(df), dtype=bool)
    rules, exch, unk = df["_last"].to_numpy(), df["exchange"].to_numpy(), df["unknown"].to_numpy()
    for i in np.flatnonzero((rules != "NO") & ~unk):
        last_ok[i] = state.resolve(rules[i], exch[i])
    return df.assign(last_ok=last_ok)


def build_session_bars(
    trades: pa.Table | pd.DataFrame,
    day: SessionDay,
    cfg: EligibilityConfig,
    usage: Usage,
    symbol: str = "SPY",
    gap_slots: frozenset[int] = frozenset(),
) -> pa.Table:
    """Regular-session bars for every scheduled slot of `day`."""
    t = trades.to_pandas() if isinstance(trades, pa.Table) else trades.copy()
    t = t.sort_values("ts", kind="stable").reset_index(drop=True)
    n_slots = int((day.close_utc - day.open_utc) / timedelta(minutes=1))
    open_ns = pd.Timestamp(day.open_utc).value
    ts_ns = (
        t["ts"].dt.tz_convert("UTC").dt.tz_localize(None).to_numpy("datetime64[ns]").astype("int64")
    )
    t["slot"] = (ts_ns - open_ns) // 60_000_000_000 + 1
    outside = ~t["slot"].between(1, n_slots)
    t = t[~outside].reset_index(drop=True)
    df = _stage1(t, day.session_date, cfg, usage, cfg.listing_market[symbol])

    usable = ~df["unknown"] & ~df["info"]
    px = df[usable & df["last_ok"]]  # E-1, E-2, E-4(a): one price population
    vol = df[usable & df["vol_ok"]]  # E-3
    g_px, g_vol = px.groupby("slot"), vol.groupby("slot")
    agg = pd.DataFrame(index=pd.RangeIndex(1, n_slots + 1, name="slot"))
    agg["open"] = g_px["price"].first()
    agg["close"] = g_px["price"].last()
    agg["high"] = g_px["price"].max()
    agg["low"] = g_px["price"].min()
    agg["volume"] = g_vol["size"].sum()
    agg["trade_count"] = g_vol["size"].count()
    agg["vwap_notional"] = (px["price"] * px["size"]).groupby(px["slot"]).sum()
    agg["vwap_volume"] = g_px["size"].sum()
    agg["quarantined_trades"] = df[df["unknown"]].groupby("slot").size()
    for c in ("volume", "trade_count", "vwap_volume", "quarantined_trades"):
        agg[c] = agg[c].fillna(0).astype("int64")
    agg["vwap_notional"] = agg["vwap_notional"].fillna(0.0)

    status = np.where(agg["open"].notna(), PRESENT, KNOWN_NO_ELIGIBLE_TRADES).astype(object)
    uncertain = (agg["quarantined_trades"] > 0).to_numpy() | agg.index.isin(list(gap_slots))
    status[uncertain] = DATA_MISSING_OR_UNCERTAIN
    agg["interval_status"] = status
    agg["vwap_uncertain"] = np.maximum.accumulate(uncertain)
    agg["cum_vwap_notional"] = agg["vwap_notional"].cumsum()
    agg["cum_vwap_volume"] = agg["vwap_volume"].cumsum()
    agg["cum_total_volume"] = agg["volume"].cumsum()
    agg["session_vwap"] = (agg["cum_vwap_notional"] / agg["cum_vwap_volume"]).where(
        agg["cum_vwap_volume"] > 0
    )
    flagged = (
        df[df["cflags"].map(bool)].groupby("slot")["cflags"].agg(lambda s: sorted(set().union(*s)))
    )
    agg["flags"] = flagged.reindex(agg.index).map(lambda v: v if isinstance(v, list) else [])
    agg["bar_start"] = [day.open_utc + timedelta(minutes=m - 1) for m in agg.index]
    agg["bar_end"] = [day.open_utc + timedelta(minutes=m) for m in agg.index]
    out = agg.reset_index()
    return pa.Table.from_pandas(out[BAR_SCHEMA.names], schema=BAR_SCHEMA, preserve_index=False)


@dataclass(frozen=True)
class PremarketLevels:
    high: float | None
    low: float | None
    eligible_trades: int
    quarantined_trades: int
    flags: tuple[str, ...]


def premarket_levels(
    trades: pa.Table | pd.DataFrame,
    day: SessionDay,
    cfg: EligibilityConfig,
    usage: Usage,
    symbol: str = "SPY",
) -> PremarketLevels:
    """PMH/PML under E-6: T is the session marker; every other condition must pass the
    last rule, against a SEPARATE application_premarket_price_state (never SIP state).
    Any quarantined trade makes the levels uncertain (null), per D-36."""
    t = trades.to_pandas() if isinstance(trades, pa.Table) else trades.copy()
    t = t.sort_values("ts", kind="stable").reset_index(drop=True)
    if t.empty:
        return PremarketLevels(None, None, 0, 0, ("no_premarket_trades",))
    df = _stage1(t, day.session_date, cfg, usage, cfg.listing_market[symbol], strip_marker="T")
    q = int(df["unknown"].sum())
    flags = tuple(sorted(set().union(*df["cflags"])))
    if q:
        return PremarketLevels(None, None, 0, q, (*flags, "premarket_uncertain"))
    px = df.loc[df["last_ok"] & ~df["info"], "price"]
    if px.empty:
        return PremarketLevels(None, None, 0, 0, (*flags, "no_eligible_premarket_trades"))
    return PremarketLevels(float(px.max()), float(px.min()), int(len(px)), 0, flags)
