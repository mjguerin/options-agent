# ruff: noqa
"""Exit review (access log #25). Rules were declared in the log before this ran:
options (live trades, one-second bid path): O1 as executed; O2 breakeven stop once +5/+10/+15%;
O3 sell half at +10/+20%; O4 trailing stop 10% below the peak once +10%.
R (replay positions, closes): R1 as specified; R2/R3 sell half at +0.5R/+1R; R4/R5 the same with
the rest stopped at entry. Nothing else is tried. Prints per-trade rows for the 13 live trades
and AGGREGATES for the replay. Changes nothing."""

import glob, json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
ET = "America/New_York"
pd.set_option("display.width", 250)
pd.set_option("display.max_columns", 40)


# ------------------------------------------------------------------ live trades, option path
def option_rules(path: pd.Series, entry: float, exit_px: float, qty: int):
    """path: bid by second inside the hold window. Returns {rule: pnl_usd}."""
    m = 100 * qty
    base = (exit_px - entry) * m
    out = {"O1 as executed": base}
    bids = path.values
    for L in (0.05, 0.10, 0.15):
        pnl, locked = base, False
        for b in bids:
            if not locked and b >= entry * (1 + L):
                locked = True
            elif locked and b <= entry:
                pnl = (b - entry) * m
                break
        out[f"O2 breakeven after +{int(L * 100)}%"] = pnl
    for L in (0.10, 0.20):
        hit = next((b for b in bids if b >= entry * (1 + L)), None)
        out[f"O3 half at +{int(L * 100)}%"] = (
            base if hit is None else 0.5 * m * (hit - entry) + 0.5 * m * (exit_px - entry)
        )
    pnl, peak, active = base, 0.0, False
    for b in bids:
        peak = max(peak, b)
        if not active and b >= entry * 1.10:
            active = True
        if active and b <= peak * 0.90:
            pnl = (b - entry) * m
            break
    out["O4 trail 10% after +10%"] = pnl
    return out


def live_review():
    rows, sims = [], []
    for day_dir in sorted(
        glob.glob(str(ROOT / "data/live/2026-10-2[789]"))
        + glob.glob(str(ROOT / "data/live/2026-10-30"))
    ):
        d = Path(day_dir) / "shadow"
        q = pd.read_parquet(d / "option_quotes_1s.parquet", columns=["second", "symbol", "bid"])
        q["second"] = pd.to_datetime(q["second"], utc=True)
        q = q[q.bid > 0]
        refs = pd.read_parquet(glob.glob(str(d / "records/*/reference_positions.parquet"))[0])
        bars = pd.read_parquet(d / "bars.parquet")
        paths = json.load(open(d / "ledger/paths.json"))
        for f in sorted(glob.glob(str(d / "ledger/acct_*.json"))):
            b = json.load(open(f))
            for t in b["trades"]:
                if t["broker_exit"] is None:
                    continue
                e_at, x_at = pd.Timestamp(t["entry_at"]), pd.Timestamp(t["exit_at"])
                s = (
                    q[
                        (q.symbol == t["symbol"])
                        & (q.second >= e_at.floor("s"))
                        & (q.second <= x_at)
                    ]
                    .set_index("second")
                    .bid
                )
                en, ex = t["broker_entry"], t["broker_exit"]
                ref = refs[refs.candidate_id == t["candidate_id"]].iloc[0]
                sign = 1 if ref.underlying_direction == "bullish" else -1
                held = bars[(bars.slot > int(ref.entry_slot)) & (bars.slot <= int(ref.exit_slot))]
                best = held.high.max() if sign > 0 else held.low.min()
                worst = held.low.min() if sign > 0 else held.high.max()
                pk = s.idxmax()
                rows.append(
                    {
                        "day": Path(day_dir).name[5:],
                        "acct": b["summary"]["account"][5:],
                        "side": ref.underlying_direction[:4],
                        "entry_et": e_at.tz_convert(ET).strftime("%H:%M"),
                        "qty": t["qty"],
                        "in": en,
                        "out": ex,
                        "pnl$": round(t["broker_gross_usd"]),
                        "exit": ",".join(r.replace("REF_", "") for r in t["exit_reasons"]),
                        "opt peak%": round((s.max() / en - 1) * 100, 1),
                        "peak@s": int((pk - e_at).total_seconds()),
                        "opt low%": round((s.min() / en - 1) * 100, 1),
                        "ref R": round(
                            sign
                            * (float(ref.reference_exit_price) - float(ref.reference_entry_price))
                            / float(ref.r_u),
                            2,
                        ),
                        "ref best R": round(
                            sign * (best - float(ref.reference_entry_price)) / float(ref.r_u), 2
                        ),
                        "ref worst R": round(
                            sign * (worst - float(ref.reference_entry_price)) / float(ref.r_u), 2
                        ),
                    }
                )
                sims.append(option_rules(s, en, ex, t["qty"]))
    return pd.DataFrame(rows), pd.DataFrame(sims)


