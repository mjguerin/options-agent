"""Synthetic fixtures for the D-04 bar builder (specs/eligibility.md checklist item 4).

Expected values are written by hand from CTS 2.11b p.82-85, not derived from the code under test.
"""

from datetime import date, time, timedelta

import pandas as pd
import pytest

from tournament.marketdata.bars import (
    DATA_MISSING_OR_UNCERTAIN,
    KNOWN_NO_ELIGIBLE_TRADES,
    PRESENT,
    build_session_bars,
    premarket_levels,
)
from tournament.marketdata.calendar import session_from_exchange_times
from tournament.marketdata.eligibility import EligibilityConfig, Usage

CFG = EligibilityConfig()
DAY = session_from_exchange_times(
    date(2024, 6, 3), time(9, 30), time(16, 0)
)  # after both E-5 windows
DEV = Usage.DEVELOPMENT


def conds(code: str) -> tuple[str, ...]:
    """Alpaca encoding: category-1 char (space or C/N/R) first, then other codes."""
    if code == "SPACE":
        return (" ",)
    return (code,) if code in ("C", "N", "R") else (" ", code)


# code -> (consolidated last, volume, informational); "UNKNOWN" = quarantined
SINGLE = {
    "SPACE": ("YES", "YES", False),
    "B": ("NO", "YES", False),
    "C": ("NO", "YES", False),
    "E": ("YES", "YES", False),
    "F": ("YES", "YES", False),
    "H": ("NO", "YES", False),
    "I": ("NO", "YES", False),
    "K": ("YES", "YES", False),
    "L": ("NOTE_3", "YES", False),
    "M": ("NO", "NO", True),
    "N": ("UNKNOWN", None, None),
    "O": ("YES", "YES", False),
    "P": ("NOTE_2", "YES", False),
    "Q": ("NO", "NO", True),
    "R": ("NO", "YES", False),
    "T": ("NO", "YES", False),
    "U": ("NO", "YES", False),
    "V": ("NO", "YES", False),
    "X": ("YES", "YES", False),
    "Z": ("NOTE_2", "YES", False),
    "4": ("NOTE_2", "YES", False),
    "5": ("YES", "YES", False),
    "6": ("YES", "YES", False),
    "7": ("NO", "YES", False),
    "8": ("UNKNOWN", None, None),
    "9": ("YES", "NO", True),
}


@pytest.mark.parametrize("code", sorted(SINGLE))
def test_every_matrix_row(code):
    last, vol, info = SINGLE[code]
    c = CFG.classify(conds(code), DAY.session_date, DEV)
    if last == "UNKNOWN":
        assert c.unknown
    else:
        assert (c.last, c.volume, c.informational, c.unknown) == (last, vol, info, False)


# Every combination observed in the 70-session regular-session sample (except N, tested below):
# combination -> (last, volume)
OBSERVED = {
    " |I": ("NO", "YES"),
    " ": ("YES", "YES"),
    " |F": ("YES", "YES"),
    " |F|I": ("NO", "YES"),
    " |4|I": ("NO", "YES"),
    " |4|B": ("NO", "YES"),
    " |7|V": ("NO", "YES"),
    " |Z|I": ("NO", "YES"),
    " |7|I": ("NO", "YES"),
    " |F|T": ("NO", "YES"),
    " |B": ("NO", "YES"),
    " |T": ("NO", "YES"),
    " |4|Z|I": ("NO", "YES"),
    " |F|T|I": ("NO", "YES"),
    " |T|I": ("NO", "YES"),
    " |4": ("NOTE_2", "YES"),
    "C|I": ("NO", "YES"),
    " |7": ("NO", "YES"),
    "C": ("NO", "YES"),
    " |Q": ("NO", "NO"),
    " |O": ("YES", "YES"),
    "C|B": ("NO", "YES"),
    " |Z": ("NOTE_2", "YES"),
    " |O|X": ("YES", "YES"),
    "R": ("NO", "YES"),
    " |O|I": ("NO", "YES"),
    " |7|Z|V": ("NO", "YES"),
    " |4|Z|B": ("NO", "YES"),
    " |X": ("YES", "YES"),
    " |P": ("NOTE_2", "YES"),
    " |4|P": ("NOTE_2", "YES"),
    " |U|I": ("NO", "YES"),
    " |Z|B": ("NO", "YES"),
    "R|I": ("NO", "YES"),
    " |F|B": ("NO", "YES"),
    "C|P": ("NO", "YES"),
    " |U": ("NO", "YES"),
}


@pytest.mark.parametrize("combo", sorted(OBSERVED))
def test_observed_combinations(combo):
    c = CFG.classify(tuple(combo.split("|")), DAY.session_date, DEV)
    assert not c.unknown
    assert (c.last, c.volume) == OBSERVED[combo]


def test_n_before_may_2024_is_a_development_assumption():
    c = CFG.classify(("N", "I"), date(2024, 3, 1), DEV)
    assert (c.last, c.volume, c.unknown) == ("NO", "YES", False)
    assert "provider_evidence:N" in c.flags
    assert CFG.classify(("N", "I"), date(2024, 3, 1), Usage.HOLDOUT).unknown  # not for held-out use


