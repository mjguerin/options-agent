"""Descriptive SPY scales behind the v0.1/v0.2 spec defaults (specs/README.md).

Reproduce on the mini:  .venv/bin/python research/scale_stats.py
First run: Oct 23 2026, history store dataset stamped eed63f9 (data/history).

Population and method, exactly as computed (descriptive only, not SD-05 features):
- Instrument SPY; source: Alpaca SIP 1-minute bars as stored in spy_bars_1m (Alpaca's own bars,
  not our SD-03 bars); calendar: Alpaca calendar; sessions 2024-02-01 .. 2024-04-30 whose
  regular-session bar count is >= 300 (62 sessions; none excluded in that window).
- TR per bar; first bar's TR is NaN (no prior close), so it never enters the mean.
- atr1m: per session, the median over bars 31+ of a 14-bar SIMPLE moving average of TR
  (not Wilder, not session-reset). Day-level summaries are then taken across sessions.
- VWAP: cumulative sum(bar vwap * volume) / cumulative volume over regular-session bars.
- day_range_atr: (session high - session low) / that session's atr1m.
- orN_width_atr: width of the first N regular bars / that session's atr1m (a full-day value,
  so it uses information from after the OR; descriptive only).
- max_abs_vwap_dist_atr / p90_abs_vwap_dist_atr: per session, max and 90th percentile of
  |close - VWAP| / rolling ATR over bars 16+; then cross-session quantiles.
- vwap_crosses: count of bars 16+ where sign(close - VWAP) changes, zeros counted as a change.
- Cross-session quantiles: pandas describe (linear interpolation).
These full-session summaries must never feed intraday decisions.
"""

from datetime import date

import numpy as np
import pandas as pd

from tournament.marketdata.store import HistoryStore

s = HistoryStore("data/history")
days = [d for d in s.days("spy_bars_1m") if date(2024, 2, 1) <= d <= date(2024, 4, 30)]
rows = []
for d in days:
    b = s.read("spy_bars_1m", d).to_pandas()
    r = b[b.regular_session].reset_index(drop=True)
    if len(r) < 300:
        continue
    prev_close = r.close.shift(1)
    tr = np.maximum(
        r.high - r.low, np.maximum((r.high - prev_close).abs(), (r.low - prev_close).abs())
    )
    atr = tr.rolling(14).mean()
    atr_mid = atr.iloc[30:].median()  # typical 1-min ATR after the open settles
    cum_pv = (r.vwap * r.volume).cumsum()
    vwap = cum_pv / r.volume.cumsum()
    dist = (r.close - vwap) / atr
    rec = {
        "date": d,
        "price": r.open[0],
        "atr1m": atr_mid,
        "atr1m_pct": atr_mid / r.open[0] * 100,
        "day_range_atr": (r.high.max() - r.low.min()) / atr_mid,
        "max_abs_vwap_dist_atr": dist.iloc[15:].abs().max(),
        "p90_abs_vwap_dist_atr": dist.iloc[15:].abs().quantile(0.9),
        "vwap_crosses": int((np.sign(r.close - vwap).diff().abs() > 0).iloc[15:].sum()),
    }
    for m in (5, 15, 30):
        orr = r.iloc[:m]
        width = orr.high.max() - orr.low.min()
        rest = r.iloc[m:]
        rec[f"or{m}_width_atr"] = width / atr_mid
        rec[f"or{m}_broken_up"] = bool((rest.high > orr.high.max()).any())
        rec[f"or{m}_broken_dn"] = bool((rest.low < orr.low.min()).any())
    rows.append(rec)

df = pd.DataFrame(rows)
print(f"sessions: {len(df)}  ({df.date.min()} .. {df.date.max()})")
pd.set_option("display.width", 160)
cols = [c for c in df.columns if c != "date"]
print(df[cols].describe(percentiles=[0.1, 0.5, 0.9]).T[["10%", "50%", "90%"]].round(3).to_string())
