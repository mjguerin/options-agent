"""The agent: config, read-only role and Postgres rules, the published view, the daily facts,
the headless narrative job (with a fake `claude`), and the rendered host files (AG-06..AG-13)."""

import importlib.util
import json
import os
import stat
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
import yaml
from test_dashboard import write_ledger

from tournament.agent.config import DEFAULT_PATH, AgentConfigError, load_agent_config
from tournament.agent.facts import build_facts, render_markdown, write_facts
from tournament.agent.render import claude_settings, narrative_args, render_agent
from tournament.agent.roles import grant_sql, hba_text
from tournament.agent.view import SecretLeak, publish_view
from tournament.alerts.store import incidents
from tournament.orders.store import memory_engine

CFG = load_agent_config()
ET_TZ = ZoneInfo("America/New_York")
REPO = Path(__file__).resolve().parents[1]


def edited(tmp_path, **sections):
    raw = yaml.safe_load(DEFAULT_PATH.read_text())
    for k, v in sections.items():
        raw[k].update(v)
    p = tmp_path / "agent.yaml"
    p.write_text(yaml.safe_dump(raw))
    return p


# ---- config -----------------------------------------------------------------------------------


def test_the_shipped_config_loads_and_hashes_stably():
    assert CFG.config_hash == load_agent_config().config_hash and len(CFG.config_hash) == 64


@pytest.mark.parametrize(
    "section,change",
    [
        ("host", {"os_user": "agent; drop"}),
        ("host", {"db_role": "someone_else"}),  # peer authentication needs the names to match
        ("host", {"view_dir": "/etc"}),
        ("host", {"narratives_dir": "../out"}),
        ("view", {"publish_time": "5pm"}),
        ("narrative", {"max_turns": 0}),
    ],
)
def test_bad_values_are_refused(tmp_path, section, change):
    with pytest.raises(AgentConfigError):
        load_agent_config(edited(tmp_path, **{section: change}))


def test_an_unknown_key_is_refused(tmp_path):
    with pytest.raises(AgentConfigError):
        load_agent_config(edited(tmp_path, narrative={"surprise": 1}))


def test_the_narrative_job_has_no_shell_network_or_write_tools():
    allowed = set(CFG.narrative.allowed_tools)
    assert allowed <= {"Read", "Grep", "Glob"}
    assert {"Bash", "Write", "Edit", "WebFetch", "WebSearch"} <= set(CFG.narrative.disallowed_tools)
    assert not allowed & set(CFG.narrative.disallowed_tools)


# ---- the database role and access rules (AC-24) ----------------------------------------------


def test_the_role_can_only_select_and_is_read_only():
    sql = grant_sql(CFG)
    assert "default_transaction_read_only = on" in sql and "NOSUPERUSER" in sql
    assert "GRANT SELECT ON ALL TABLES" in sql
    for verb in ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "ALL PRIVILEGES", "CREATE ROLE ,"):
        assert verb not in sql.replace("REVOKE CREATE", "")
    assert f"FOR ROLE {CFG.host.owner_db_role} IN SCHEMA public GRANT SELECT" in sql


def test_the_access_rules_use_peer_authentication_and_close_tcp():
    rules = [r.split() for r in hba_text(CFG).splitlines() if r and not r.startswith("#")]
    assert not any("trust" in r for r in rules)
    local = [r for r in rules if r[0] == "local"]
    assert {(r[2], r[3]) for r in local} == {
        (CFG.host.owner_db_role, "peer"),
        (CFG.host.db_role, "peer"),
    } | {(CFG.host.owner_db_role, "peer")}
    assert all(r[-1] == "reject" for r in rules if r[0] == "host")
    assert not any(r[1] == "all" and r[2] == "all" for r in local)


# ---- the published view -----------------------------------------------------------------------


