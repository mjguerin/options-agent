"""Daily jobs: when the engine runs, what the nightly job checks, and the rendered host files."""

import configparser
import plistlib
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
import yaml

from tournament import appconfig
from tournament.appconfig import load_app_config
from tournament.live.runner import PreflightFailed, unique_run_dir
from tournament.marketdata.calendar import SessionDay
from tournament.marketdata.history import DATASETS
from tournament.ops import engine as engine_job
from tournament.ops.nightly import coverage
from tournament.ops.render import calendar_xml, render_all
from tournament.ops.schedule import Action, engine_plan, start_moment

CFG = load_app_config()
DAY = SessionDay(
    date(2026, 10, 26),
    pd.Timestamp("2026-10-26T13:30:00Z").to_pydatetime(),
    pd.Timestamp("2026-10-26T20:00:00Z").to_pydatetime(),
)


def utc(s):
    return pd.Timestamp(s, tz="UTC")


# ---- the engine's schedule ----------------------------------------------------------------------


def test_the_start_moment_is_the_configured_exchange_time():
    assert start_moment(DAY, CFG) == utc("2026-10-26T07:55:00")  # 03:55 ET (EDT) is 07:55 UTC


@pytest.mark.parametrize(
    "now, action",
    [
        ("2026-10-26T07:40:00", Action.WAIT),  # launchd fired early
        ("2026-10-26T07:55:00", Action.RUN),
        ("2026-10-26T15:10:00", Action.RUN),  # a restart mid-session
        ("2026-10-26T20:00:00", Action.SKIP_TOO_LATE),
        ("2026-10-26T21:30:00", Action.SKIP_TOO_LATE),
    ],
)
def test_the_engine_waits_runs_or_skips_by_the_clock(now, action):
    assert engine_plan(utc(now), DAY, CFG).action is action


def test_a_day_without_a_session_is_skipped():
    assert engine_plan(utc("2026-10-26T08:00:00"), None, CFG).action is Action.SKIP_NOT_A_SESSION


def test_the_wait_length_is_exact():
    p = engine_plan(utc("2026-10-26T07:50:00"), DAY, CFG)
    assert p.wait_s == 300


def test_the_start_time_is_a_config_value(tmp_path):
    raw = yaml.safe_load(appconfig.DEFAULT_PATH.read_text())
    raw["engine"]["start_time"] = "04:30"
    p = tmp_path / "a.yaml"
    p.write_text(yaml.safe_dump(raw))
    assert start_moment(DAY, load_app_config(p)) == utc("2026-10-26T08:30:00")


# ---- run_daily, with the network and the session faked ----


class Clock:
    def __init__(self, t):
        self.t, self.slept = utc(t), []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += pd.Timedelta(seconds=s)


@pytest.fixture()
def faked(monkeypatch):
    seen = {}

    class Cal:
        def __init__(self, creds):
            pass

        def calendar(self, a, b):
            return seen["days"]

    monkeypatch.setattr("tournament.adapters.alpaca_history.AlpacaHistory", Cal)
    monkeypatch.setattr("tournament.secrets.load_alpaca_credentials", lambda alias: object())
    monkeypatch.setattr(
        "tournament.live.runner.run_session", lambda *a, **k: seen.setdefault("ran", a)
    )
    seen["days"] = [DAY]
    return seen


def test_run_daily_waits_then_runs_the_session(faked, tmp_path):
    clock = Clock("2026-10-26T07:30:00")
    code = engine_job.run_daily(Path("."), tmp_path / "live", clock.now, clock.sleep)
    assert code == 0 and faked["ran"][1] == "shadow"
    assert sum(clock.slept) >= 25 * 60 and max(clock.slept) <= engine_job.WAIT_SLICE_S


def test_run_daily_skips_a_non_session_day_after_holding_for_supervisor(faked, tmp_path):
    faked["days"] = []
    clock = Clock("2026-10-26T07:30:00")
    assert engine_job.run_daily(Path("."), tmp_path / "live", clock.now, clock.sleep) == 0
    assert "ran" not in faked and clock.slept == [CFG.engine.startsecs + 2]


