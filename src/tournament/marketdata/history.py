"""Historical downloader: one shared batch job that fills the Parquet store.

Per session day it stores SPY 1-minute bars (04:00 to close + 4h, with a
regular-session flag), SPY trades (regular session), the day's option universe,
and 1-minute bars for that universe. Days already on disk are skipped.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Protocol

import pyarrow as pa
import pyarrow.compute as pc

from tournament.marketdata.calendar import EXCHANGE_TZ, SessionDay
from tournament.marketdata.schemas import CALENDAR_SCHEMA, OPTION_BAR_SCHEMA
from tournament.marketdata.store import HistoryStore

UNDERLYING = "SPY"
DATASETS = ("spy_bars_1m", "spy_trades", "option_contracts", "option_bars_1m")
HISTORY_START = date(2024, 2, 1)  # Alpaca options history begins here (MD-11)


class HistorySource(Protocol):
    def calendar(self, start: date, end: date) -> list[SessionDay]: ...
    def stock_bars(self, symbol: str, start: datetime, end: datetime) -> pa.Table: ...
    def stock_trades(self, symbol: str, start: datetime, end: datetime) -> pa.Table: ...
    def option_contracts(
        self, underlying: str, exp_from: date, exp_to: date, strike_low: float, strike_high: float
    ) -> pa.Table: ...
    def option_bars(self, symbols: list[str], start: datetime, end: datetime) -> pa.Table: ...
    def option_trades(self, symbols: list[str], start: datetime, end: datetime) -> pa.Table: ...


def bars_from_trades(trades: pa.Table) -> pa.Table:
    """1-minute bars per symbol from option trades: first/max/min/last price,
    summed size, trade count and size-weighted price. Every trade counts; no
    condition filter, so totals can differ slightly from Alpaca's own bars."""
    if trades.num_rows == 0:
        return OPTION_BAR_SCHEMA.empty_table()
    df = trades.select(["symbol", "ts", "price", "size"]).to_pandas()
    df = df.sort_values(["symbol", "ts"], kind="stable")
    df["minute"] = df["ts"].dt.floor("min")
    df["notional"] = df["price"] * df["size"]
    g = df.groupby(["symbol", "minute"], sort=True)
    out = g.agg(
        open=("price", "first"),
        high=("price", "max"),
        low=("price", "min"),
        close=("price", "last"),
        volume=("size", "sum"),
        trade_count=("price", "size"),
        notional=("notional", "sum"),
    ).reset_index()
    out["vwap"] = out["notional"] / out["volume"]
    out = out.rename(columns={"minute": "ts"}).drop(columns="notional")
    return pa.Table.from_pandas(
        out[OPTION_BAR_SCHEMA.names], schema=OPTION_BAR_SCHEMA, preserve_index=False
    )


@dataclass(frozen=True)
class UniverseRule:
    """Which option contracts to keep per day. Wide on purpose; D06 narrows it later."""

    max_dte: int = 7  # calendar days after the session date
    strike_pct: float = 0.05  # around the session's first regular-hours open

    def bounds(self, session_date: date, reference_price: float) -> tuple[date, date, float, float]:
        return (
            session_date,
            session_date + timedelta(days=self.max_dte),
            reference_price * (1 - self.strike_pct),
            reference_price * (1 + self.strike_pct),
        )


class SessionGuardError(RuntimeError):
    pass


def check_outside_session(now: datetime | None = None) -> None:
    """Heavy downloads stay out of trading hours (NFR-05): 09:00-16:30 ET on weekdays."""
    now = (now or datetime.now(UTC)).astimezone(EXCHANGE_TZ)
    if now.weekday() < 5 and time(9, 0) <= now.time() <= time(16, 30):
        raise SessionGuardError(
            "Refusing to download during the trading session (09:00-16:30 ET). "
            "Run after the close, or pass --allow-during-session."
        )