def make_repo(tmp_path, secret="s3cr3t-token-value"):
    repo = tmp_path / "repo"
    (repo / "specs").mkdir(parents=True)
    (repo / "src" / "pkg" / "__pycache__").mkdir(parents=True)
    (repo / "config").mkdir()
    (repo / "specs" / "rules.md").write_text("the rules")
    (repo / "src" / "pkg" / "a.py").write_text("x = 1")
    (repo / "src" / "pkg" / "__pycache__" / "a.pyc").write_bytes(b"\x00")
    (repo / "src" / "pkg" / "big.bin").write_bytes(b"0" * (6 * 1024 * 1024))
    (repo / "src" / "pkg" / "data.parquet").write_bytes(b"PAR1")
    (repo / "README.md").write_text("readme")
    (repo / ".env").write_text(f"ALPACA_SECRET={secret}\nSHORT=abc\n")
    (repo / "data" / "history").mkdir(parents=True)
    (repo / "data" / "history" / "spy.parquet").write_bytes(b"PAR1")
    write_ledger(repo, "2026-10-26", "acct:orb", 10.0)
    return repo


def test_the_view_copies_reading_material_and_never_secrets_or_history(tmp_path):
    repo = make_repo(tmp_path)
    dest = tmp_path / "shared" / "view"
    manifest = publish_view(repo, CFG, dest, None, repo / "data" / "live")
    got = {str(p.relative_to(dest)) for p in dest.rglob("*") if p.is_file()}
    assert {"specs/rules.md", "src/pkg/a.py", "README.md", "VIEW_MANIFEST.json"} <= got
    assert any(g.startswith("data/live/2026-10-26/shadow/ledger/") for g in got)
    assert not any(".env" in g or g.endswith((".pyc", ".parquet")) for g in got)
    assert "src/pkg/big.bin" in manifest["skipped_too_large"] and "src/pkg/big.bin" not in got
    assert not (dest / "data" / "history").exists()
    mode = stat.S_IMODE((dest / "specs" / "rules.md").stat().st_mode)
    assert mode == 0o644 and stat.S_IMODE(dest.stat().st_mode) == 0o755


def test_a_secret_value_in_a_copied_file_stops_the_publish(tmp_path):
    repo = make_repo(tmp_path)
    dest = tmp_path / "shared" / "view"
    publish_view(repo, CFG, dest, None, repo / "data" / "live")  # a good view is in place
    (repo / "specs" / "oops.md").write_text("key is s3cr3t-token-value, do not share")
    with pytest.raises(SecretLeak):
        publish_view(repo, CFG, dest, None, repo / "data" / "live")
    assert (dest / "specs" / "rules.md").exists() and not (dest / "specs" / "oops.md").exists()
    assert not dest.with_name("view.new").exists()  # the half-built copy is removed


def test_a_republish_replaces_the_view_without_leaving_the_old_one(tmp_path):
    repo = make_repo(tmp_path)
    dest = tmp_path / "shared" / "view"
    publish_view(repo, CFG, dest, None, repo / "data" / "live")
    (repo / "specs" / "rules.md").write_text("new rules")
    (repo / "specs" / "gone.md").write_text("x")
    publish_view(repo, CFG, dest, None, repo / "data" / "live")
    (repo / "specs" / "gone.md").unlink()
    publish_view(repo, CFG, dest, None, repo / "data" / "live")
    assert (dest / "specs" / "rules.md").read_text() == "new rules"
    assert not (dest / "specs" / "gone.md").exists()
    assert not dest.with_name("view.old").exists()


# ---- facts ------------------------------------------------------------------------------------


def seed_incident(engine, opened: str, kind="STOCK_FEED_DOWN"):
    with engine.begin() as c:
        c.execute(
            incidents.insert().values(
                dedupe_key=f"{kind}:{opened}", kind=kind, severity="warning", component="live",
                account=None, detail="d", opened_at=pd.Timestamp(opened, tz="UTC").to_pydatetime(),
            )
        )  # fmt: skip


