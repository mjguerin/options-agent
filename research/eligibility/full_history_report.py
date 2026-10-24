# ruff: noqa
"""D-04 checklist item 8: full-history report. The PRODUCTION bar builder (Usage.DEVELOPMENT)
over every session, compared field by field with the provider's bars. Integrity only.

Agreement: |ours - provider| <= 1e-6 for prices, volume and count; <= 5e-6 for minute VWAP.
Every discrepancy gets a category and an evidence status:
  opening_minute_T_print            slot 1 open/high/low equals a T-conditioned print in that minute
                                    (observed; cause not established)
  likely_provider_finalization_difference
                                    our volume/count higher than the provider's (hypothesized)
  unexplained                       anything else
Writes data/reports/d04_full_history.json and prints a summary.
"""

import collections, json, time
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from tournament.marketdata.bars import PRESENT, build_session_bars, premarket_levels
from tournament.marketdata.calendar import SessionDay
from tournament.marketdata.eligibility import EligibilityConfig, Usage
from tournament.marketdata.store import HistoryStore

ROOT = Path(__file__).resolve().parents[2]
s = HistoryStore(ROOT / "data" / "history")
cfg = EligibilityConfig()
cal = pq.read_table(s.root / "calendar.parquet").to_pandas()
FIELDS = ["open", "high", "low", "close", "vwap", "volume", "trade_count"]
TOL = {"vwap": 5e-6}

agree = {f: [0, 0] for f in FIELDS}
cats = collections.Counter()
unexplained_examples = []
status_counts, flag_counts = collections.Counter(), collections.Counter()
quarantined, uncertain_sessions = 0, []
coverage = collections.Counter()
pm = {"levels": 0, "uncertain": 0, "none": 0, "pmh_equal_provider": 0, "pml_equal_provider": 0}
t0 = time.time()
for r in cal.itertuples():
    d = r.session_date if isinstance(r.session_date, date) else r.session_date.date()
    if not s.has("spy_trades", d):
        continue
    day = SessionDay(
        d, pd.Timestamp(r.open_utc).to_pydatetime(), pd.Timestamp(r.close_utc).to_pydatetime()
    )
    trades = pq.read_table(s.path("spy_trades", d)).to_pandas()
    ours = (
        build_session_bars(trades, day, cfg, Usage.DEVELOPMENT).to_pandas().set_index("bar_start")
    )
    status_counts.update(ours["interval_status"].tolist())
    for fl in ours["flags"]:
        flag_counts.update(fl)
    quarantined += int(ours["quarantined_trades"].sum())
    if ours["vwap_uncertain"].any():
        uncertain_sessions.append(str(d))
    ours["vwap"] = ours["vwap_notional"] / ours["vwap_volume"].replace(0, np.nan)

    bars = s.read("spy_bars_1m", d).to_pandas()
    prov = bars[bars["regular_session"]].set_index("ts")
    present = ours[ours["interval_status"] == PRESENT]
    both = present.index.intersection(prov.index)
    coverage["present_slots"] += len(present)
    coverage["present_with_provider_bar"] += len(both)
    coverage["present_without_provider_bar"] += len(present.index.difference(prov.index))
    coverage["provider_bar_where_we_have_no_price"] += len(prov.index.difference(present.index))
    first = ours.index[0]
    t_prints = None
    # minutes where our volume is higher than the provider's: an extra final-tape trade there
    # can also move that minute's prices and VWAP, so the whole minute takes that category
    extra = set(
        both[(present.loc[both, "volume"].to_numpy() > prov.loc[both, "volume"].to_numpy())]
    )
    for f in FIELDS:
        a = prov.loc[both, f].astype(float)
        o = present.loc[both, f].astype(float)
        ok = np.isclose(a, o, rtol=0, atol=TOL.get(f, 1e-6))
        agree[f][0] += int(ok.sum())
        agree[f][1] += len(ok)
        for m in both[~ok]:
            if m in extra:
                cat = "likely_provider_finalization_difference"
            elif f in ("open", "high", "low", "vwap") and m == first:
                if t_prints is None:
                    mm = trades[(trades["ts"] >= m) & (trades["ts"] < m + pd.Timedelta(minutes=1))]
                    t_prints = set(
                        mm.loc[mm["conditions"].map(lambda c: "T" in c), "price"].round(6)
                    )
                if f == "vwap":
                    # reproduce: our last-eligible trades plus round-lot T prints whose other
                    # conditions pass the last rule (odd lots excluded)
                    mm = trades[(trades["ts"] >= m) & (trades["ts"] < m + pd.Timedelta(minutes=1))]
                    base = mm["conditions"].map(lambda c: tuple(x for x in c if x != "T"))
                    keep = base.map(lambda k: cfg.classify(k, d, Usage.DEVELOPMENT).last == "YES")
                    alt = (mm.loc[keep, "price"] * mm.loc[keep, "size"]).sum() / mm.loc[
                        keep, "size"
                    ].sum()
                    cat = (
                        "opening_minute_T_print"
                        if abs(alt - float(a[m])) <= 5e-6
                        else "unexplained"
                    )
                else:
                    cat = (
                        "opening_minute_T_print"
                        if round(float(a[m]), 6) in t_prints
                        else "unexplained"
                    )
            else:
                cat = "unexplained"
            cats[(f, cat)] += 1
            if cat == "unexplained" and len(unexplained_examples) < 60:
                unexplained_examples.append(
                    {
                        "session": str(d),
                        "minute": str(m),
                        "field": f,
                        "provider": float(a[m]),
                        "ours": float(o[m]),
                    }
                )
    if s.has("spy_trades_premarket", d):
        lv = premarket_levels(
            pq.read_table(s.path("spy_trades_premarket", d)), day, cfg, Usage.DEVELOPMENT
        )
        if lv.high is None:
            pm["uncertain" if "premarket_uncertain" in lv.flags else "none"] += 1
        else:
            pm["levels"] += 1
            start = (
                pd.Timestamp(d).tz_localize("America/New_York") + pd.Timedelta(hours=4)
            ).tz_convert("UTC")
            pb = bars[(bars["ts"] >= start) & (bars["ts"] < pd.Timestamp(r.open_utc))]
            pm["pmh_equal_provider"] += int(np.isclose(pb["high"].max(), lv.high, atol=1e-6))
            pm["pml_equal_provider"] += int(np.isclose(pb["low"].min(), lv.low, atol=1e-6))

report = {
    "sessions": len(cal),
    "usage": "development",
    "elapsed_s": round(time.time() - t0, 1),
    "agreement": {f: f"{a}/{n} = {a / n:.4%}" for f, (a, n) in agree.items()},
    "discrepancies": {f"{f}: {c}": n for (f, c), n in sorted(cats.items())},
    "unexplained_examples": unexplained_examples,
    "interval_status_counts": dict(status_counts),
    "slot_coverage": dict(coverage),
    "quarantined_trades": quarantined,
    "sessions_with_vwap_uncertain": uncertain_sessions,
    "assumption_flag_trade_slots": dict(flag_counts),
    "premarket_levels": pm,
}
out = ROOT / "data" / "reports"
out.mkdir(parents=True, exist_ok=True)
(out / "d04_full_history.json").write_text(json.dumps(report, indent=1))
print(json.dumps({k: v for k, v in report.items() if k != "unexplained_examples"}, indent=1))
print("unexplained examples:", json.dumps(unexplained_examples[:10], indent=1))
