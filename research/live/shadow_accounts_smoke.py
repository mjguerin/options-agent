# ruff: noqa
"""Shadow accounts smoke (integrity, no strategy behaviour): real OPRA quotes, one injected
candidate per direction, modeled fills, a reference exit, the summary. 50 seconds."""

import asyncio, json, tempfile
from datetime import date, datetime, timedelta, UTC
from pathlib import Path

import pandas as pd

from tournament.adapters.alpaca import data_account_alias
from tournament.adapters.alpaca_history import AlpacaHistory
from tournament.live.options_runner import OptionsCollector
from tournament.live.runner import latest_spy_price, load_feed_config
from tournament.live.shadow_accounts import ShadowAccounts
from tournament.marketdata.calendar import SessionDay
from tournament.secrets import load_alpaca_credentials

creds = load_alpaca_credentials(data_account_alias())
fc = load_feed_config()
now = datetime.now(UTC)
day = SessionDay(
    date.today(), now.replace(hour=13, minute=30, second=0, microsecond=0), now + timedelta(hours=4)
)
spot = latest_spy_price(creds, fc["spot_feed"], "SPY")
c = OptionsCollector(fc["options"], fc, creds, day, "shadow", spot, AlpacaHistory(creds))
tmp = Path(tempfile.mkdtemp())
acc = ShadowAccounts(
    day, c, ["vpc", "orb", "brt", "fbr", "vmr"], 10000.0, 10, tmp / "a.sqlite", 1.0
)
real_tick = c.stream.on_tick
c.stream.on_tick = lambda t: (real_tick(t), acc.tick(t))


def rec(cid, sid, direction):
    t = pd.Timestamp.now(tz="UTC")
    return {
        "candidate_id": cid,
        "account_binding": f"acct:{sid}",
        "underlying_direction": direction,
        "entry_reference": spot,
        "submission_eligible": True,
        "expires_at": t + pd.Timedelta(seconds=60),
        "signal_available_at": t,
    }


async def main():
    task = asyncio.create_task(c.run(pd.Timestamp.now(tz="UTC") + pd.Timedelta(seconds=50)))
    await asyncio.sleep(12)  # quotes are flowing
    acc._candidate(rec("smoke-up", "vpc", "bullish"))
    acc._candidate(rec("smoke-dn", "vmr", "bearish"))
    await asyncio.sleep(15)
    acc._reference_exit("vpc", "smoke-up", "TARGET", pd.Timestamp.now(tz="UTC"))
    await task


asyncio.run(main())
acc.tick(pd.Timestamp.now(tz="UTC") + pd.Timedelta(seconds=5))
print(json.dumps(acc.summary(), indent=1, default=str))
for i in acc.store.all_for("acct:vpc") + acc.store.all_for("acct:vmr"):
    print(
        i.account_binding,
        i.purpose.value,
        i.side,
        i.symbol,
        i.qty_intended,
        i.limit_price,
        i.status.value,
        i.qty_filled,
        i.avg_fill_price,
        i.reasons,
    )
