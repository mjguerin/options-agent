"""Alerts: incident lifecycle, delivery, rate limits and expiry, watchdogs, the engine monitor."""

import json
from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest
import yaml

from tournament.alerts.bot import AlertBot
from tournament.alerts.config import DEFAULT_PATH, AlertsConfigError, load_alerts_config
from tournament.alerts.incidents import Incidents
from tournament.alerts.monitor import SessionMonitor
from tournament.alerts.store import AlertStore
from tournament.alerts.telegram import SendResult, TelegramSender
from tournament.alerts.watchdog import (
    check_backup,
    check_database,
    check_disk,
    check_engine,
    check_nightly,
    last_weekday_due,
)
from tournament.live.health import FeedHealth
from tournament.orders.store import memory_engine
from tournament.secrets import TelegramCredentials

CFG = load_alerts_config()
T0 = pd.Timestamp("2026-10-26T15:00:00Z")


def at(s: float) -> pd.Timestamp:
    return T0 + pd.Timedelta(seconds=s)


class FakeSender:
    def __init__(self):
        self.sent, self.fail, self.retry_after = [], 0, None

    def send(self, text):
        if self.fail:
            self.fail -= 1
            return SendResult(False, self.retry_after, "HTTP 500")
        self.sent.append(text)
        return SendResult(True)


def build(cfg=CFG):
    store = AlertStore(memory_engine(), cfg)
    sender = FakeSender()
    return store, sender, AlertBot(store, sender, cfg)


def edited(tmp_path, **sections):
    raw = yaml.safe_load(DEFAULT_PATH.read_text())
    for name, values in sections.items():
        raw[name].update(values)
    p = tmp_path / "alerts.yaml"
    p.write_text(yaml.safe_dump(raw))
    return load_alerts_config(p)


# ---- config ----


def test_every_message_is_data_in_the_config(tmp_path):
    assert {"STOCK_FEED_DOWN", "DAILY_LIMIT_HALT", "ENGINE_SILENT", "SESSION_SUMMARY"} <= set(
        CFG.kinds
    )
    assert "{detail}" in CFG.kinds["STOCK_FEED_DOWN"].open
    raw = yaml.safe_load(DEFAULT_PATH.read_text())
    raw["kinds"]["DISK_LOW"]["open"] = "low: {nonsense}"
    p = tmp_path / "a.yaml"
    p.write_text(yaml.safe_dump(raw))
    with pytest.raises(AlertsConfigError):
        load_alerts_config(p)
    with pytest.raises(AlertsConfigError):
        edited(tmp_path, delivery={"max_per_minute": {"info": 0, "warning": 1, "critical": 1}})


# ---- the incident lifecycle (10.5) ----


def test_an_incident_alerts_once_while_open_and_once_when_resolved():
    store, sender, bot = build()
    assert store.open(at(0), "STOCK_FEED_DOWN", "engine", "not connected") is True
    assert store.open(at(5), "STOCK_FEED_DOWN", "engine", "still down") is False  # a flap, not news
    bot.step(at(6))
    assert len(sender.sent) == 1 and "SPY feed down: not connected" in sender.sent[0]
    assert store.resolve(at(30), "STOCK_FEED_DOWN") is True
    assert store.resolve(at(31), "STOCK_FEED_DOWN") is False
    bot.step(at(32))
    assert sender.sent[-1] == "SPY feed is back." and len(sender.sent) == 2
    assert store.open_incidents() == []


def test_incidents_are_per_account_where_they_have_one():
    store, sender, bot = build()
    store.open(at(0), "DAILY_LIMIT_HALT", "engine", "$510 of $500", account="acct:vpc")
    store.open(at(0), "DAILY_LIMIT_HALT", "engine", "$505 of $500", account="acct:orb")
    bot.step(at(1))
    assert len(sender.sent) == 2 and any("acct:orb" in t for t in sender.sent)


def test_a_critical_incident_that_stays_open_is_repeated():
    store, sender, bot = build()
    store.open(at(0), "ENGINE_ERROR", "engine", "slot 40: boom")
    bot.step(at(1))
    bot.step(at(CFG.delivery.repeat_open_critical_s - 1))
    assert len(sender.sent) == 1
    bot.step(at(CFG.delivery.repeat_open_critical_s + 1))
    assert len(sender.sent) == 2 and sender.sent[1].startswith("Still open: ")
    store.open(at(2), "DAILY_LIMIT_HALT", "engine", "x", account="acct:vpc")  # a warning
    bot.step(at(5 * CFG.delivery.repeat_open_critical_s))
    assert not any(t.startswith("Still open: ") and "daily loss" in t for t in sender.sent)


