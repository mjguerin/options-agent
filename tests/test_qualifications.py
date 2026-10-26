"""The owner's qualifications to the Step 3 drafts (2026-10-26): unknown statuses fail closed,
late state-dependent trades are never guessed, clock skew is refused, quantities and marks are
reported honestly, the break-glass path works without the API, subscriptions keep held contracts,
and the nightly check looks at data quality and not only at files."""

import json
import sys
from datetime import date

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from test_account_runtime import CALL, build, cand, prices
from test_account_runtime import at as t_at
from test_live import at, bars, feed, msg, session
from test_live_options import SYM, sec
from test_live_options import feed as option_feed
from test_live_options import q as quote_msg

from tournament.live.quotes import CLOCK_SKEW
from tournament.risk.config import load_risk_config
from tournament.risk.daily import DailyLossGuard, OpenPosition
from tournament.risk.sizing import size_order

RISK = load_risk_config()


# ---- D-49: the feed's trading status, read through the configured mapping ------------------


def status(s, code, when, received=None):
    m = {"T": "s", "S": "SPY", "sc": code, "t": when.isoformat()}
    s.on_messages([m], received or when + pd.Timedelta(milliseconds=50))


def test_an_unlisted_status_code_fails_closed_for_entries():
    s = session()
    feed(s, msg(at(1, 5), 500.0))
    status(s, "Z", at(2, 10))  # not in the mapping: not guessed at
    feed(s, msg(at(2, 30), 500.0))
    feed(s, msg(at(4, 30), 500.0))
    s.tick(at(5, 3))
    b = bars(s)
    assert b.at[2, "interval_status"] == "DATA_MISSING_OR_UNCERTAIN"
    assert b.at[4, "interval_status"] == "PRESENT"  # the hold ended after the configured minute
    assert "unknown_status:Z" in [i[2] for i in s.health.unhealthy]


def test_a_listed_informational_code_changes_nothing_and_resume_ends_only_the_halt():
    s = session()
    feed(s, msg(at(1, 5), 500.0))
    status(s, "H", at(2, 10))
    status(s, "Q", at(2, 20))  # quotation resumption: not a trading resumption
    feed(s, msg(at(3, 5), 500.0))
    status(s, "T", at(3, 40))
    feed(s, msg(at(4, 5), 500.0))
    s.tick(at(5, 3))
    assert list(bars(s)["interval_status"]) == ["PRESENT", "HALTED", "HALTED", "PRESENT"]
    assert not any(r.startswith("unknown_status") for _, _, r in s.health.unhealthy)


# ---- D-50: a late trade whose eligibility depends on state is not guessed -----------------


def test_a_late_trade_that_depends_on_the_last_sale_state_makes_the_next_slot_uncertain():
    s = session()
    feed(s, msg(at(1, 5), 500.0))
    feed(s, msg(at(1, 30), 500.2, c=("Z",)), received=at(2, 5))  # NOTE_2: depends on tape order
    feed(s, msg(at(2, 30), 500.0))
    s.tick(at(3, 3))
    b = bars(s)
    assert b.at[1, "interval_status"] == "PRESENT"  # earlier decisions stand
    assert b.at[2, "interval_status"] == "DATA_MISSING_OR_UNCERTAIN"
    assert "late_state_dependent_trade" in b.at[2, "flags"] and bool(b.at[2, "vwap_uncertain"])


def test_a_plain_late_trade_still_counts_forward():
    s = session()
    feed(s, msg(at(1, 5), 500.0))
    feed(s, msg(at(1, 30), 501.0, 100), received=at(2, 5))
    s.tick(at(3, 3))
    b = bars(s)
    assert (
        b.at[2, "interval_status"] != "DATA_MISSING_OR_UNCERTAIN"
        and b.at[2, "cum_total_volume"] == 200
    )


# ---- D-52: a quote stamped in the future means the clocks disagree ------------------------


