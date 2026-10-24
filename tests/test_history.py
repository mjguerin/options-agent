from datetime import UTC, date, datetime, time, timedelta

import pyarrow as pa
import pytest

from tournament.adapters.alpaca_history import (
    bars_table,
    concat_trade_slices,
    option_trades_table,
    time_slices,
    trades_table,
)
from tournament.marketdata.calendar import session_from_exchange_times
from tournament.marketdata.history import (
    DATASETS,
    Downloader,
    SessionGuardError,
    UniverseRule,
    bars_from_trades,
    check_outside_session,
)
from tournament.marketdata.schemas import CONTRACT_SCHEMA, OPTION_BAR_SCHEMA
from tournament.marketdata.store import HistoryStore

DAY = session_from_exchange_times(date(2024, 2, 1), time(9, 30), time(16, 0))


def raw_bar(ts: datetime, price: float) -> dict:
    return {
        "t": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "o": price,
        "h": price + 1,
        "l": price - 1,
        "c": price,
        "v": 100,
        "n": 10,
        "vw": price,
    }


def raw_trade(ts: str, price: float, size: int, trade_id: int = 1) -> dict:
    return {"t": ts, "p": price, "s": size, "x": "C", "c": ["I"], "i": trade_id, "z": "A"}


class FakeSource:
    def __init__(self, option_bars_missing=False):
        self.calls = []
        self.contract_bounds = None
        self.option_bars_missing = option_bars_missing

    def calendar(self, start, end):
        self.calls.append("calendar")
        return [DAY]

    def stock_bars(self, symbol, start, end):
        self.calls.append("stock_bars")
        # A pre-market bar at 480 must not anchor the strikes; the 09:30 bar at 500 does.
        return bars_table(
            [
                raw_bar(DAY.open_utc - timedelta(hours=1), 480.0),
                raw_bar(DAY.open_utc, 500.0),
                raw_bar(DAY.close_utc, 510.0),
            ]
        )

    def stock_trades(self, symbol, start, end):
        self.calls.append("stock_trades")
        self.trade_windows = getattr(self, "trade_windows", []) + [(start, end)]
        return trades_table(
            [
                {
                    "t": "2024-02-01T14:30:00.000699096Z",
                    "p": 500.1,
                    "s": 40,
                    "x": "T",
                    "c": [" ", "T"],
                    "i": 7,
                    "z": "B",
                }
            ]
        )

    def option_contracts(self, underlying, exp_from, exp_to, low, high):
        self.calls.append("option_contracts")
        self.contract_bounds = (exp_from, exp_to, low, high)
        return pa.table(
            {
                "symbol": ["SPY240201C00500000", "SPY240201P00500000"],
                "expiration": [date(2024, 2, 1)] * 2,
                "strike": [500.0] * 2,
                "right": ["call", "put"],
                "style": ["american"] * 2,
                "multiplier": [100] * 2,
            },
            schema=CONTRACT_SCHEMA,
        )

    def option_bars(self, symbols, start, end):
        self.calls.append("option_bars")
        if self.option_bars_missing:
            return OPTION_BAR_SCHEMA.empty_table()
        return pa.concat_tables(
            [bars_table([raw_bar(DAY.open_utc, 1.5)], symbol=s) for s in symbols]
        )

    def option_trades(self, symbols, start, end):
        self.calls.append("option_trades")
        return option_trades_table(
            {
                s: [
                    raw_trade("2024-02-01T14:30:05Z", 1.0, 2),
                    raw_trade("2024-02-01T14:30:40Z", 2.0, 2),
                ]
                for s in symbols
            }
        )


@pytest.fixture
def setup(tmp_path):
    source, store = FakeSource(), HistoryStore(tmp_path)
    return (
        source,
        store,
        Downloader(
            source,
            store,
            UniverseRule(max_dte=7, strike_pct=0.05),
            commit="abc123",
            log=lambda _: None,
        ),
    )