# ---- delivery ----


def test_each_severity_has_its_own_allowance(tmp_path):
    limits = {"info": 1, "warning": 1, "critical": 5}
    store, sender, bot = build(edited(tmp_path, delivery={"max_per_minute": limits}))
    for i in range(4):
        store.notify(at(0), "SESSION_SUMMARY", f"quiet {i}")  # info
    for i in range(3):
        store.open(at(0), "ENGINE_ERROR", "engine", f"boom {i}", key=f"E{i}")  # critical
    bot.step(at(1))
    assert sum("Decision engine stopped" in t for t in sender.sent) == 3  # not held back by info
    assert sum(t.startswith("quiet") for t in sender.sent) == 1


def test_the_most_severe_alert_goes_first():
    store, sender, bot = build()
    store.notify(at(0), "SESSION_SUMMARY", "all quiet")
    store.open(at(0), "DISK_LOW", "watchdog", "5 GB free")  # warning
    store.open(at(0), "DATABASE_DOWN", "watchdog", "OperationalError")  # critical
    bot.step(at(1))
    assert sender.sent[0].startswith("Database unreachable") and sender.sent[-1] == "all quiet"


def test_a_failed_send_backs_off_and_retries_without_losing_the_alert():
    store, sender, bot = build()
    sender.fail, sender.retry_after = 1, 30
    store.open(at(0), "STOCK_FEED_DOWN", "engine", "x")
    bot.step(at(1))
    assert sender.sent == [] and bot.stats["failed"] == 1
    bot.step(at(10))  # inside the 30 s the channel asked for
    assert sender.sent == []
    bot.step(at(32))
    assert len(sender.sent) == 1 and bot.stats["sent"] == 1


def test_the_rate_limit_holds_the_rest_for_the_next_minute(tmp_path):
    limits = {"info": 3, "warning": 3, "critical": 3}
    store, sender, bot = build(edited(tmp_path, delivery={"max_per_minute": limits}))
    for i in range(5):
        store.open(at(0), "ACCOUNT_ERROR", "engine", f"e{i}", account=f"a{i}")
    bot.step(at(1))
    assert len(sender.sent) == 3
    bot.step(at(30))
    assert len(sender.sent) == 3
    bot.step(at(62))
    assert len(sender.sent) == 5


def test_an_alert_that_could_not_be_sent_in_time_is_dropped_with_a_note(tmp_path):
    cfg = edited(tmp_path, delivery={"expire_after_s": 600})
    store, sender, bot = build(cfg)
    store.open(at(0), "STOCK_FEED_DOWN", "engine", "an hour-old outage")
    bot.step(at(700))  # the channel was down; now it is long after
    assert bot.stats["expired"] == 1
    bot.step(at(701))
    assert sender.sent == ["1 alert(s) could not be delivered in time and were dropped."]


def test_the_minimum_severity_is_a_config_value(tmp_path):
    store, sender, bot = build(edited(tmp_path, delivery={"min_severity": "critical"}))
    store.open(at(0), "DISK_LOW", "watchdog", "5 GB free")
    store.open(at(0), "ENGINE_ERROR", "engine", "boom")
    bot.step(at(1))
    assert len(sender.sent) == 1 and sender.sent[0].startswith("Decision engine stopped")


# ---- telegram ----


class Http:
    def __init__(self, status=200, body=None, raises=None):
        self.status, self.body, self.raises, self.calls = status, body or {}, raises, []

    def post(self, url, json, timeout):
        self.calls.append((url, json, timeout))
        if self.raises:
            raise self.raises
        return SimpleNamespace(status_code=self.status, json=lambda: self.body, text=str(self.body))


CREDS = TelegramCredentials("123:SECRET", "42")