def code_commit(repo: Path) -> str:
    try:
        out = subprocess.run(
            # "-dirty" marks files written by uncommitted code.
            ["git", "-C", str(repo), "describe", "--always", "--dirty", "--abbrev=7"],
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def calendar_table(days: Iterable[SessionDay]) -> pa.Table:
    days = list(days)
    return pa.table(
        {
            "session_date": [d.session_date for d in days],
            "open_utc": [d.open_utc for d in days],
            "close_utc": [d.close_utc for d in days],
            "early_close": [d.early_close for d in days],
        },
        schema=CALENDAR_SCHEMA,
    )


def _with_regular_flag(bars: pa.Table, day: SessionDay) -> pa.Table:
    ts = bars.column("ts")
    flag = pc.and_(
        pc.greater_equal(ts, pa.scalar(day.open_utc, bars.schema.field("ts").type)),
        pc.less(ts, pa.scalar(day.close_utc, bars.schema.field("ts").type)),
    )
    return bars.append_column("regular_session", flag)


class Downloader:
    def __init__(
        self,
        source: HistorySource,
        store: HistoryStore,
        universe: UniverseRule | None = None,
        commit: str = "unknown",
        log: Callable[[str], None] = print,
    ) -> None:
        self.source = source
        self.store = store
        self.universe = universe or UniverseRule()
        self.commit = commit
        self.log = log

    def _meta(self, **extra) -> dict:
        return {
            "source": "alpaca",
            "code_commit": self.commit,
            "downloaded_at": datetime.now(UTC).isoformat(timespec="seconds"),
            **extra,
        }

    def sync_calendar(self, start: date, end: date) -> list[SessionDay]:
        days = self.source.calendar(start, end)
        self.store.write("calendar", None, calendar_table(days), self._meta(start=start, end=end))
        return days

    def _spy_bars(self, day: SessionDay, refresh: bool) -> pa.Table:
        if not refresh and self.store.has("spy_bars_1m", day.session_date):
            return self.store.read("spy_bars_1m", day.session_date)
        bars = self.source.stock_bars(UNDERLYING, day.extended_start_utc, day.extended_end_utc)
        return _with_regular_flag(bars, day)

    def _universe(self, day: SessionDay, bars: pa.Table) -> tuple[pa.Table, float]:
        regular = bars.filter(bars.column("regular_session"))
        if regular.num_rows == 0:
            raise RuntimeError(f"{day.session_date}: no regular-session SPY bars to anchor strikes")
        reference = regular.column("open")[0].as_py()
        exp_from, exp_to, low, high = self.universe.bounds(day.session_date, reference)
        return self.source.option_contracts(UNDERLYING, exp_from, exp_to, low, high), reference

    def download_day(
        self, day: SessionDay, datasets: Iterable[str] = DATASETS, force: bool = False
    ) -> dict:
        d = day.session_date
        wanted = {name for name in datasets if force or not self.store.has(name, d)}
        written: dict[str, int] = {}
        # The universe is rebuilt when asked for, or when option bars need it and it's missing.
        need_universe = "option_contracts" in wanted or (
            "option_bars_1m" in wanted and not self.store.has("option_contracts", d)
        )

        def save(name: str, table: pa.Table, **meta) -> None:
            self.store.write(name, d, table, self._meta(session_date=d, **meta))
            written[name] = table.num_rows

        # SPY bars come first: their regular-session open anchors the option universe.
        if "spy_bars_1m" in wanted or need_universe:
            bars = self._spy_bars(day, refresh="spy_bars_1m" in wanted)
            if "spy_bars_1m" in wanted:
                save("spy_bars_1m", bars, feed="sip", symbol=UNDERLYING)

        if "spy_trades" in wanted:
            save(
                "spy_trades",
                self.source.stock_trades(UNDERLYING, day.open_utc, day.close_utc),
                feed="sip",
                symbol=UNDERLYING,
            )

        if need_universe:
            contracts, reference = self._universe(day, bars)
            save(
                "option_contracts",
                contracts,
                reference_open=reference,
                max_dte=self.universe.max_dte,
                strike_pct=self.universe.strike_pct,
            )
        elif "option_bars_1m" in wanted:
            contracts = self.store.read("option_contracts", d)

        if "option_bars_1m" in wanted:
            symbols = contracts.column("symbol").to_pylist()
            obars = self.source.option_bars(symbols, day.open_utc, day.close_utc)
            bars_source = "alpaca_bars"
            if obars.num_rows == 0 and symbols:
                # Alpaca's pre-built option bars have holes (e.g. 2024-02-02) where the
                # trades exist. Build the bars ourselves and label them (MD-03, MD-05).
                obars = bars_from_trades(
                    self.source.option_trades(symbols, day.open_utc, day.close_utc)
                )
                bars_source = "built_from_trades"
                self.log(f"{d}: no Alpaca option bars; built {obars.num_rows} from trades")
            save(
                "option_bars_1m",
                obars,
                feed="opra",
                contracts=len(symbols),
                bars_source=bars_source,
            )
        return written

    def run(
        self,
        start: date,
        end: date,
        datasets: Iterable[str] = DATASETS,
        force: bool = False,
        before_each_day: Callable[[], None] | None = None,
    ) -> None:
        """`before_each_day` may raise (e.g. SessionGuardError) to stop between days;
        finished days stay on disk, so the next run resumes where this one stopped."""
        days = self.sync_calendar(start, end)
        self.log(f"{len(days)} sessions from {start} to {end}")
        for i, day in enumerate(days, 1):
            if before_each_day:
                before_each_day()
            written = self.download_day(day, datasets, force)
            note = ", ".join(f"{k}={v}" for k, v in written.items()) or "already on disk"
            self.log(f"[{i}/{len(days)}] {day.session_date}: {note}")