def test_calendar_is_utc_across_dst_and_early_close():
    assert DAY.open_utc == datetime(2024, 2, 1, 14, 30, tzinfo=UTC)  # EST
    summer = session_from_exchange_times(date(2024, 7, 1), time(9, 30), time(16, 0))
    assert summer.open_utc == datetime(2024, 7, 1, 13, 30, tzinfo=UTC)  # EDT
    black_friday = session_from_exchange_times(date(2024, 11, 29), time(9, 30), time(13, 0))
    assert black_friday.early_close and not DAY.early_close
    assert DAY.extended_start_utc == datetime(2024, 2, 1, 9, 0, tzinfo=UTC)


def test_trades_keep_nanoseconds_and_conditions():
    t = trades_table(
        [
            {
                "t": "2024-02-01T14:30:00.000699096Z",
                "p": 1.0,
                "s": 1,
                "x": "T",
                "c": [" ", "T"],
                "i": 1,
                "z": "B",
            }
        ]
    )
    row = t.to_pylist()[0]
    assert t.column("ts").cast(pa.int64())[0].as_py() % 1_000_000_000 == 699096
    assert row["conditions"] == [" ", "T"]


def test_downloads_every_dataset_with_provenance(setup):
    source, store, dl = setup
    dl.run(date(2024, 2, 1), date(2024, 2, 1))
    for name in DATASETS:
        assert store.has(name, DAY.session_date), name
    meta = store.provenance(store.path("option_contracts", DAY.session_date))
    assert meta["code_commit"] == "abc123" and meta["reference_open"] == 500.0
    assert (store.root / "calendar.parquet").is_file()
    assert not list(store.root.rglob("*.partial"))


def test_universe_anchored_on_regular_session_open(setup):
    source, store, dl = setup
    dl.download_day(DAY)
    exp_from, exp_to, low, high = source.contract_bounds
    assert (exp_from, exp_to) == (date(2024, 2, 1), date(2024, 2, 8))
    assert (low, high) == pytest.approx((475.0, 525.0))


def test_regular_session_flag(setup):
    source, store, dl = setup
    dl.download_day(DAY)
    flags = store.read("spy_bars_1m", DAY.session_date).column("regular_session").to_pylist()
    assert flags == [False, True, False]  # pre-market, 09:30, 16:00 (close is exclusive)


def test_contracts_carry_no_point_in_time_fields(setup):
    source, store, dl = setup
    dl.download_day(DAY)
    columns = set(store.read("option_contracts", DAY.session_date).column_names)
    assert not columns & {"close_price", "open_interest"}


def test_rerun_skips_days_on_disk(setup):
    source, store, dl = setup
    dl.download_day(DAY)
    source.calls.clear()
    assert dl.download_day(DAY) == {}
    assert source.calls == []


def test_force_redownloads(setup):
    source, store, dl = setup
    dl.download_day(DAY)
    assert set(dl.download_day(DAY, force=True)) == set(DATASETS)


def test_option_bars_alone_rebuild_missing_universe(setup):
    source, store, dl = setup
    written = dl.download_day(DAY, datasets=["option_bars_1m"])
    assert set(written) == {"option_contracts", "option_bars_1m"}
    assert store.read("option_bars_1m", DAY.session_date).schema.equals(OPTION_BAR_SCHEMA)


def test_option_bars_reuse_universe_on_disk(setup):
    source, store, dl = setup
    dl.download_day(DAY, datasets=["spy_bars_1m", "option_contracts"])
    source.calls.clear()
    dl.download_day(DAY, datasets=["option_bars_1m"])
    assert source.calls == ["option_bars"]


@pytest.mark.parametrize(
    "when, blocked",
    [
        (datetime(2026, 10, 22, 14, 0, tzinfo=UTC), True),  # Thu 10:00 ET
        (datetime(2026, 10, 22, 21, 0, tzinfo=UTC), False),  # Thu 17:00 ET
        (datetime(2026, 10, 24, 15, 0, tzinfo=UTC), False),  # Saturday
    ],
)
def test_session_guard(when, blocked):
    if blocked:
        with pytest.raises(SessionGuardError):
            check_outside_session(when)
    else:
        check_outside_session(when)


