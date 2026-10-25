"""Feature engine: SD acceptance scenarios (specs/acceptance_tests.md) and causality."""

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from tournament.features.engine import (
    CURRENT,
    FALLBACK,
    INCOMPLETE,
    RANGE,
    TREND_UP,
    FeatureConfig,
    PriorContext,
    SessionLevels,
    cluster,
    compute_session_features,
)
from tournament.features.pipeline import RvolHistory
from tournament.marketdata.bars import DATA_MISSING_OR_UNCERTAIN, KNOWN_NO_ELIGIBLE_TRADES, PRESENT

CFG = FeatureConfig.load()
OPEN = datetime(2024, 6, 3, 13, 30, tzinfo=UTC)


def make_bars(close, high=None, low=None, vwap=None, volume=None, status=None):
    n = len(close)
    close = np.asarray(close, dtype=float)
    high = close + 0.1 if high is None else np.asarray(high, float)
    low = close - 0.1 if low is None else np.asarray(low, float)
    vwap = np.cumsum(close) / np.arange(1, n + 1) if vwap is None else np.asarray(vwap, float)
    status = [PRESENT] * n if status is None else status
    df = pd.DataFrame(
        {
            "slot": np.arange(1, n + 1),
            "bar_end": [OPEN + timedelta(minutes=i + 1) for i in range(n)],
            "interval_status": status,
            "open": close,
            "high": high,
            "low": low,
            "close": close,
            "volume": np.full(n, 1000) if volume is None else volume,
            "session_vwap": vwap,
            "vwap_uncertain": np.zeros(n, bool),
        }
    )
    priced = df["interval_status"] == PRESENT
    for c in ("open", "high", "low", "close"):
        df.loc[~priced, c] = np.nan
    unc = np.maximum.accumulate((df["interval_status"] == DATA_MISSING_OR_UNCERTAIN).to_numpy())
    df["vwap_uncertain"] = unc
    return df


DEFAULT_PRIOR = PriorContext(final_atr=0.2)


def features(bars, prior=DEFAULT_PRIOR, pmh=None, pml=None, baseline=None):
    return compute_session_features(bars, "XNYS-2024-06-03", prior, pmh, pml, baseline, CFG)


def test_atr_fallback_then_init_at_14_then_wilder():  # SH-09
    close = [500 + 0.0 * i for i in range(16)]
    high = [c + 0.5 for c in close]
    low = [c - 0.5 for c in close]  # every TR = 1.0
    high[14] = close[14] + 1.5  # slot 15: TR = 2.0
    f, _ = features(make_bars(close, high, low))
    assert list(f["atr_source"][:13]) == [FALLBACK] * 13 and list(f["a_cur"][:13]) == [0.2] * 13
    assert f["atr_source"][13] == CURRENT and f["a_cur"][13] == pytest.approx(1.0)
    assert f["a_cur"][14] == pytest.approx((13 * 1.0 + 2.0) / 14)


def test_atr_not_ready_without_prior_session():
    f, _ = features(make_bars([500.0] * 20), prior=PriorContext())
    assert pd.isna(f["a_cur"][12]) and pd.isna(f["atr_source"][12]) and not pd.isna(f["a_cur"][13])


def test_atr_carries_through_no_price_slots_and_inits_on_14th_valid_tr():
    status = [PRESENT] * 20
    status[5] = KNOWN_NO_ELIGIBLE_TRADES
    f, _ = features(make_bars([500.0] * 20, status=status))
    assert (
        f["atr_source"][13] == FALLBACK and f["atr_source"][14] == CURRENT
    )  # 14th valid TR at slot 15


def test_displacement_first_defined_at_slot_31():  # SH-10
    f, _ = features(make_bars(np.linspace(500, 503, 40)))
    assert pd.isna(f["disp_30_atr"][29]) and not pd.isna(f["disp_30_atr"][30])


def test_crosses_and_incomplete_regime_before_slot_60():  # SH-11, SH-42
    close = 500 + 0.3 * np.sin(np.arange(80) / 3)
    f, _ = features(make_bars(close, vwap=np.full(80, 500.0)))
    assert pd.isna(f["crosses_60"][58]) and not pd.isna(f["crosses_60"][59])
    assert f["regime"][44] == INCOMPLETE
    assert f["regime"][70] == RANGE


def test_side_carries_on_exact_equality():  # SH-12
    f, _ = features(make_bars([501, 500, 499], vwap=[500, 500, 500]))
    assert list(f["side"]) == [1, 1, -1]


def test_trend_up_regime():
    close = np.linspace(500, 512, 60)
    f, _ = features(make_bars(close, vwap=close - 0.5))  # VWAP rising, price above it
    assert f["regime"][45] == TREND_UP


def test_clustering_is_transitive():  # SH-13
    zones = cluster({"A": 100.0, "B": 100.8, "C": 101.6}, 1.0, "zv2", "s", 15, 1.0)
    assert len(zones) == 1 and (zones[0].low, zones[0].high) == (100.0, 101.6)


