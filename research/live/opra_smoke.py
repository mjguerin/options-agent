# ruff: noqa
"""OPRA smoke test (integrity, no strategy behaviour): connect, subscribe the 0DTE watch window,
measure delivery lag and quote gates, then probe the provider's symbol limit."""

import asyncio
from datetime import date, timedelta
import sys

import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest

from tournament.adapters.alpaca import DATA_ACCOUNT_ALIAS
from tournament.adapters.alpaca_history import AlpacaHistory
from tournament.live.option_stream import AlpacaOptionStream
from tournament.live.quotes import OptionFeed
from tournament.live.runner import load_feed_config
from tournament.live.subscriptions import OptionUniverse, parse_occ
from tournament.secrets import load_alpaca_credentials

creds = load_alpaca_credentials(DATA_ACCOUNT_ALIAS)
cfg = load_feed_config()["options"]
spot = (
    StockHistoricalDataClient(creds.key_id, creds.secret_key)
    .get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols="SPY", feed="sip"))["SPY"]
    .price
)
today = date.today()
rows = AlpacaHistory(creds).option_contracts("SPY", today, today, spot * 0.9, spot * 1.1)
df = pd.DataFrame(rows.to_pandas())
print("spot", spot, "contracts today", len(df), flush=True)
uni = OptionUniverse(df[["symbol", "expiration", "strike", "right"]])
small = uni.window(spot, {today}, cfg["watch_strikes_each_side"])
big = uni.window(spot, {today}, 60)
print("window sizes", len(small), len(big), flush=True)

feed = OptionFeed(
    pd.Timedelta(seconds=cfg["quote_max_age_s"]),
    pd.Timedelta(seconds=cfg["quote_max_delivery_lag_s"]),
    pd.Timedelta(seconds=cfg["feed_silent_after_s"]),
)
open_ts = pd.Timestamp(today.isoformat() + "T13:30:00Z")
st = AlpacaOptionStream(
    "opra",
    creds.key_id,
    creds.secret_key,
    mode="shadow",
    max_symbols=1000,
    on_messages=feed.on_messages,
    on_connected=feed.on_connected,
    on_disconnected=feed.on_disconnected,
    on_tick=lambda now: feed.tick(now, open_ts, bool(st.confirmed)),
)
st.set_symbols(set(small))


async def main():
    task = asyncio.create_task(st.run(pd.Timestamp.now(tz="UTC") + pd.Timedelta(seconds=110)))
    await asyncio.sleep(40)
    now = pd.Timestamp.now(tz="UTC")
    views = [feed.view(s, now) for s in small]
    print(
        "phase A: confirmed",
        len(st.confirmed),
        "quotes",
        feed.book.counts,
        "usable",
        sum(v.usable for v in views),
        "of",
        len(views),
        flush=True,
    )
    reasons = {}
    for v in views:
        for r in v.reasons:
            reasons[r] = reasons.get(r, 0) + 1
    print("unusable reasons", reasons, "lag", feed.book.lag_percentiles(), flush=True)
    st.set_symbols(set(big))
    await asyncio.sleep(35)
    print(
        "phase B: asked",
        len(big),
        "confirmed",
        len(st.confirmed),
        "errors",
        st.errors[:3],
        flush=True,
    )
    await task


try:
    asyncio.run(main())
except Exception as e:
    print("ended:", type(e).__name__, e)
print(
    "connects",
    st.connects,
    "errors",
    [(c, m) for _, c, m in st.errors][:5],
    "entries_allowed",
    feed.entries_allowed(pd.Timestamp.now(tz="UTC")),
)