def test_o_before_march_28_2024_is_backfilled_in_development_only():
    c = CFG.classify((" ", "O"), date(2024, 2, 5), DEV)
    assert (c.last, c.unknown) == ("YES", False) and "rule_backfilled:O" in c.flags
    assert CFG.classify((" ", "O"), date(2024, 2, 5), Usage.LIVE).unknown
    assert not CFG.classify((" ", "O"), date(2024, 3, 28), Usage.HOLDOUT).unknown  # verified row


@pytest.mark.parametrize(
    "combo, reason",
    [
        ((" ", "F", "4"), "C1"),
        ((" ", "U", "F"), "C2"),
        ((" ", "L", "Z"), "C3"),
        ((" ", "Y"), "unknown"),
    ],
)
def test_constraint_violations_and_unknown_codes_quarantine(combo, reason):
    assert CFG.classify(combo, DAY.session_date, DEV).unknown


# ---- bar building ------------------------------------------------------------------------


def trade(minute: int, sec: float, price: float, size: int, c: tuple[str, ...], exch: str = "P"):
    ts = DAY.open_utc + timedelta(minutes=minute - 1, seconds=sec)
    return {
        "ts": pd.Timestamp(ts),
        "price": price,
        "size": size,
        "exchange": exch,
        "conditions": list(c),
        "trade_id": None,
        "tape": "B",
    }


def bars(rows, gaps=frozenset()):
    return (
        build_session_bars(pd.DataFrame(rows), DAY, CFG, DEV, gap_slots=gaps)
        .to_pandas()
        .set_index("slot")
    )


def test_every_scheduled_slot_has_a_row():
    b = bars([trade(1, 1, 500.0, 100, (" ",))])
    assert len(b) == 390 and b.index[0] == 1 and b.index[-1] == 390
    assert b.loc[2, "interval_status"] == KNOWN_NO_ELIGIBLE_TRADES and pd.isna(b.loc[2, "open"])


def test_ohlc_uses_last_eligible_set_only():
    b = bars(
        [
            trade(1, 1, 500.00, 100, (" ",)),
            trade(1, 2, 499.00, 10, (" ", "I")),  # odd lot: volume only
            trade(1, 3, 503.00, 100, (" ", "4")),  # NOTE_2, but a last already exists: excluded
            trade(1, 4, 500.50, 100, (" ", "F")),
        ]
    )
    r = b.loc[1]
    assert (r["open"], r["high"], r["low"], r["close"]) == (500.00, 500.50, 500.00, 500.50)
    assert (r["volume"], r["trade_count"]) == (310, 4)
    assert r["vwap_volume"] == 200  # E-4(a): only the two last-eligible trades


def test_note2_qualifies_when_first_and_then_establishes_the_last():
    b = bars(
        [
            trade(1, 1, 501.00, 100, (" ", "Z")),  # no qualifying last yet: qualifies
            trade(1, 2, 502.00, 100, (" ", "Z")),  # the first Z established the last: excluded
            trade(1, 3, 500.00, 100, (" ",)),
        ]
    )
    r = b.loc[1]
    assert (r["open"], r["high"], r["close"]) == (501.00, 501.00, 500.00)


def test_note3_sold_last_from_listing_market_or_same_participant():
    b = bars(
        [
            trade(1, 1, 500.00, 100, (" ",), exch="Z"),
            trade(
                1, 2, 505.00, 100, (" ", "L"), exch="K"
            ),  # other participant, not listing: excluded
            trade(1, 3, 501.00, 100, (" ", "L"), exch="P"),  # listing market: qualifies
            trade(1, 4, 502.00, 100, (" ", "L"), exch="P"),  # same participant as last: qualifies
        ]
    )
    r = b.loc[1]
    assert (r["high"], r["close"]) == (502.00, 502.00)


def test_multi_note_requires_every_note():
    # 4 (NOTE_2) with L (NOTE_3): after a last exists, NOTE_2 fails, so the trade is excluded
    b = bars([trade(1, 1, 500.0, 100, (" ",)), trade(1, 2, 509.0, 100, (" ", "4", "L"), exch="P")])
    assert b.loc[1, "high"] == 500.0


def test_informational_updates_sip_state_but_not_bars():
    # '9' is consolidated-last YES: it establishes the last, so a later Z can't qualify
    b = bars(
        [
            trade(1, 1, 499.0, 0, (" ", "9")),
            trade(1, 2, 501.0, 100, (" ", "Z")),
            trade(1, 3, 500.0, 100, (" ",)),
        ]
    )
    r = b.loc[1]
    assert (r["open"], r["high"], r["volume"]) == (500.0, 500.0, 200)  # Z is volume-eligible


def test_volume_only_slot():
    b = bars([trade(1, 1, 500.0, 100, (" ",)), trade(2, 5, 500.1, 30, (" ", "I"))])
    r = b.loc[2]
    assert r["interval_status"] == KNOWN_NO_ELIGIBLE_TRADES
    assert pd.isna(r["open"]) and r["volume"] == 30
    assert r["session_vwap"] == pytest.approx(500.0)  # unchanged: the odd lot isn't in VWAP