def test_missing_option_bars_are_built_from_trades(tmp_path):
    source, store = FakeSource(option_bars_missing=True), HistoryStore(tmp_path)
    Downloader(source, store, commit="abc123", log=lambda _: None).download_day(DAY)
    bars = store.read("option_bars_1m", DAY.session_date)
    assert bars.num_rows == 2  # one minute for each of the two contracts
    assert store.provenance(store.path("option_bars_1m", DAY.session_date))["bars_source"] == (
        "built_from_trades"
    )
    assert "option_trades" in source.calls


def test_alpaca_bars_are_labelled(setup):
    source, store, dl = setup
    dl.download_day(DAY)
    meta = store.provenance(store.path("option_bars_1m", DAY.session_date))
    assert meta["bars_source"] == "alpaca_bars"
    assert "option_trades" not in source.calls


def test_bars_from_trades():
    trades = option_trades_table(
        {
            "X": [
                raw_trade("2024-02-01T14:30:05Z", 1.0, 1),
                raw_trade("2024-02-01T14:30:20Z", 3.0, 3),
                raw_trade("2024-02-01T14:30:59.999Z", 2.0, 1),
                raw_trade("2024-02-01T14:31:00Z", 5.0, 2),
            ]
        }
    )
    first, second = bars_from_trades(trades).to_pylist()
    assert (first["open"], first["high"], first["low"], first["close"]) == (1.0, 3.0, 1.0, 2.0)
    assert (first["volume"], first["trade_count"]) == (5, 3)
    assert first["vwap"] == pytest.approx((1 * 1 + 3 * 3 + 2 * 1) / 5)
    assert second["ts"] == datetime(2024, 2, 1, 14, 31, tzinfo=UTC) and second["volume"] == 2


def test_time_slices_cover_the_session_exactly():
    windows = time_slices(DAY.open_utc, DAY.close_utc, 30)
    assert len(windows) == 13
    assert windows[0][0] == DAY.open_utc and windows[-1][1] == DAY.close_utc
    assert all(a[1] == b[0] for a, b in zip(windows, windows[1:], strict=False))


def test_slices_drop_trades_repeated_on_an_edge():
    edge = raw_trade("2024-02-01T15:00:00Z", 2.0, 1, trade_id=9)
    first = trades_table([raw_trade("2024-02-01T14:59:59Z", 1.0, 1, trade_id=8), edge])
    second = trades_table([edge, raw_trade("2024-02-01T15:00:01Z", 3.0, 1, trade_id=10)])
    assert concat_trade_slices([first, second]).column("trade_id").to_pylist() == [8, 9, 10]


def test_option_trades_without_ids():
    # Alpaca option trades have no "i" field; the column is null for them.
    rows = [{"t": "2024-02-02T14:30:00Z", "p": 1.0, "s": 1, "x": "C", "c": ["I"]}]
    table = option_trades_table({"SPY240202C00490000": rows})
    assert table.column("trade_id").to_pylist() == [None]
    assert bars_from_trades(table).num_rows == 1


def test_run_stops_between_days_and_keeps_finished_ones(tmp_path):
    second = session_from_exchange_times(date(2024, 2, 2), time(9, 30), time(16, 0))
    source, store = FakeSource(), HistoryStore(tmp_path)
    source.calendar = lambda start, end: [DAY, second]
    checks = []

    def guard():
        checks.append(1)
        if len(checks) == 2:
            raise SessionGuardError("session open")

    with pytest.raises(SessionGuardError):
        Downloader(source, store, log=lambda _: None).run(
            DAY.session_date, second.session_date, before_each_day=guard
        )
    assert store.has("spy_trades", DAY.session_date)
    assert not store.has("spy_trades", second.session_date)


def test_premarket_trades_window_and_raw_conditions(setup):
    source, store, dl = setup
    dl.download_day(DAY, datasets=["spy_trades_premarket"])
    ((start, end),) = source.trade_windows
    assert start == datetime(2024, 2, 1, 9, 0, tzinfo=UTC)  # 04:00 ET (EST)
    assert end == DAY.open_utc
    stored = store.read("spy_trades_premarket", DAY.session_date)
    assert stored.column("conditions").to_pylist() == [[" ", "T"]]  # unfiltered
    assert (
        store.provenance(store.path("spy_trades_premarket", DAY.session_date))["window"]
        == "04:00 ET to open"
    )