def test_a_quote_from_the_future_is_unusable_unless_within_the_clock_tolerance():
    f = option_feed()
    f.on_messages([quote_msg(sec(10))], sec(0.5))  # stamped 9.5 s ahead of its arrival
    v = f.view(SYM, sec(1))
    assert not v.usable and CLOCK_SKEW in v.reasons
    f2 = option_feed()
    f2.on_messages([quote_msg(sec(0.6))], sec(0.5))  # 100 ms ahead: inside the 250 ms tolerance
    assert f2.view(SYM, sec(1)).usable


# ---- D-55 and D-58: what was asked for, and the three profit-and-loss figures ---------------


def test_the_requested_quantity_is_kept_beside_the_admitted_one():
    r = size_order(1.44, 5, 10_000, 10_000, RISK)  # the budget allows 13; only 5 are quoted
    assert (r.requested_quantity, r.quantity, r.capped_by) == (13, 5, "ask_size")
    c = size_order(1.44, 50, 10_000, 600, RISK)
    assert (c.requested_quantity, c.quantity, c.capped_by) == (13, 4, "cash")


def test_realized_estimated_and_conservative_risk_are_kept_apart():
    g = DailyLossGuard(RISK.daily_loss, 100)
    marked = OpenPosition(CALL, 10, 1.44, 1.50)  # +$60 at its bid
    blind = OpenPosition(CALL.replace("765", "766"), 5, 1.00, None)  # no usable bid: valued at zero
    v = g.risk_view(-100.0, [marked, blind])
    assert v["realized_usd"] == -100.0
    assert v["estimated_usd"] == pytest.approx(-40.0)  # the blind position is left out, not zeroed
    assert v["risk_usd"] == pytest.approx(-100.0 + 60.0 - 500.0)  # premium counted as lost
    assert v["unmarked_positions"] == 1
    # reading the view never latches anything
    assert g.breached_at is None


# ---- D-53: held contracts keep their quotes ----------------------------------------------------


def test_held_positions_and_working_orders_are_what_the_subscription_must_keep():
    from tournament.live.shadow_accounts import ShadowAccounts

    rt, sim, m = build()
    prices(m, t_at(0))
    rt.on_candidate(t_at(0), cand())
    acc = ShadowAccounts.__new__(ShadowAccounts)
    acc.router = type("R", (), {"runtimes": {rt.binding: rt}})()
    acc.store = rt.store
    assert acc.held_symbols() == {CALL}  # the working entry order
    for s in (1.5, 2.5):
        prices(m, t_at(s))
        rt.on_tick(t_at(s))
    assert acc.held_symbols() == {CALL}  # and then the position


# ---- D-75: the break-glass path ------------------------------------------------------------


@pytest.fixture()
def queue(monkeypatch):
    from tournament.api.store import ControlStore
    from tournament.orders.store import memory_engine

    engine = memory_engine()
    monkeypatch.setattr("tournament.orders.store.default_engine", lambda: engine)
    return ControlStore(engine)


def test_break_glass_queues_an_emergency_without_the_api_and_audits_it(queue, capsys):
    from tournament.cli import main

    assert main(["breakglass", "emergency", "--yes"]) == 0
    (cmd,) = queue.pending(pd.Timestamp.now(tz="UTC"))
    assert (cmd.command, cmd.scope, cmd.actor) == ("EMERGENCY", "global", "operator-cli")
    audit = queue.recent_actions(5)[0]
    assert audit["actor"] == "operator-cli" and audit["command_id"] == cmd.id
    assert "queued EMERGENCY" in capsys.readouterr().out


def test_break_glass_flatten_needs_an_account_and_a_terminal_or_yes(queue, monkeypatch, capsys):
    from tournament.cli import main

    assert main(["breakglass", "flatten", "--yes"]) == 2  # which account?
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert main(["breakglass", "flatten", "--account", "acct:vpc"]) == 2  # no one to confirm
    assert queue.pending(pd.Timestamp.now(tz="UTC")) == []
    assert main(["breakglass", "flatten", "--account", "acct:vpc", "--yes"]) == 0
    (cmd,) = queue.pending(pd.Timestamp.now(tz="UTC"))
    assert (cmd.command, cmd.account) == ("FLATTEN_ACCOUNT", "acct:vpc")
    capsys.readouterr()