def test_the_message_goes_to_the_one_chat_and_the_token_never_leaks():
    http = Http()
    assert TelegramSender(CREDS, CFG.telegram, http).send("hello").ok
    url, body, timeout = http.calls[0]
    assert url == "https://api.telegram.org/bot123:SECRET/sendMessage"
    assert body["chat_id"] == "42" and body["text"] == "hello" and timeout == 10
    err = TelegramSender(
        CREDS, CFG.telegram, Http(raises=OSError("failed for bot123:SECRET"))
    ).send("x")
    assert not err.ok and "SECRET" not in err.error
    assert "SECRET" not in repr(CREDS) and "SECRET" not in str(CREDS)


def test_rate_limit_replies_carry_the_retry_wait():
    res = TelegramSender(CREDS, CFG.telegram, Http(429, {"parameters": {"retry_after": 17}})).send(
        "x"
    )
    assert not res.ok and res.retry_after_s == 17.0


# ---- watchdogs ----

WD = CFG.watchdog
START, CLOSE = pd.Timestamp("2026-10-26T07:55:00Z"), pd.Timestamp("2026-10-26T20:00:00Z")


def engine_findings(now, hb):
    out = check_engine(pd.Timestamp(now), date(2026, 10, 26), START, CLOSE, hb, WD)
    return {f.kind: f.active for f in out}


def test_a_missing_or_silent_engine_is_found_only_while_it_should_be_running():
    beating = {"session": "2026-10-26", "at": "2026-10-26T12:00:00+00:00"}
    assert engine_findings("2026-10-26T07:58:00Z", None) == {
        "ENGINE_NOT_STARTED": False,
        "ENGINE_SILENT": False,
    }
    assert engine_findings("2026-10-26T08:10:00Z", None)["ENGINE_NOT_STARTED"] is True
    yesterday = {"session": "2026-10-23", "at": "2026-10-23T19:00:00+00:00"}
    assert engine_findings("2026-10-26T08:10:00Z", yesterday)["ENGINE_NOT_STARTED"] is True
    assert engine_findings("2026-10-26T12:00:30Z", beating) == {
        "ENGINE_NOT_STARTED": False,
        "ENGINE_SILENT": False,
    }
    assert engine_findings("2026-10-26T12:05:00Z", beating)["ENGINE_SILENT"] is True
    assert not any(engine_findings("2026-10-26T22:00:00Z", None).values())  # after the close
    none = check_engine(T0, None, None, None, None, WD)
    assert not any(f.active for f in none)  # not a session day


FIRST = date(2026, 10, 26)


def test_no_report_is_demanded_for_days_before_the_job_existed(tmp_path):
    assert not check_nightly(date(2026, 10, 23), tmp_path, FIRST)[0].active
    assert check_nightly(date(2026, 10, 26), tmp_path, FIRST)[0].active


def test_the_nightly_job_must_report_and_report_clean(tmp_path):
    tz = "America/New_York"
    now = pd.Timestamp("2026-10-27T00:30:00Z")  # Monday 20:30 ET: past 16:45 + 3 h? no, 19:45
    assert last_weekday_due(now, tz, "16:45", WD.nightly_deadline_after_s) == date(2026, 10, 26)
    early = pd.Timestamp("2026-10-26T22:00:00Z")  # Monday 18:00 ET: Monday's deadline not reached
    assert last_weekday_due(early, tz, "16:45", WD.nightly_deadline_after_s) == date(2026, 10, 23)
    monday = pd.Timestamp("2026-11-02T10:00:00Z")
    assert last_weekday_due(monday, tz, "16:45", WD.nightly_deadline_after_s) == date(2026, 10, 30)
    d = date(2026, 10, 26)
    assert check_nightly(d, tmp_path, FIRST)[0].active  # no report
    (tmp_path / "nightly_2026-10-26.json").write_text(
        json.dumps({"missing": {"2026-10-26": ["spy_trades"]}})
    )
    assert check_nightly(d, tmp_path, FIRST)[0].active
    (tmp_path / "nightly_2026-10-26.json").write_text(json.dumps({"missing": {}, "error": None}))
    assert not check_nightly(d, tmp_path, FIRST)[0].active