def test_facts_are_deterministic_and_come_from_the_ledgers_and_incidents(tmp_path):
    write_ledger(tmp_path, "2026-10-26", "acct:orb", 100.0)
    write_ledger(tmp_path, "2026-10-26", "acct:vpc", -20.0)
    write_ledger(tmp_path, "2026-10-27", "acct:orb", 5.0)
    engine = memory_engine()
    seed_incident(engine, "2026-10-26T15:00:00")  # 11:00 ET on the 26th
    seed_incident(engine, "2026-10-27T15:00:00")  # another day: not in these facts
    live = tmp_path / "data" / "live"
    a = build_facts(date(2026, 10, 26), live, engine, ET_TZ, 10)
    b = build_facts(date(2026, 10, 26), live, engine, ET_TZ, 10)
    assert a == b and len(a["content_hash"]) == 64
    assert a["has_session"] and len(a["today"]) == 2
    assert [i["kind"] for i in a["incidents"]] == ["STOCK_FEED_DOWN"]
    assert a["sessions_covered"] == ["2026-10-26"]  # nothing after the day leaks in
    orb = next(r for r in a["cumulative"] if r["account"] == "acct:orb")
    assert orb["net realized $"] == 100.0
    other = build_facts(date(2026, 10, 27), live, engine, ET_TZ, 10)
    assert other["content_hash"] != a["content_hash"] and other["sessions_covered"] == [
        "2026-10-26", "2026-10-27"
    ]  # fmt: skip


def test_a_day_without_a_ledger_says_so(tmp_path):
    f = build_facts(date(2026, 10, 26), tmp_path / "none", None, ET_TZ, 10)
    assert not f["has_session"] and f["today"] == [] and f["incidents"] == []
    assert "No session ledger" in render_markdown(f)


def test_facts_are_written_as_json_and_markdown(tmp_path):
    write_ledger(tmp_path, "2026-10-26", "acct:orb", 100.0)
    f = build_facts(date(2026, 10, 26), tmp_path / "data" / "live", None, ET_TZ, 10)
    paths = write_facts(f, tmp_path / "facts")
    assert {p.name for p in paths} == {"2026-10-26.json", "2026-10-26.md"}
    assert json.loads(paths[0].read_text())["content_hash"] == f["content_hash"]
    assert "acct:orb" in paths[1].read_text()


# ---- the headless narrative job ---------------------------------------------------------------


