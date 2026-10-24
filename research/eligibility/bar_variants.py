# ruff: noqa
"""D-04 (E) diagnostic: build regular-session minute bars from SPY trades under the draft
matrix and competing adapter variants, and compare field by field with Alpaca's own bars.

Compares two bar constructions (integrity). Strategy behaviour is not examined.
"""

import csv
import json
from datetime import date

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from tournament.marketdata.store import HistoryStore

MATRIX = "config/trade_eligibility/cts_sale_conditions.csv"
INFORMATIONAL = {"M", "Q", "9"}
VWAP_ALLOWED = {"SPACE", "E", "F", "I", "K", "L", "O", "X", "Z", "5", "6"}
LISTING_MARKET = "P"  # NYSE Arca


def load_matrix(day):
    rows = {}
    for r in csv.DictReader(open(MATRIX)):
        lo = date.fromisoformat(r["effective_from"])
        hi = date.fromisoformat(r["effective_to"]) if r["effective_to"] else date.max
        if lo <= day < hi:  # effective_from inclusive, effective_to exclusive
            rows[r["condition_code"]] = r
    return rows


def codes_of(conds):
    out = []
    for c in conds:
        out.append("SPACE" if c == " " else c)
    return out


def combine(rules):
    """CTS p83: any NO wins; all YES -> YES; YES plus notes -> the notes decide."""
    if any(r in ("NO",) for r in rules):
        return "NO"
    if any(r in ("UNVERIFIED", "TBD") for r in rules):
        return "UNKNOWN"
    notes = sorted({r for r in rules if r.startswith("NOTE")})
    return "|".join(notes) if notes else "YES"


def classify(df, day, matrix, unverified_policy):
    keys = df["conditions"].map(lambda c: "|".join(codes_of(c)))
    flags = {}
    for k in keys.unique():
        codes = k.split("|")
        rows = []
        for c in codes:
            r = matrix.get(c)
            if r is None:
                rows = None
                break
            if r["consolidated_last_rule"] == "UNVERIFIED" and unverified_policy == "current":
                # E-5 proposal: use the earliest later row for this code, flagged
                later = [
                    x
                    for x in csv.DictReader(open(MATRIX))
                    if x["condition_code"] == c and x["effective_from"] > str(day)
                ]
                r = later[0] if later else r
            rows.append(r)
        if rows is None:
            flags[k] = dict(last="UNKNOWN", hl="UNKNOWN", vol="UNKNOWN", vwap=False, info=False)
            continue
        flags[k] = dict(
            last=combine([r["consolidated_last_rule"] for r in rows]),
            hl=combine([r["consolidated_high_low_rule"] for r in rows]),
            vol=combine([r["volume_rule"] for r in rows]),
            vwap=all(c in VWAP_ALLOWED for c in codes),
            info=any(c in INFORMATIONAL for c in codes),
        )
    f = pd.DataFrame([flags[k] for k in keys], index=df.index)
    return pd.concat([df, f], axis=1)


def resolve_sip_last(df):
    """Stage 1: resolved SIP consolidated-last eligibility, with state updated from RESOLVED
    eligibility (a qualifying NOTE_2/NOTE_3 trade also establishes the last). Evaluation order is
    the stored order: timestamp, then provider order within a timestamp. That's a deterministic
    approximation, not reconstructed CTS dissemination order. Informational records are NOT
    excluded here, so they still update SIP state; the application exclusion is stage 2."""
    rules = df["last"].to_numpy()
    exch = df["exchange"].to_numpy()
    out = np.zeros(len(df), dtype=bool)
    has_last, last_exch = False, None
    for i in np.flatnonzero((rules != "NO") & (rules != "UNKNOWN")):
        r = rules[i]
        if r == "YES":
            q = True
        else:  # one or more notes; all present notes must pass (stated assumption)
            q = True
            if "NOTE_2" in r:
                q &= not has_last
            if "NOTE_3" in r:
                q &= (not has_last) or exch[i] == last_exch or exch[i] == LISTING_MARKET
        if q:
            out[i] = True
            has_last, last_exch = True, exch[i]
    return out


