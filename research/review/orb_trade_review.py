# ruff: noqa
"""ORB trade-by-trade review (access log #28). EXPLORATORY: nothing here is evidence or a registered
test; every number comes from trades it would later be judged on. Reads Design (2024-02-01 to
2024-04-30) and the live shadow ORB trades only.

Declared before running: (A) one row per Design ORB position; (B) hindsight ceilings (mean R of the
best possible exit, of holding to the close); (C) one-at-a-time sweeps of ORB parameters, 35 variants
in all, reported as mean R, mean underlying points, trades and win share against v1; (D) entry
splits by range width, volume ratio, entry time and direction; (E) the live ORB trades with the
option-level exit rules of access log #25. Changes nothing."""

import glob, json, sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "research" / "exits"))
from exit_review import option_rules  # noqa: E402

from tournament.features.pipeline import run_sessions
from tournament.marketdata.eligibility import Usage
from tournament.marketdata.store import HistoryStore
from tournament.replay.harness import ReplayRun, dataset_id_for
from tournament.strategies.orb import ORB

pd.set_option("display.width", 250)
pd.set_option("display.max_columns", 40)
pd.set_option("display.max_rows", 500)
ET = "America/New_York"
START, END = date(2024, 2, 1), date(2024, 4, 30)
V1 = yaml.safe_load((ROOT / "config/strategies/orb-v1.yaml").read_text())
store = HistoryStore(ROOT / "data" / "history")
ds = dataset_id_for(store)
outs = list(run_sessions(store, Usage.DEVELOPMENT, START, END))
assert max(str(o.day.session_date) for o in outs) <= "2024-04-30"
BARS = {o.session_id: o.bars.reset_index(drop=True) for o in outs}
DAY = {o.session_id: str(o.day.session_date) for o in outs}


def worker(**changes):
    cfg = json.loads(json.dumps(V1))
    cfg["params"].update(changes)
    return ORB(config=cfg)


def run(w):
    r = ReplayRun([w], Usage.DEVELOPMENT, dataset_id=ds).run_outputs(outs)
    p = r.reference_positions
    p = p[p.reference_exit_price.notna()].copy()
    sign = np.where(p.underlying_direction == "bullish", 1.0, -1.0)
    p["sign"] = sign
    p["pts"] = sign * (p.reference_exit_price.astype(float) - p.reference_entry_price.astype(float))
    p["R"] = p.pts / p.r_u.astype(float)
    return p, r.candidates


# ------------------------------------------------------------------ (A) per-trade table
base, cands = run(worker())
feat = cands.set_index("candidate_id").features.map(json.loads)
rows = []
for _, r in base.iterrows():
    b = BARS[r.session_id]
    s, E, ru = r.sign, float(r.reference_entry_price), float(r.r_u)
    held = b[(b.slot > r.entry_slot) & (b.slot <= r.exit_slot)]
    after = b[b.slot > r.exit_slot]
    hi = lambda d: (s * (d.high if s > 0 else d.low) - s * E) / ru if len(d) else np.nan
    lo = lambda d: (s * (d.low if s > 0 else d.high) - s * E) / ru if len(d) else np.nan
    f = feat.get(r.candidate_id, {})
    tgt = json.loads(r.target_policy)["price"]
    mfe, mae = (hi(held).max(), lo(held).min()) if len(held) else (np.nan, np.nan)
    post_best = hi(after).max() if len(after) else np.nan
    close_R = (s * (float(b.close.iloc[-1]) - E)) / ru
    after_tgt = bool(len(after) and ((s * (after.high if s > 0 else after.low)) >= s * abs(tgt)).any())
    rows.append(
        {
            "day": DAY[r.session_id][5:],
            "dir": r.underlying_direction[:4],
            "in_slot": int(r.entry_slot),
            "held": int(r.exit_slot - r.entry_slot),
            "w_a": round(f.get("w_a", np.nan), 1),
            "rvol": round(f.get("rvol", np.nan), 1),
            "exit": r.exit_primary_reason[:6],
            "R": round(r.R, 2),
            "mfe_R": round(mfe, 2),
            "mae_R": round(mae, 2),
            "post_best_R": round(post_best, 2),
            "close_R": round(close_R, 2),
            "tgt_later": after_tgt,
        }
    )
T = pd.DataFrame(rows)
print(f"(A) ORB DESIGN POSITIONS: {len(T)} in {len(outs)} sessions (R on the strategy's own risk r_u)")
print(T.to_string(index=False))