def load_runner():
    spec = importlib.util.spec_from_file_location(
        "run_narrative", REPO / "ops/agent/run_narrative.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


FAKE_CLAUDE = """#!/bin/sh
echo "$@" > "$(dirname "$0")/argv.txt"
{ if [ "$FAKE_MODE" = "fail" ]; then exit 3; fi; }
if [ "$FAKE_MODE" = "short" ]; then T="too short"; else T="$(python3 -c "print('word ' * 120)")"; fi
echo '{"type":"assistant","message":"reading facts"}'
echo "{\\"type\\":\\"result\\",\\"is_error\\":false,\\"result\\":\\"$T\\"}"
"""


@pytest.fixture
def job(tmp_path):
    view = tmp_path / "view"
    (view / "facts").mkdir(parents=True)
    (view / "config" / "agent").mkdir(parents=True)
    (view / "config" / "agent" / "p.md").write_text("Write the note for FACTS_DATE.")
    write_ledger(tmp_path, "2026-10-26", "acct:orb", 100.0)
    f = build_facts(date(2026, 10, 26), tmp_path / "data" / "live", None, ET_TZ, 10)
    write_facts(f, view / "facts")
    claude = tmp_path / "claude"
    claude.write_text(FAKE_CLAUDE)
    claude.chmod(0o755)

    def args(day="2026-10-26", mode=""):
        os.environ["FAKE_MODE"] = mode
        return load_runner().parser().parse_args(
            ["--view", str(view), "--out", str(tmp_path / "out"), "--logs", str(tmp_path / "logs"),
             "--claude", str(claude), "--model", "sonnet", "--max-turns", "3", "--timeout", "30",
             "--prompt-file", "config/agent/p.md", "--output-format", "stream-json",
             "--min-words", "80", "--config-hash", "abc", "--tz", "America/New_York",
             "--date", day, "--allowed", "Read", "Grep", "--disallowed", "Bash", "Write"]
        )  # fmt: skip

    yield tmp_path, args
    os.environ.pop("FAKE_MODE", None)


def test_the_narrative_is_written_with_its_sources_and_the_tool_log_is_kept(job):
    tmp, args = job
    assert load_runner().run(args()) == 0
    text = (tmp / "out" / "2026-10-26.md").read_text()
    facts = json.loads((tmp / "view" / "facts" / "2026-10-26.json").read_text())
    assert text.startswith("---\n") and f"facts_content_hash: {facts['content_hash']}" in text
    assert "agent_config_hash: abc" in text and text.count("word") >= 100
    argv = (tmp / "argv.txt").read_text()
    assert "--allowedTools Read,Grep" in argv and "--disallowedTools Bash,Write" in argv
    assert "Write the note for 2026-10-26." in argv
    assert (tmp / "logs" / "narrative-2026-10-26.jsonl").exists()


def test_a_missing_or_failed_run_leaves_no_narrative_and_a_nonzero_exit(job):
    tmp, args = job
    assert load_runner().run(args(day="2026-10-20")) == 2  # no facts for that day
    assert load_runner().run(args(mode="short")) == 4  # too little to publish
    assert load_runner().run(args(mode="fail")) == 4
    assert not (tmp / "out").exists() or not list((tmp / "out").glob("*.md"))


def test_a_day_without_a_session_is_not_an_error(job, tmp_path):
    tmp, args = job
    f = build_facts(date(2026, 10, 28), tmp / "none", None, ET_TZ, 10)
    write_facts(f, tmp / "view" / "facts")
    assert load_runner().run(args(day="2026-10-28")) == 0
    assert not (tmp / "out" / "2026-10-28.md").exists()


def test_the_runner_has_no_third_party_imports():
    tree = (REPO / "ops/agent/run_narrative.py").read_text()
    for name in ("yaml", "pandas", "pydantic", "tournament"):
        assert f"import {name}" not in tree and f"from {name}" not in tree


# ---- the rendered host files ------------------------------------------------------------------


def test_the_host_files_render_complete_and_valid(tmp_path):
    paths = render_agent(
        CFG, "America/New_York", "mgmini", Path("/Users/mgmini"), REPO, "/opt/homebrew/bin/uv",
        tmp_path,
    )  # fmt: skip
    assert {p.name for p in paths} == {
        "agent-narrative.plist", "agent-view.plist", "claude-settings.json",
        "agent-grants.sql", "pg_hba.conf",
    }  # fmt: skip
    for name in ("agent-narrative.plist", "agent-view.plist"):
        root = ET.parse(tmp_path / name).getroot()
        assert root.tag == "plist" and "__" not in (tmp_path / name).read_text()
    narrative = (tmp_path / "agent-narrative.plist").read_text()
    assert f"<string>{CFG.host.os_user}</string>" in narrative
    assert "<string>--disallowed</string>" in narrative and "<string>Bash</string>" in narrative
    assert narrative.count("<key>Weekday</key>") == 5 and "<integer>17</integer>" in narrative
    view_job = (tmp_path / "agent-view.plist").read_text()
    assert "<string>mgmini</string>" in view_job and "<string>publish</string>" in view_job
    settings = json.loads((tmp_path / "claude-settings.json").read_text())
    assert "Write" in settings["permissions"]["deny"]
    assert "Bash(psql:*)" in settings["permissions"]["allow"]


def test_the_job_arguments_come_from_the_config():
    args = narrative_args(CFG, "America/New_York")
    assert args[:2] == [CFG.host.python, str(CFG.shared("view_dir") / "ops/agent/run_narrative.py")]
    assert args[args.index("--model") + 1] == CFG.narrative.model
    assert args[args.index("--config-hash") + 1] == CFG.config_hash
    assert claude_settings(CFG)["permissions"]["allow"] == CFG.interactive.allowed_tools


def test_the_narrative_runner_script_runs_standalone(tmp_path):
    done = subprocess.run(
        [sys.executable, "-I", str(REPO / "ops/agent/run_narrative.py"), "--help"],
        capture_output=True, text=True,
    )  # fmt: skip
    assert done.returncode == 0 and "--view" in done.stdout