def test_the_engine_applies_a_break_glass_command_like_any_other(queue):
    from test_api import CFG as API_CFG
    from test_api import at as api_at

    from tournament.live.controls import ControlPlane

    rt, sim, m = build()
    prices(m, t_at(0))
    rt.on_candidate(t_at(0), cand())
    for s in (1.5, 2.5):
        prices(m, t_at(s))
        rt.on_tick(t_at(s))
    session_stub = type("S", (), {})()
    session_stub.health = type("H", (), {"connected": True})()
    session_stub.day = type("D", (), {"session_date": date(2026, 10, 26)})()
    session_stub.builder = type("B", (), {"finalized": 3})()
    import tempfile
    from pathlib import Path

    plane = ControlPlane(
        queue,
        API_CFG,
        {rt.binding: rt},
        Path(tempfile.mkdtemp()) / "s.json",
        session_stub,
        "shadow",
    )
    queue.enqueue(
        pd.Timestamp.now(tz="UTC"), 120, "operator-cli", "EMERGENCY", "global", None, None, None
    )
    plane.tick(pd.Timestamp.now(tz="UTC"))
    assert rt.operator_halt == "OPERATOR_EMERGENCY" and api_at(0) is not None


# ---- D-76: stale state shows as a sequence that stopped moving ---------------------------------


def test_each_published_state_has_the_next_sequence_number():
    from test_api import CFG as API_CFG

    from tournament.live.controls import ControlPlane

    rt, _, _ = build()
    stub = type("S", (), {})()
    stub.health = type("H", (), {"connected": True})()
    stub.day = type("D", (), {"session_date": date(2026, 10, 26)})()
    stub.builder = type("B", (), {"finalized": 1})()
    plane = ControlPlane(
        None, API_CFG, {rt.binding: rt}, __import__("pathlib").Path("/tmp/x.json"), stub, "shadow"
    )
    a, b = plane.snapshot(t_at(0)), plane.snapshot(t_at(5))
    assert (a["seq"], b["seq"]) == (1, 2) and "pnl_view" in b["accounts"][rt.binding]
    assert b["accounts"][rt.binding]["pnl_view"]["unmarked_positions"] == 0


# ---- D-68: data that exists must also look like a session -------------------------------------


class FakeStore:
    def __init__(self, tmp, rows):
        self.tmp, self.rows = tmp, rows

    def has(self, name, d):
        return (name, d) in self.rows

    def path(self, name, d):
        p = self.tmp / f"{name}-{d}.parquet"
        pq.write_table(pa.table({"x": list(range(self.rows[(name, d)]))}), p)
        return p


def test_a_truncated_download_is_caught_even_though_its_file_exists(tmp_path):
    from tournament.appconfig import app_config
    from tournament.ops.nightly import quality

    q = app_config().nightly.quality
    d1, d2 = date(2026, 10, 22), date(2026, 10, 23)
    rows = {
        ("spy_trades", d1): 600_000, ("spy_bars_1m", d1): 960, ("option_contracts", d1): 200,
        ("option_bars_1m", d1): 50_000,
        ("spy_trades", d2): 1_200, ("spy_bars_1m", d2): 960, ("option_contracts", d2): 200,
        ("option_bars_1m", d2): 50_000,
    }  # fmt: skip
    issues = quality(FakeStore(tmp_path, rows), [d1, d2], q)
    assert list(issues) == ["2026-10-23"] and "spy_trades: 1200 rows" in issues["2026-10-23"][0]
    assert json.dumps(issues)


def test_the_nightly_report_with_thin_data_is_a_failure(tmp_path):
    from tournament.alerts.watchdog import check_nightly

    d = date(2026, 10, 26)
    (tmp_path / "nightly_2026-10-26.json").write_text(
        json.dumps({"missing": {}, "error": None, "quality_issues": {"2026-10-26": ["x"]}})
    )
    f = check_nightly(d, tmp_path, d)[0]
    assert f.active and "1 too thin" in f.detail
