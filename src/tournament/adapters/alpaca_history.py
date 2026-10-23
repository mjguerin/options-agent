"""Alpaca historical data, converted to our own Arrow schemas (MD-01, MD-02, MD-11).

Uses raw responses: building pydantic models for ~1M trades a day is twice as slow.
Alpaca has no historical option quotes, only option bars and trades, so replay
sees option prices but not past bid/ask spreads.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

import pandas as pd
import pyarrow as pa
from alpaca.data.enums import DataFeed
from alpaca.data.historical import OptionHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import (
    OptionBarsRequest,
    OptionTradesRequest,
    StockBarsRequest,
    StockTradesRequest,
)
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.enums import AssetStatus
from alpaca.trading.requests import GetCalendarRequest, GetOptionContractsRequest

from tournament.adapters.alpaca import paper_trading_client
from tournament.marketdata.calendar import SessionDay, session_from_exchange_times
from tournament.marketdata.schemas import (
    BAR_SCHEMA,
    CONTRACT_SCHEMA,
    OPTION_BAR_SCHEMA,
    OPTION_TRADE_SCHEMA,
    TRADE_SCHEMA,
)
from tournament.secrets import AlpacaCredentials

OPTION_SYMBOLS_PER_REQUEST = 100


def _ts(values: list[str]) -> pd.Series:
    return pd.to_datetime(pd.Series(values, dtype="object"), utc=True, format="ISO8601")


def bars_table(rows: list[dict], symbol: str | None = None) -> pa.Table:
    df = pd.DataFrame(
        {
            "ts": _ts([r["t"] for r in rows]),
            "open": [r["o"] for r in rows],
            "high": [r["h"] for r in rows],
            "low": [r["l"] for r in rows],
            "close": [r["c"] for r in rows],
            "volume": [int(r["v"]) for r in rows],
            "trade_count": [int(r["n"]) for r in rows],
            "vwap": [r["vw"] for r in rows],
        }
    )
    if symbol is None:
        return pa.Table.from_pandas(df, schema=BAR_SCHEMA, preserve_index=False)
    df.insert(0, "symbol", symbol)
    return pa.Table.from_pandas(df, schema=OPTION_BAR_SCHEMA, preserve_index=False)


def trades_table(rows: list[dict]) -> pa.Table:
    df = pd.DataFrame(
        {
            "ts": _ts([r["t"] for r in rows]),
            "price": [r["p"] for r in rows],
            "size": [int(r["s"]) for r in rows],
            "exchange": [r["x"] for r in rows],
            "conditions": [list(r.get("c") or []) for r in rows],
            "trade_id": [r.get("i") for r in rows],  # option trades carry no ID
            "tape": [r.get("z") for r in rows],
        }
    )
    return pa.Table.from_pandas(df, schema=TRADE_SCHEMA, preserve_index=False)


def option_trades_table(raw: dict[str, list[dict]]) -> pa.Table:
    tables = []
    for symbol in sorted(raw):
        t = trades_table(raw[symbol])
        tables.append(t.add_column(0, "symbol", pa.array([symbol] * t.num_rows, pa.string())))
    if not tables:
        return OPTION_TRADE_SCHEMA.empty_table()
    return pa.concat_tables(tables)


def time_slices(start: datetime, end: datetime, minutes: int) -> list[tuple[datetime, datetime]]:
    step = timedelta(minutes=minutes)
    edges = [start]
    while edges[-1] + step < end:
        edges.append(edges[-1] + step)
    return list(zip(edges, [*edges[1:], end], strict=True))


def concat_trade_slices(tables: list[pa.Table]) -> pa.Table:
    """Join time slices in order. A trade stamped exactly on a slice edge can come
    back in both neighbours, so drop exact repeats (same time, id, venue, price, size)."""
    if not tables:
        return TRADE_SCHEMA.empty_table()
    df = pa.concat_tables(tables).to_pandas()
    key = ["ts", "trade_id", "exchange", "price", "size"]
    df = df.loc[~df.duplicated(subset=key)].sort_values("ts", kind="stable")
    return pa.Table.from_pandas(df, schema=TRADE_SCHEMA, preserve_index=False)


TRADE_SLICE_MINUTES = 30
PARALLEL_REQUESTS = 8


class AlpacaHistory:
    """Reads history with the `data` keys (the login that holds SIP + OPRA)."""

    def __init__(self, creds: AlpacaCredentials) -> None:
        self._creds = creds
        self.stock = StockHistoricalDataClient(creds.key_id, creds.secret_key, raw_data=True)
        self.option = OptionHistoricalDataClient(creds.key_id, creds.secret_key, raw_data=True)
        self.trading = paper_trading_client(creds)

    def _stock_client(self) -> StockHistoricalDataClient:
        # One client per worker thread; a shared requests session isn't thread-safe.
        return StockHistoricalDataClient(self._creds.key_id, self._creds.secret_key, raw_data=True)

    def calendar(self, start: date, end: date) -> list[SessionDay]:
        rows = self.trading.get_calendar(GetCalendarRequest(start=start, end=end))
        return [session_from_exchange_times(r.date, r.open.time(), r.close.time()) for r in rows]

    def stock_bars(self, symbol: str, start: datetime, end: datetime) -> pa.Table:
        raw = self.stock.get_stock_bars(
            StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame.Minute,
                start=start,
                end=end,
                feed=DataFeed.SIP,
            )
        )
        return bars_table(raw.get(symbol, []))

    def stock_trades(self, symbol: str, start: datetime, end: datetime) -> pa.Table:
        # Old pages come back slowly (~3.5 s per 10,000 trades), so fetch 30-minute
        # slices in parallel; ~65 pages a day stays far under the rate limit.
        def fetch(window: tuple[datetime, datetime]) -> pa.Table:
            raw = self._stock_client().get_stock_trades(
                StockTradesRequest(
                    symbol_or_symbols=symbol, start=window[0], end=window[1], feed=DataFeed.SIP
                )
            )
            return trades_table(raw.get(symbol, []))

        windows = time_slices(start, end, TRADE_SLICE_MINUTES)
        with ThreadPoolExecutor(max_workers=PARALLEL_REQUESTS) as pool:
            return concat_trade_slices(list(pool.map(fetch, windows)))

    def option_contracts(
        self, underlying: str, exp_from: date, exp_to: date, strike_low: float, strike_high: float
    ) -> pa.Table:
        found = {}
        # Expired contracts are "inactive"; ones still trading today are "active".
        for status in (AssetStatus.INACTIVE, AssetStatus.ACTIVE):
            token = None
            while True:
                page = self.trading.get_option_contracts(
                    GetOptionContractsRequest(
                        underlying_symbols=[underlying],
                        status=status,
                        expiration_date_gte=exp_from,
                        expiration_date_lte=exp_to,
                        strike_price_gte=f"{strike_low:.2f}",
                        strike_price_lte=f"{strike_high:.2f}",
                        limit=10000,
                        page_token=token,
                    )
                )
                for c in page.option_contracts or []:
                    found[c.symbol] = c
                token = page.next_page_token
                if not token:
                    break
        contracts = [found[s] for s in sorted(found)]
        return pa.table(
            {
                "symbol": [c.symbol for c in contracts],
                "expiration": [c.expiration_date for c in contracts],
                "strike": [float(c.strike_price) for c in contracts],
                "right": [getattr(c.type, "value", c.type) for c in contracts],
                "style": [getattr(c.style, "value", c.style) for c in contracts],
                "multiplier": [int(c.size) for c in contracts],
            },
            schema=CONTRACT_SCHEMA,
        )

    def option_bars(self, symbols: list[str], start: datetime, end: datetime) -> pa.Table:
        tables = []
        for i in range(0, len(symbols), OPTION_SYMBOLS_PER_REQUEST):
            batch = symbols[i : i + OPTION_SYMBOLS_PER_REQUEST]
            raw = self.option.get_option_bars(
                OptionBarsRequest(
                    symbol_or_symbols=batch, timeframe=TimeFrame.Minute, start=start, end=end
                )
            )
            tables += [bars_table(raw[s], symbol=s) for s in batch if raw.get(s)]
        if not tables:
            return OPTION_BAR_SCHEMA.empty_table()
        return pa.concat_tables(tables).sort_by([("symbol", "ascending"), ("ts", "ascending")])

    def option_trades(self, symbols: list[str], start: datetime, end: datetime) -> pa.Table:
        raw: dict[str, list[dict]] = {}
        for i in range(0, len(symbols), OPTION_SYMBOLS_PER_REQUEST):
            batch = symbols[i : i + OPTION_SYMBOLS_PER_REQUEST]
            raw.update(
                self.option.get_option_trades(
                    OptionTradesRequest(symbol_or_symbols=batch, start=start, end=end)
                )
            )
        return option_trades_table({s: rows for s, rows in raw.items() if rows})