def test_zones_v1_at_open_and_v2_with_or():
    f, lv = features(
        make_bars(np.full(30, 500.0)),
        prior=PriorContext(0.2, 505.0, 495.0, 500.0),
        pmh=503.0,
        pml=497.0,
    )
    assert {z.members for z in lv.zones["zv1"]} == {("PDL",), ("PML",), ("PMH",), ("PDH",)}
    assert lv.or_complete and lv.orh == pytest.approx(500.1)
    assert list(f["zone_version"][[0, 13, 14, 20]]) == ["zv1", "zv1", "zv2", "zv2"]
    assert any("ORH" in z.members for z in lv.zones["zv2"])


def test_incomplete_or_excludes_or_levels():  # SH-15
    status = [PRESENT] * 30
    status[7] = KNOWN_NO_ELIGIBLE_TRADES
    _, lv = features(
        make_bars(np.full(30, 500.0), status=status), prior=PriorContext(0.2, 505.0, 495.0, 500.0)
    )
    assert not lv.or_complete and lv.orh is None
    assert all("ORH" not in z.members for z in lv.zones["zv2"])


def test_swing_tie_and_session_edge():  # SH-16, SH-17
    high = [1, 2, 5, 5, 2, 1, 2, 3, 6, 3]
    f, _ = features(make_bars(np.zeros(10), high=high, low=[-1] * 10))
    found = [s for lst in f["swings_new"] for s in lst if s["side"] == "high"]
    assert [s["pivot_slot"] for s in found] == []  # slots 3/4 tie; slot 9 is too near the edge


def test_swing_confirmed_k_slots_after_pivot():
    high = [1, 2, 3, 9, 3, 2, 1, 1]
    f, _ = features(make_bars(np.zeros(8), high=high, low=[-1] * 8))
    assert f["swings_new"][5] and f["swings_new"][5][0] == {
        "swing_id": "XNYS-2024-06-03:H:4",
        "side": "high",
        "pivot_slot": 4,
        "price": 9.0,
        "confirmed_slot": 6,
    }
    assert all(not f["swings_new"][i] for i in range(5))


def test_rvol_ready_missing_and_zero():
    status = [PRESENT, DATA_MISSING_OR_UNCERTAIN, KNOWN_NO_ELIGIBLE_TRADES]
    base = np.array([500.0, 500.0, 500.0])
    f, _ = features(
        make_bars([500.0] * 3, volume=np.array([1000, 1000, 0]), status=status), baseline=base
    )
    assert f["rvol"][0] == 2.0 and pd.isna(f["rvol"][1]) and f["rvol"][2] == 0.0
    f2, _ = features(make_bars([500.0] * 3))
    assert pd.isna(f2["rvol"][0])


def test_vwap_uncertainty_stops_displacement():
    status = [PRESENT] * 40
    status[35] = DATA_MISSING_OR_UNCERTAIN
    f, _ = features(make_bars(np.linspace(500, 503, 40), status=status))
    assert not pd.isna(f["disp_30_atr"][34]) and pd.isna(f["disp_30_atr"][36])


def test_rvol_history_needs_full_sessions_and_skips_missing_slots():
    h = RvolHistory(sessions=3, full_slots=4)
    for v in (100, 200, 300):
        h.add(make_bars([500.0] * 4, volume=np.full(4, v)))
    h.add(make_bars([500.0] * 2, volume=np.full(2, 999)))  # early close: ignored
    status = [PRESENT, DATA_MISSING_OR_UNCERTAIN, PRESENT, PRESENT]
    h.add(make_bars([500.0] * 4, volume=np.full(4, 400), status=status))
    base = h.baseline()
    assert base[0] == pytest.approx(300) and base[1] == pytest.approx(
        200
    )  # slot 2 skips the missing value


def test_pair_midpoints():
    lv = SessionLevels(pdh=510, pdl=490, pdc=500, pmh=505, pml=None, orh=502, orl=498)
    assert (
        lv.pair_midpoint("PDH") == 500
        and lv.pair_midpoint("ORL") == 500
        and lv.pair_midpoint("PMH") is None
    )


def _random_session(seed, n=390):
    rng = np.random.default_rng(seed)
    close = 500 + np.cumsum(rng.normal(0, 0.15, n))
    high = close + rng.uniform(0, 0.3, n)
    low = close - rng.uniform(0, 0.3, n)
    status = np.where(rng.uniform(size=n) < 0.02, KNOWN_NO_ELIGIBLE_TRADES, PRESENT).tolist()
    return make_bars(close, high, low, volume=rng.integers(100, 5000, n), status=status)


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_no_look_ahead_truncation_property(seed):
    """Features for slots 1..m from bars truncated at m equal those from the full session."""
    bars = _random_session(seed)
    prior = PriorContext(0.2, 505.0, 495.0, 500.0)
    base = np.full(390, 1500.0)
    full, _ = features(bars, prior, 503.0, 497.0, base)
    for m in (1, 14, 15, 31, 59, 60, 61, 137, 389):
        part, _ = features(bars.iloc[:m], prior, 503.0, 497.0, base)
        for col in full.columns:
            a, b = list(full[col][:m]), list(part[col])
            assert all(
                x == y or (x is None and y is None) or (pd.isna(x) and pd.isna(y))
                if not isinstance(x, list)
                else x == y
                for x, y in zip(a, b, strict=True)
            ), (col, m)
