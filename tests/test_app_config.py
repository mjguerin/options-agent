"""The app config: validated, no defaults in code, switchable, and no settings left in code."""

import re
from pathlib import Path

import pytest
import yaml

from tournament import appconfig
from tournament.appconfig import AppConfigError, app_config, load_app_config
from tournament.marketdata.calendar import SessionDay
from tournament.marketdata.history import SessionGuardError, check_outside_session

SRC = Path(appconfig.__file__).parent


def edited(tmp_path, **sections):
    raw = yaml.safe_load(appconfig.DEFAULT_PATH.read_text())
    for name, values in sections.items():
        raw[name].update(values)
    p = tmp_path / "app.yaml"
    p.write_text(yaml.safe_dump(raw))
    return p


def test_the_default_file_loads_with_the_values_the_code_used_to_hold():
    c = load_app_config()
    assert c.underlying == "SPY" and c.exchange.timezone == "America/New_York"
    assert c.history.start_date.isoformat() == "2024-02-01"
    assert (c.history.universe.max_dte, c.history.universe.strike_pct) == (7, 0.05)
    assert c.history.trade_slice_minutes == 30 and c.history.parallel_requests == 8
    assert len(c.config_hash) == 64


@pytest.mark.parametrize(
    "section, values",
    [
        ("exchange", {"timezone": "Not/AZone"}),
        ("exchange", {"regular_close": "4pm"}),
        ("history", {"parallel_requests": 0}),
        ("accounts", {"surprise": "x"}),
    ],
)
def test_bad_values_stop_startup(tmp_path, section, values):
    with pytest.raises(AppConfigError):
        load_app_config(edited(tmp_path, **{section: values}))


def test_a_missing_value_stops_startup(tmp_path):
    raw = yaml.safe_load(appconfig.DEFAULT_PATH.read_text())
    del raw["database"]
    p = tmp_path / "a.yaml"
    p.write_text(yaml.safe_dump(raw))
    with pytest.raises(AppConfigError, match="database"):
        load_app_config(p)


def test_changing_the_file_changes_behaviour_with_no_code_change(tmp_path, monkeypatch):
    from datetime import UTC, date, datetime

    day = SessionDay(
        date(2026, 10, 26),
        datetime(2026, 10, 26, 13, 30, tzinfo=UTC),
        datetime(2026, 10, 26, 20, 0, tzinfo=UTC),
    )
    assert day.extended_start_utc == datetime(2026, 10, 26, 8, 0, tzinfo=UTC)  # 04:00 ET
    assert day.extended_end_utc == datetime(2026, 10, 27, 0, 0, tzinfo=UTC)  # close + 4 h
    base = yaml.safe_load(appconfig.DEFAULT_PATH.read_text())
    base["exchange"].update(extended_start="05:00", after_hours_hours=2)
    base["history"]["download_guard"].update(start="10:00")
    p = tmp_path / "other.yaml"
    p.write_text(yaml.safe_dump(base))
    monkeypatch.setenv(appconfig.ENV_VAR, str(p))
    assert app_config().exchange.after_hours_hours == 2
    assert day.extended_start_utc == datetime(2026, 10, 26, 9, 0, tzinfo=UTC)
    assert day.extended_end_utc == datetime(2026, 10, 26, 22, 0, tzinfo=UTC)
    at_930 = datetime(2026, 10, 26, 13, 30, tzinfo=UTC)  # 09:30 ET, a Monday
    check_outside_session(at_930)  # the guard now starts at 10:00
    with pytest.raises(SessionGuardError):
        check_outside_session(datetime(2026, 10, 26, 14, 30, tzinfo=UTC))


def test_no_endpoint_or_tunable_is_hard_coded_in_source():
    """URLs live in config. The paper broker host stays in code on purpose: it is the
    paper-only safety invariant (NFR-06)."""
    allowed_paper = SRC / "adapters" / "alpaca.py"
    offenders = []
    for path in SRC.rglob("*.py"):
        text = path.read_text()
        for m in re.finditer(r"""["'](wss?|https?)://[^"']+["']""", text):
            if path == allowed_paper and "paper-api.alpaca.markets" in m.group(0):
                continue
            offenders.append((path.name, m.group(0)))
    assert offenders == []