# ------------------------------------------------------------------ (B) ceilings
print("\n(B) HINDSIGHT CEILINGS (not attainable; they bound what exit timing could add)")
print(f"  as specified      mean R {T.R.mean():+.3f}  win share {(T.R > 0).mean():.2f}")
print(f"  best bar high/low mean R {T.mfe_R.mean():+.3f}  (exit exactly at the peak every time)")
print(f"  hold to the close mean R {T.close_R.mean():+.3f}")
print("  by exit reason:")
print(T.groupby("exit").agg(n=("R", "size"), meanR=("R", "mean"), mfe=("mfe_R", "mean"), mae=("mae_R", "mean"), post=("post_best_R", "mean"), close=("close_R", "mean"), tgt_later=("tgt_later", "mean")).round(2).to_string())

# ------------------------------------------------------------------ (C) sweeps
GRID = {
    "tgt_mult": [0.75, 1.5, 2.0],
    "inv_frac": [0.15, 0.4, 0.5],
    "ft_bars": [5, 15, 20, None],
    "ft_r": [0.25, 0.75, 1.0],
    "max_hold": [30, 90, 120],
    "trail_activate_r": [0.5, 1.5, 2.0],
    "stop_buffer": [0.1, 0.5],
    "brk_buffer": [0.0, 0.2, 0.3],
    "rvol_min": [1.0, 2.0, 2.5],
    "w_min": [2.0, 4.0],
    "w_max": [6.0, 8.0, 15.0],
    "last_entry_slot": [60, 90, 180],
}
n_var = sum(len(v) for v in GRID.values())
print(f"\n(C) ONE-AT-A-TIME SWEEPS ({n_var} variants; v1: n={len(base)}, mean R {base.R.mean():+.3f}, mean pts {base.pts.mean():+.3f}, win {(base.R > 0).mean():.2f})")
out = []
for k, vals in GRID.items():
    for v in vals:
        p, _ = run(worker(**{k: v}))
        out.append({"param": k, "value": v, "n": len(p), "mean_R": round(p.R.mean(), 3), "mean_pts": round(p.pts.mean(), 3), "total_pts": round(p.pts.sum(), 1), "win": round((p.R > 0).mean(), 2)})
print(pd.DataFrame(out).to_string(index=False))

# ------------------------------------------------------------------ (D) entry splits
print("\n(D) ENTRY SPLITS on the v1 positions (counts shown; small groups are noise)")
def split(col, bins=None):
    x = T.copy()
    x["g"] = pd.cut(x[col], bins) if bins is not None else x[col]
    print(x.groupby("g", observed=True).agg(n=("R", "size"), meanR=("R", "mean"), win=("R", lambda s: (s > 0).mean())).round(2).to_string())
for col, bins in (("dir", None), ("w_a", [0, 4, 5, 6, 8, 11]), ("rvol", [0, 2, 3, 5, 100]), ("in_slot", [15, 30, 60, 90, 120])):
    print(f"-- by {col}")
    split(col, bins)

# ------------------------------------------------------------------ (E) live ORB trades
print("\n(E) LIVE SHADOW ORB TRADES, option path from one-second bids")
live, sims = [], []
for day_dir in sorted(glob.glob(str(ROOT / "data/live/2026-*"))):
    d = Path(day_dir) / "shadow"
    f = d / "ledger" / "acct_orb.json"
    if not f.exists():
        continue
    q = pd.read_parquet(d / "option_quotes_1s.parquet", columns=["second", "symbol", "bid"])
    q["second"] = pd.to_datetime(q["second"], utc=True)
    q = q[q.bid > 0]
    b = json.load(open(f))
    for t in b["trades"]:
        if t["broker_exit"] is None:
            continue
        e_at, x_at = pd.Timestamp(t["entry_at"]), pd.Timestamp(t["exit_at"])
        s = q[(q.symbol == t["symbol"]) & (q.second >= e_at.floor("s")) & (q.second <= x_at)].set_index("second").bid
        if s.empty:
            continue
        en, ex = t["broker_entry"], t["broker_exit"]
        live.append({"day": Path(day_dir).name[5:], "entry_et": e_at.tz_convert(ET).strftime("%H:%M"), "qty": t["qty"], "in": en, "out": ex, "pnl$": round(t["broker_gross_usd"]), "exit": ",".join(r.replace("REF_", "") for r in t["exit_reasons"]), "peak%": round((s.max() / en - 1) * 100, 1), "peak@s": int((s.idxmax() - e_at).total_seconds()), "low%": round((s.min() / en - 1) * 100, 1)})
        sims.append(option_rules(s, en, ex, t["qty"]))
L, S = pd.DataFrame(live), pd.DataFrame(sims)
print(L.to_string(index=False))
if len(S):
    print(f"\nlive ORB option rules, dollars over {len(S)} trades:")
    print(pd.DataFrame({"total $": S.sum().round(0), "trades changed": {c: int((S[c].round(2) != S["O1 as executed"].round(2)).sum()) for c in S}}).to_string())