# ------------------------------------------------------------------ replay positions, R
def r_rules(refs: pd.DataFrame, bars_by: dict):
    out = []
    for _, r in refs.iterrows():
        if pd.isna(r.reference_exit_price):
            continue
        sign = 1 if r.underlying_direction == "bullish" else -1
        E, ru = float(r.reference_entry_price), float(r.r_u)
        base = sign * (float(r.reference_exit_price) - E) / ru
        b = bars_by[r.session_id]
        held = b[(b.slot > int(r.entry_slot)) & (b.slot <= int(r.exit_slot))]
        closes = (sign * (held.close.values - E) / ru) if len(held) else np.array([])
        res = {
            "strategy": r.strategy_id,
            "base": base,
            "mfe_close": closes.max() if len(closes) else 0.0,
        }
        for T, name in ((0.5, "0.5"), (1.0, "1.0")):
            idx = next(
                (i for i, c in enumerate(closes[:-1]) if c >= T), None
            )  # not on the exit bar itself
            if idx is None:
                res[f"half@{name}R"] = base
                res[f"half@{name}R+BE"] = base
                res[f"trig@{name}"] = False
                continue
            half = closes[idx]
            res[f"half@{name}R"] = 0.5 * half + 0.5 * base
            rest = base
            for c in closes[idx + 1 : -1]:
                if c <= 0:
                    rest = c
                    break
            res[f"half@{name}R+BE"] = 0.5 * half + 0.5 * rest
            res[f"trig@{name}"] = True
        out.append(res)
    return pd.DataFrame(out)


def replay_review():
    from tournament.features.pipeline import run_sessions
    from tournament.marketdata.eligibility import Usage
    from tournament.marketdata.store import HistoryStore
    from tournament.replay.harness import ReplayRun, dataset_id_for
    from tournament.strategies.brt import BRT
    from tournament.strategies.fbr import FBR
    from tournament.strategies.orb import ORB
    from tournament.strategies.vmr import VMR
    from tournament.strategies.vpc import VPC

    store = HistoryStore(ROOT / "data" / "history")
    outs = list(run_sessions(store, Usage.DEVELOPMENT, date(2024, 2, 1), date(2024, 4, 30)))
    run = ReplayRun(
        [VPC(), ORB(), BRT(), FBR(), VMR()], Usage.DEVELOPMENT, dataset_id=dataset_id_for(store)
    ).run_outputs(outs)
    return r_rules(run.reference_positions, {o.session_id: o.bars for o in outs})


if __name__ == "__main__":
    live, sims = live_review()
    print("LIVE TRADES, one row each (option path from the one-second bids)")
    print(live.to_string(index=False))
    print(
        "\nWHAT EACH DECLARED OPTION RULE WOULD HAVE DONE, dollars over the 13 trades (negative = loss)"
    )
    tot = sims.sum().round(0)
    changed = {c: int((sims[c].round(2) != sims["O1 as executed"].round(2)).sum()) for c in sims}
    losers = sims["O1 as executed"] < 0
    print(
        pd.DataFrame(
            {
                "total $": tot,
                "trades changed": pd.Series(changed),
                "losers' total $": sims[losers].sum().round(0),
            }
        ).to_string()
    )
    rr = replay_review()
    print(f"\nREPLAY (2024-02-01 to 2024-04-30), {len(rr)} positions, in R: aggregates")
    cols = ["base", "half@0.5R", "half@0.5R+BE", "half@1.0R", "half@1.0R+BE"]
    for label, g in [("all", rr)] + [(s, x) for s, x in rr.groupby("strategy")]:
        if len(g) == 0:
            continue
        line = {c: round(g[c].mean(), 3) for c in cols}
        win = {c: round(float((g[c] > 0).mean()), 2) for c in cols}
        print(f"{label:5} n={len(g):3}  mean R {line}")
        print(f"      win share {win}")
    los = rr[rr.base < 0]
    print(
        f"\nbaseline LOSERS: {len(los)} of {len(rr)}. Share that ever CLOSED at or above +0.5R: {round(float((los.mfe_close >= 0.5).mean()), 2)}, at or above +1R: {round(float((los.mfe_close >= 1.0).mean()), 2)}"
    )
    print("mean R among the losers under each rule:", {c: round(los[c].mean(), 3) for c in cols})
    print(
        "share of ALL positions where the first tier triggered: +0.5R %.2f, +1R %.2f"
        % (rr["trig@0.5"].mean(), rr["trig@1.0"].mean())
    )