def test_backup_must_have_succeeded_not_merely_run(tmp_path):
    log = tmp_path / "backup.log"
    assert check_backup(T0, log, WD)[0].active  # never ran
    log.write_text("pg_dump: error: connection refused\n")  # a failed run touched the file
    assert "no successful backup" in check_backup(T0, log, WD)[0].detail
    log.write_text("2026-10-26T20:00:01Z backup: ok 14M\n")
    when = pd.Timestamp("2026-10-26T20:00:01Z")
    assert not check_backup(when + pd.Timedelta(hours=2), log, WD)[0].active
    assert check_backup(when + pd.Timedelta(hours=WD.backup_max_age_h + 1), log, WD)[0].active


def test_supervisor_programs_it_gave_up_on_are_reported():
    from tournament.alerts.watchdog import check_supervisor

    ok = "alerts RUNNING pid 1\napi RUNNING pid 2\nengine EXITED Oct 26 04:00\n"
    assert not check_supervisor(ok)[0].active and not check_supervisor(None)[0].active
    bad = ok + "dashboard FATAL Exited too quickly\n"
    f = check_supervisor(bad)[0]
    assert f.active and f.detail == "dashboard"


def test_disk_and_database_checks(tmp_path):
    low = lambda p: SimpleNamespace(free=5e9)  # noqa: E731
    ok = lambda p: SimpleNamespace(free=500e9)  # noqa: E731
    assert check_disk(tmp_path, WD, low)[0].active and not check_disk(tmp_path, WD, ok)[0].active
    assert not check_database(memory_engine())[0].active

    class Broken:
        def connect(self):
            raise ConnectionError("down")

    assert check_database(Broken())[0].active


def test_the_bot_opens_and_resolves_incidents_from_its_watchdogs():
    store, sender, bot = build()
    state = {"down": True}
    from tournament.alerts.watchdog import Finding

    bot.watches.append(lambda now: [Finding("DATABASE_DOWN", state["down"], "OperationalError")])
    bot.step(at(0))
    assert [i.kind for i in store.open_incidents()] == ["DATABASE_DOWN"]
    state["down"] = False
    bot.step(at(WD.check_every_s + 1))
    assert store.open_incidents() == [] and sender.sent[-1] == "Database reachable."


def test_a_broken_watchdog_check_never_stops_the_others():
    store, sender, bot = build()
    from tournament.alerts.watchdog import Finding

    def boom(now):
        raise RuntimeError("bug")

    bot.watches += [boom, lambda now: [Finding("DISK_LOW", True, "1 GB")]]
    bot.step(at(0))
    assert [i.kind for i in store.open_incidents()] == ["DISK_LOW"] and bot.stats[
        "watch_errors"
    ] == 1


# ---- the engine's own monitor ----


def monitor(tmp_path, store=None, collector=None, engine=None, accounts=None):
    store = store or AlertStore(memory_engine(), CFG)
    health = FeedHealth(pd.Timedelta(seconds=5))
    health.on_connected(T0 - pd.Timedelta(minutes=1))
    session = SimpleNamespace(
        health=health,
        day=SimpleNamespace(
            session_date=date(2026, 10, 26),
            close_utc=pd.Timestamp("2026-10-26T20:00:00Z").to_pydatetime(),
        ),
        builder=SimpleNamespace(finalized=12),
        bars=[],
    )
    m = SessionMonitor(
        Incidents(store), CFG, session, tmp_path / "hb.json", "shadow", collector, engine, accounts
    )
    return m, store, session


def test_a_feed_must_stay_down_past_the_grace_period_to_be_an_incident(tmp_path):
    m, store, session = monitor(tmp_path)
    session.health.on_disconnected(at(0))
    m.tick(at(1))
    m.tick(at(CFG.monitor.feed_down_grace_s - 1))
    assert store.open_incidents() == []  # a short reconnect says nothing
    m.tick(at(CFG.monitor.feed_down_grace_s + 1))
    assert [i.kind for i in store.open_incidents()] == ["STOCK_FEED_DOWN"]
    session.health.on_connected(at(20))
    m.tick(at(22))
    assert store.open_incidents() == []


def test_the_heartbeat_is_written_atomically_and_carries_the_session_and_slot(tmp_path):
    m, _, _ = monitor(tmp_path)
    m.tick(at(0))
    hb = json.loads((tmp_path / "hb.json").read_text())
    assert hb["session"] == "2026-10-26" and hb["slot"] == 12 and hb["mode"] == "shadow"
    assert not list(tmp_path.glob("*.tmp"))


