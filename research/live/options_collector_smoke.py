# ruff: noqa
"""Collector smoke (integrity): real OPRA for 70 s with a re-centring at 35 s; writes snapshots."""

import asyncio, json, sys
from datetime import date, datetime, timedelta, UTC
from pathlib import Path

import pandas as pd

from tournament.adapters.alpaca import data_account_alias
from tournament.adapters.alpaca_history import AlpacaHistory
from tournament.live.options_runner import OptionsCollector
from tournament.live.runner import latest_spy_price, load_feed_config
from tournament.marketdata.calendar import SessionDay
from tournament.secrets import load_alpaca_credentials

creds = load_alpaca_credentials(data_account_alias())
cfg = load_feed_config()["options"]
now = datetime.now(UTC)
day = SessionDay(
    date.today(), now.replace(hour=13, minute=30, second=0, microsecond=0), now + timedelta(hours=5)
)
fc = load_feed_config()
spot = latest_spy_price(creds, fc["spot_feed"], "SPY")
c = OptionsCollector(cfg, fc, creds, day, "shadow", spot, AlpacaHistory(creds))
first = set(c.stream.desired)


async def main():
    t = asyncio.create_task(c.run(pd.Timestamp.now(tz="UTC") + pd.Timedelta(seconds=70)))
    await asyncio.sleep(35)
    c.on_bar({"close": spot + 4.0})
    await t


asyncio.run(main())
out = Path("data/smoke_options")
out.mkdir(parents=True, exist_ok=True)
s = c.write(out)
print(json.dumps(s, indent=1, default=str))
print(
    "window moved:",
    len(first - c.stream.desired),
    "contracts dropped,",
    len(c.stream.desired - first),
    "added; confirmed",
    len(c.stream.confirmed),
)
snaps = pd.read_parquet(out / "option_quotes_1s.parquet")
print(
    snaps.groupby("second").size().describe()[["count", "mean", "max"]].to_dict(),
    snaps.symbol.nunique(),
)
