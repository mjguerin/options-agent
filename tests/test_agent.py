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
             "--audit", str(tmp_path / "audit" / "a.jsonl"),
             "--transcripts", str(tmp_path / "projects"),
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
    assert "Bash" in settings["permissions"]["deny"] and "Bash(psql:*)" not in str(settings)


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


def test_the_trading_code_never_imports_the_agent():  # AC-14
    """Nothing the engine, orders, risk or alerts need can depend on the agent. Only the CLI may
    reach it, and only inside its own command."""
    src = REPO / "src" / "tournament"
    for f in src.rglob("*.py"):
        if "agent" in f.relative_to(src).parts[:1] or f.name == "cli.py":
            continue
        text = f.read_text()
        assert "tournament.agent" not in text, f"{f.relative_to(src)} reaches the agent"


# ---- audit, denial tests and the versioned view (AC-13, AG-08, D-84) --------------------------


def load_module(name):
    spec = importlib.util.spec_from_file_location(name, REPO / f"ops/agent/{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(REPO / "ops/agent"))
    spec.loader.exec_module(mod)
    return mod


def stream(*events):
    return [json.dumps(e) for e in events]


def tool_use(i, name, **inp):
    item = {"type": "tool_use", "id": i, "name": name, "input": inp}
    return {"type": "assistant", "message": {"content": [item]}}


def tool_result(i, text, error=False):
    item = {"type": "tool_result", "tool_use_id": i, "content": text, "is_error": error}
    return {"type": "user", "message": {"content": [item]}}


def test_every_attempted_call_is_audited_with_its_outcome_and_no_content():
    audit = load_module("audit")
    lines = stream(
        tool_use("1", "Read", file_path="/v/specs/a.md"),
        tool_result("1", "SECRET FILE CONTENT"),
        tool_use("2", "Bash", command="touch /v/x"),
        tool_result("2", "Permission to use Bash has been denied", error=True),
        tool_use("3", "WebFetch", url="https://example.com/" + "a" * 500),
        tool_result("3", "boom", error=True),
        tool_use("4", "Grep", pattern="x"),
    )
    r = audit.records(lines, "narrative-job", "2026-10-26")
    assert [(x["tool"], x["action_class"], x["outcome"]) for x in r] == [
        ("Read", "read", "allowed"), ("Bash", "exec", "denied"),
        ("WebFetch", "network", "error"), ("Grep", "search", "no_result"),
    ]  # fmt: skip
    assert r[1]["target"] == "touch /v/x" and "denied" in r[1]["reason"]
    assert len(r[2]["target"]) <= 200
    assert "SECRET" not in json.dumps(r)  # outputs are never copied into the audit
    assert all(x["actor"] == "narrative-job" and x["at"] for x in r)


def test_interactive_transcripts_are_swept_once_each(tmp_path):
    audit = load_module("audit")
    projects = tmp_path / "projects" / "p"
    projects.mkdir(parents=True)
    t = projects / "s1.jsonl"
    t.write_text("\n".join(stream(tool_use("a", "Read", file_path="/v/x"), tool_result("a", "ok"))))
    state, out = tmp_path / "state.json", tmp_path / "audit.jsonl"
    assert audit.sweep_transcripts(tmp_path / "projects", state, out, "interactive-session") == 1
    assert audit.sweep_transcripts(tmp_path / "projects", state, out, "interactive-session") == 0
    t.write_text(
        t.read_text() + "\n" + "\n".join(stream(tool_use("b", "Edit", file_path="/v/y"),
                                                  tool_result("b", "not allowed", error=True)))
    )  # fmt: skip
    assert audit.sweep_transcripts(tmp_path / "projects", state, out, "interactive-session") == 1
    rows = [json.loads(x) for x in out.read_text().splitlines()]
    assert [r["outcome"] for r in rows] == ["allowed", "denied"]
    assert {r["actor"] for r in rows} == {"interactive-session"}


def test_the_narrative_job_writes_its_audit_trail(job):
    tmp, args = job
    assert load_runner().run(args()) == 0
    # the fake claude issues no tool calls, so the audit exists only if there was something to log
    assert not (tmp / "audit" / "a.jsonl").exists() or (tmp / "audit" / "a.jsonl").read_text() == ""


def test_the_owners_interactive_sessions_have_no_shell_and_no_writes():
    i = CFG.interactive
    assert set(i.allowed_tools) <= {"Read", "Grep", "Glob"}
    assert {"Bash", "Write", "Edit", "WebFetch", "WebSearch"} <= set(i.disallowed_tools)
    assert "Bash" in CFG.narrative.disallowed_tools


DENIAL_CLAUDE = """#!__PY__
import json, os
mode = os.environ["FAKE_MODE"]


def use(i, name, **inp):
    print(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": i, "name": name, "input": inp}]}}))


def result(i, text, error):
    print(json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": i, "content": text, "is_error": error}]}}))


if mode in ("refuse", "leak", "withheld"):
    on = ["Read", "Bash", "Write", "WebFetch"]
    tools = ["Read", "Grep", "Glob"] if mode == "withheld" else on
    print(json.dumps({"type": "system", "subtype": "init", "tools": tools}))
if mode in ("refuse", "withheld"):
    print(json.dumps({"type": "result", "is_error": False, "result": "I cannot do that."}))
elif mode == "leak":
    use("z", "Bash", command="x")
    result("z", "ok", False)
else:
    for i, (name, inp) in enumerate([("Bash", {"command": "x"}), ("Write", {"file_path": "f"}),
                                     ("WebFetch", {"url": "u"}), ("Read", {"file_path": "e"})]):
        use(str(i), name, **inp)
        result(str(i), "Permission denied", True)
"""


def denial_args(tmp_path, mode):
    view = tmp_path / "view"
    (view / "config" / "risk").mkdir(parents=True, exist_ok=True)
    (view / "config" / "risk" / "risk-v1.yaml").write_text("loss_limit: 500\n")
    claude = tmp_path / "claude"
    claude.write_text(DENIAL_CLAUDE.replace("__PY__", sys.executable))
    claude.chmod(0o755)
    os.environ["FAKE_MODE"] = mode
    return load_module("verify_denials").parser().parse_args(
        ["--view", str(view), "--claude", str(claude), "--model", "sonnet", "--timeout", "30",
         "--audit", str(tmp_path / "audit.jsonl"), "--owner-env", "/o/.env",
         "--allowed", "Read", "Grep", "--disallowed", "Bash", "Write", "Edit", "WebFetch"]
    )  # fmt: skip


def test_the_negative_tests_pass_when_every_privileged_call_is_refused_and_audited(
    tmp_path, capsys
):
    mod = load_module("verify_denials")
    assert mod.run(denial_args(tmp_path, "denied")) == 0
    out = capsys.readouterr().out
    assert out.count("PASS") == 5 and "FAIL" not in out
    rows = [json.loads(x) for x in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert rows and {r["actor"] for r in rows} == {"denial-test"}
    assert {r["outcome"] for r in rows} == {"denied"}


def test_a_privileged_call_that_goes_through_fails_the_negative_tests(tmp_path, capsys):
    assert load_module("verify_denials").run(denial_args(tmp_path, "leak")) == 1
    assert "FAIL  run a shell command" in capsys.readouterr().out


def test_a_model_that_only_declines_with_the_tool_on_offer_is_inconclusive(tmp_path, capsys):
    assert load_module("verify_denials").run(denial_args(tmp_path, "refuse")) == 0
    out = capsys.readouterr().out
    assert out.count("INCONCLUSIVE") == 5 and "PASS" not in out


def test_a_tool_that_is_never_offered_passes_structurally(tmp_path, capsys):
    assert load_module("verify_denials").run(denial_args(tmp_path, "withheld")) == 0
    out = capsys.readouterr().out
    assert out.count("not offered to the model") == 4  # shell, two writes, the web
    assert "INCONCLUSIVE  read the broker credentials" in out  # Read is offered; it must be tried


def test_each_published_view_is_versioned_and_leaves_a_history_line(tmp_path):
    repo = make_repo(tmp_path)
    dest = tmp_path / "shared" / "view"
    version = {"git_commit": "abc123", "git_dirty": False, "published_at": "2026-10-26T21:00:00Z"}
    facts = tmp_path / "facts"
    facts.mkdir()
    (facts / "2026-10-26.json").write_text(json.dumps({"content_hash": "h1"}))
    m = publish_view(repo, CFG, dest, facts, repo / "data" / "live", version)
    assert m["git_commit"] == "abc123" and m["facts_content_hashes"] == {"2026-10-26": "h1"}
    publish_view(repo, CFG, dest, facts, repo / "data" / "live", {**version, "git_commit": "def"})
    lines = (dest.parent / "view-history.jsonl").read_text().splitlines()
    assert [json.loads(x)["git_commit"] for x in lines] == ["abc123", "def"]
    assert json.loads((dest / "VIEW_MANIFEST.json").read_text())["git_commit"] == "def"