def test_engine_and_option_failures_become_incidents(tmp_path):
    engine = SimpleNamespace(error="slot 40: RuntimeError: boom")
    collector = SimpleNamespace(
        error="FatalStreamError: auth: 402",
        feed=SimpleNamespace(health=FeedHealth(pd.Timedelta(seconds=5))),
    )
    m, store, _ = monitor(tmp_path, collector=collector, engine=engine)
    m.tick(at(0))
    assert {i.kind for i in store.open_incidents()} == {"ENGINE_ERROR", "OPTION_FEED_FAILED"}


def test_accounts_report_limit_halts_blocks_errors_and_residual_exposure(tmp_path):
    def rt(halted=None, blocked=None, held=None):
        return SimpleNamespace(
            halted=halted,
            guard=SimpleNamespace(update=lambda *a: SimpleNamespace(loss=510.0)),
            realized_pnl=lambda: -100.0,
            risk=SimpleNamespace(daily_loss=SimpleNamespace(limit_usd=500)),
            orders=SimpleNamespace(
                entries_blocked=blocked,
                blind={},
                unresolved=lambda *a: [],
                cfg=SimpleNamespace(exit=SimpleNamespace(blind_alert_after_s=15)),
            ),
            store=SimpleNamespace(filled_by_symbol=lambda b: held or {}),
        )

    accounts = SimpleNamespace(
        errors=["tick acct:vpc: boom"],
        router=SimpleNamespace(
            runtimes={
                "acct:vpc": rt(halted="DAILY_LIMIT"),
                "acct:orb": rt(blocked="mismatch"),
                "acct:brt": rt(held={"SPY...": 3}),
            }
        ),
    )
    m, store, _ = monitor(tmp_path, accounts=accounts)
    m.tick(at(0))
    kinds = sorted((i.kind, i.account) for i in store.open_incidents())
    assert ("DAILY_LIMIT_HALT", "acct:vpc") in kinds and ("ENTRIES_BLOCKED", "acct:orb") in kinds
    assert not any(k == "RESIDUAL_EXPOSURE" for k, _ in kinds)  # not yet past the flat deadline
    m.tick(m.flat_deadline + pd.Timedelta(seconds=2))
    assert ("RESIDUAL_EXPOSURE", "acct:brt") in sorted(
        (i.kind, i.account) for i in store.open_incidents()
    )
    assert any(i.kind == "ACCOUNT_ERROR" for i in store.open_incidents())


def test_unknown_conditions_are_alerted_and_the_session_ends_with_a_summary(tmp_path):
    m, store, session = monitor(tmp_path)
    session.bars = [{"quarantined_trades": 2, "interval_status": "DATA_MISSING_OR_UNCERTAIN"}]
    m.tick(at(0))
    m.finish(at(5))
    _, sender, bot = build()
    bot.store = store
    bot.sender = sender
    bot.step(at(6))
    assert any("2 trade(s) quarantined" in t for t in sender.sent)
    assert any(t.startswith("Session 2026-10-26 (shadow): 12 slots") for t in sender.sent)


def test_without_a_database_the_engine_carries_on():
    class Broken:
        def open(self, *a, **k):
            raise ConnectionError("db down")

    inc = Incidents(Broken())
    assert inc.open(T0, "STOCK_FEED_DOWN", "x") is None and inc.failures == 1
    assert Incidents(None).open(T0, "STOCK_FEED_DOWN", "x") is None


def test_the_telegram_variables_are_loaded_from_env_with_the_mode_check(tmp_path):
    import os

    from tournament.secrets import (
        InsecureEnvFileError,
        MissingCredentialsError,
        load_telegram_credentials,
    )

    env = tmp_path / ".env"
    env.write_text("TELEGRAM_BOT_TOKEN=1:abc\nTELEGRAM_CHAT_ID=99\n")
    os.chmod(env, 0o644)
    with pytest.raises(InsecureEnvFileError):
        load_telegram_credentials(env)
    os.chmod(env, 0o600)
    c = load_telegram_credentials(env)
    assert (c.bot_token, c.chat_id) == ("1:abc", "99")
    env.write_text("TELEGRAM_BOT_TOKEN=\n")
    with pytest.raises(MissingCredentialsError):
        load_telegram_credentials(env)