def test_a_failed_preflight_exits_with_code_3_so_no_restart_loop(faked, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise PreflightFailed({"failures": [["config_risk", "", True]]})

    monkeypatch.setattr("tournament.live.runner.run_session", boom)
    clock = Clock("2026-10-26T08:00:00")
    assert engine_job.run_daily(Path("."), tmp_path / "live", clock.now, clock.sleep) == 3


# ---- the nightly job ---------------------------------------------------------------------------


def test_coverage_names_what_is_missing_per_session():
    class Store:
        def has(self, name, d):
            return not (d == date(2026, 10, 23) and name in ("spy_trades", "option_bars_1m"))

    assert coverage(Store(), [date(2026, 10, 22), date(2026, 10, 23)]) == {
        "2026-10-23": ["spy_trades", "option_bars_1m"]
    }
    assert set(DATASETS) >= {"spy_trades", "option_bars_1m"}


def test_a_restart_never_overwrites_the_run_before_it(tmp_path):
    d1 = unique_run_dir(tmp_path, "shadow")
    (d1 / "stream.jsonl").write_text("{}")
    d2 = unique_run_dir(tmp_path, "shadow")
    (d2 / "stream.jsonl").write_text("{}")
    assert (d1.name, d2.name, unique_run_dir(tmp_path, "shadow").name) == (
        "shadow",
        "shadow-r2",
        "shadow-r3",
    )


# ---- the rendered host files ---------------------------------------------------------------------


@pytest.fixture()
def rendered(tmp_path):
    files = render_all(
        CFG, "mgmini", Path("/Users/mgmini"), Path("/Users/mgmini/options-agent"),
        "/opt/homebrew/bin/uv", tmp_path,
    )  # fmt: skip
    return {f.name: f for f in files}


def test_every_job_file_renders_without_placeholders(rendered):
    assert set(rendered) == {
        "supervisord.conf",
        "supervisor.plist",
        "engine-start.plist",
        "nightly.plist",
    }
    for f in rendered.values():
        assert "__" not in f.read_text().replace("__main__", "")


@pytest.mark.parametrize("program, cmd", [("alerts", "alerts run"), ("api", "api run")])
def test_the_alert_bot_and_the_api_run_all_day_and_restart_themselves(rendered, program, cmd):
    cp = configparser.RawConfigParser()
    cp.read(rendered["supervisord.conf"])
    p = cp[f"program:{program}"]
    assert p["autostart"] == "true" and p["autorestart"] == "true"
    assert f"tournament {cmd}" in p["command"]


def test_the_engine_program_follows_the_app_config(rendered):
    cp = configparser.RawConfigParser()
    cp.read(rendered["supervisord.conf"])
    p = cp["program:engine"]
    assert p["startsecs"] == str(CFG.engine.startsecs) and p["startretries"] == "3"
    assert p["autostart"] == "false" and p["autorestart"] == "unexpected"
    assert p["exitcodes"] == "0,3" and "tournament engine daily" in p["command"]
    assert cp["unix_http_server"]["file"].endswith("data/run/supervisor.sock")


def test_launchd_starts_the_engine_on_weekdays_at_the_launch_time(rendered):
    plist = plistlib.loads(rendered["engine-start.plist"].read_bytes())
    days = plist["StartCalendarInterval"]
    assert [d["Weekday"] for d in days] == [1, 2, 3, 4, 5]
    assert {(d["Hour"], d["Minute"]) for d in days} == {(3, 50)}
    assert plist["ProgramArguments"][-2:] == ["start", "engine"] and plist["UserName"] == "mgmini"


def test_the_nightly_job_runs_after_the_download_guard_ends(rendered):
    plist = plistlib.loads(rendered["nightly.plist"].read_bytes())
    assert {(d["Hour"], d["Minute"]) for d in plist["StartCalendarInterval"]} == {(16, 45)}
    assert plist["ProgramArguments"][-3:] == ["tournament", "ops", "nightly"]
    assert CFG.history.download_guard.end == "16:30"  # the job starts after the guard ends


def test_the_supervisor_daemon_is_kept_alive_and_separate_from_homebrews(rendered):
    plist = plistlib.loads(rendered["supervisor.plist"].read_bytes())
    assert plist["KeepAlive"] is True and plist["RunAtLoad"] is True
    assert "/data/run/supervisord.conf" in plist["ProgramArguments"][-1]
    assert "/opt/homebrew/etc" not in "".join(plist["ProgramArguments"])


def test_weekday_calendar_can_be_every_day():
    assert calendar_xml("03:50", False).count("<dict>") == 1