def resolve_last_old(df):
    """The v1-draft rule, kept to measure the effect of the fix: state from literal YES only."""
    definite = (df["last"] == "YES").to_numpy()
    prior_definite = np.concatenate([[0], np.cumsum(definite)[:-1]]) > 0
    note2 = df["last"].str.contains("NOTE_2", regex=False).to_numpy()
    note3 = df["last"].str.contains("NOTE_3", regex=False).to_numpy()
    elig = definite | (note2 & ~prior_definite)
    last_exch = pd.Series(np.where(definite, df["exchange"], None)).ffill().shift(1).to_numpy()
    elig |= note3 & (
        ~prior_definite
        | (df["exchange"].to_numpy() == last_exch)
        | (df["exchange"].to_numpy() == LISTING_MARKET)
    )
    return elig & ~df["info"].to_numpy()


def resolve_last(df):
    """Stage 2: application inclusion for bar open/close (E-7 removes informational records)."""
    return resolve_sip_last(df) & ~df["info"].to_numpy()


def build(df, hl_variant, vwap_variant):
    df = df.copy()
    df["minute"] = df["ts"].dt.floor("min")
    df["el_last"] = resolve_last(df)
    if hl_variant == "sip":
        df["el_hl"] = df["hl"].isin(["YES"]) | df["hl"].str.startswith("NOTE")
        df["el_hl"] &= ~df["info"]
    else:  # "last": high/low only from trades eligible for the last
        df["el_hl"] = df["el_last"]
    df["el_vol"] = (df["vol"] == "YES") & ~df["info"]
    df["el_vwap"] = df["vwap"] if vwap_variant == "policy" else df["el_vol"]
    g = df.groupby("minute")
    lastp = df[df["el_last"]].groupby("minute")["price"]
    hlp = df[df["el_hl"]].groupby("minute")["price"]
    vol = df[df["el_vol"]].groupby("minute")["size"]
    vw = df[df["el_vwap"]].assign(n=lambda x: x["price"] * x["size"]).groupby("minute")
    out = pd.DataFrame(
        {
            "open": lastp.first(),
            "close": lastp.last(),
            "high": hlp.max(),
            "low": hlp.min(),
            "volume": vol.sum(),
            "trade_count": vol.count(),
            "vwap": vw["n"].sum() / vw["size"].sum(),
        }
    )
    return out


s = HistoryStore("data/history")
days = s.days("spy_trades")
dev = [d for d in days if date(2024, 2, 1) <= d <= date(2024, 12, 31)]
post = [d for d in days if d >= date(2025, 11, 3)]
sample = sorted(set(dev[:: max(1, len(dev) // 15)][:15] + post[:: max(1, len(post) // 5)][:5]))

variants = {"HL=sip": ("sip", "volume"), "HL=last": ("last", "volume")}
fields = ["open", "high", "low", "close", "volume", "trade_count", "vwap"]
totals = {v: {f: [0, 0] for f in fields} for v in [*variants, "VWAP=policy"]}
unknown = 0
for d in sample:
    t = pq.read_table(s.path("spy_trades", d)).to_pandas()
    alp = s.read("spy_bars_1m", d).to_pandas()
    alp = alp[alp["regular_session"]].set_index("ts")
    df = classify(t, d, load_matrix(d), "current")
    unknown += int((df["last"] == "UNKNOWN").sum())
    for name, (hl, vwv) in {**variants, "VWAP=policy": ("last", "policy")}.items():
        ours = build(df, hl, vwv).reindex(alp.index)
        for f in fields:
            if name == "VWAP=policy" and f != "vwap":
                continue
            a, o = alp[f].astype(float), ours[f].astype(float)
            ok = np.isclose(a, o, rtol=0, atol=1e-6 if f != "vwap" else 5e-4) & o.notna()
            totals[name][f][0] += int(ok.sum())
            totals[name][f][1] += len(a)
report = {
    v: {f: f"{m}/{n} = {m / n:.4%}" for f, (m, n) in fs.items() if n} for v, fs in totals.items()
}
print(
    json.dumps(
        {
            "sessions": [str(d) for d in sample],
            "unknown_condition_trades": unknown,
            "match_vs_alpaca": report,
        },
        indent=1,
    )
)