def test_unknown_condition_quarantines_and_makes_vwap_uncertain():
    b = bars(
        [
            trade(1, 1, 500.0, 100, (" ",)),
            trade(3, 1, 500.2, 100, (" ", "Y")),
            trade(4, 1, 500.3, 100, (" ",)),
        ]
    )
    assert b.loc[3, "interval_status"] == DATA_MISSING_OR_UNCERTAIN
    assert b.loc[3, "quarantined_trades"] == 1 and b.loc[3, "volume"] == 0
    assert (
        not b.loc[2, "vwap_uncertain"]
        and b.loc[3, "vwap_uncertain"]
        and b.loc[390, "vwap_uncertain"]
    )
    assert b.loc[4, "interval_status"] == PRESENT


def test_download_gap_slot_is_uncertain():
    b = bars([trade(1, 1, 500.0, 100, (" ",))], gaps=frozenset({10}))
    assert b.loc[10, "interval_status"] == DATA_MISSING_OR_UNCERTAIN and b.loc[10, "vwap_uncertain"]


def test_vwap_accumulators_and_separate_populations():
    b = bars(
        [
            trade(1, 1, 500.0, 100, (" ",)),
            trade(1, 2, 510.0, 100, (" ",)),
            trade(2, 1, 520.0, 200, (" ", "F")),
            trade(2, 2, 400.0, 50, (" ", "I")),
        ]
    )
    assert b.loc[2, "cum_vwap_notional"] == pytest.approx(500 * 100 + 510 * 100 + 520 * 200)
    assert b.loc[2, "cum_vwap_volume"] == 400
    assert b.loc[2, "cum_total_volume"] == 450  # includes the odd lot
    assert b.loc[2, "session_vwap"] == pytest.approx((50000 + 51000 + 104000) / 400)


def test_open_close_use_timestamp_then_stored_order():
    b = bars([trade(1, 1, 500.0, 100, (" ",)), trade(1, 1, 501.0, 100, (" ",))])  # same timestamp
    assert (b.loc[1, "open"], b.loc[1, "close"]) == (500.0, 501.0)


def test_development_assumption_flags_reach_the_bar():
    day = session_from_exchange_times(date(2024, 2, 5), time(9, 30), time(16, 0))
    rows = [
        {
            "ts": pd.Timestamp(day.open_utc + timedelta(seconds=1)),
            "price": 500.0,
            "size": 100,
            "exchange": "P",
            "conditions": [" ", "O"],
            "trade_id": None,
            "tape": "B",
        }
    ]
    b = build_session_bars(pd.DataFrame(rows), day, CFG, DEV).to_pandas().set_index("slot")
    assert b.loc[1, "open"] == 500.0 and "rule_backfilled:O" in list(b.loc[1, "flags"])
    held_out = (
        build_session_bars(pd.DataFrame(rows), day, CFG, Usage.HOLDOUT)
        .to_pandas()
        .set_index("slot")
    )
    assert held_out.loc[1, "interval_status"] == DATA_MISSING_OR_UNCERTAIN


# ---- pre-market (E-6) ----------------------------------------------------------------------


def pm(sec: float, price: float, size: int, c: tuple[str, ...]):
    ts = DAY.open_utc - timedelta(hours=1) + timedelta(seconds=sec)
    return {
        "ts": pd.Timestamp(ts),
        "price": price,
        "size": size,
        "exchange": "P",
        "conditions": list(c),
        "trade_id": None,
        "tape": "B",
    }


def test_premarket_t_is_the_marker_and_odd_lots_excluded():
    lv = premarket_levels(
        pd.DataFrame(
            [
                pm(1, 498.0, 100, (" ", "T")),
                pm(2, 497.0, 100, (" ", "F", "T")),
                pm(3, 490.0, 5, (" ", "T", "I")),
                pm(4, 506.0, 100, (" ", "U")),  # odd lot; U isn't the marker
            ]
        ),
        DAY,
        CFG,
        DEV,
    )
    assert (lv.high, lv.low, lv.eligible_trades) == (498.0, 497.0, 2)


def test_premarket_state_is_separate_from_regular_sip_state():
    # A pre-market 'qualifying last' must not stop the first regular-session Z from qualifying
    premarket_levels(pd.DataFrame([pm(1, 498.0, 100, (" ", "T"))]), DAY, CFG, DEV)
    b = bars([trade(1, 1, 501.0, 100, (" ", "Z")), trade(1, 2, 500.0, 100, (" ",))])
    assert b.loc[1, "open"] == 501.0


def test_premarket_unknown_condition_makes_levels_uncertain():
    lv = premarket_levels(
        pd.DataFrame([pm(1, 498.0, 100, (" ", "T")), pm(2, 499.0, 100, (" ", "Y"))]), DAY, CFG, DEV
    )
    assert lv.high is None and "premarket_uncertain" in lv.flags
