"""Render the agent's job files and Claude Code settings from config/agent (D-59).

Nothing here touches the system. `ops/host/setup_agent.sh` installs the output.
"""

from __future__ import annotations

import json
from pathlib import Path
from xml.sax.saxutils import escape

from tournament.agent.config import AgentConfig
from tournament.agent.roles import grant_sql, hba_text
from tournament.ops.render import calendar_xml

TEMPLATES = Path(__file__).resolve().parents[3] / "ops" / "agent" / "templates"
RUNNER = "ops/agent/run_narrative.py"


def _args_xml(args: list[str]) -> str:
    return "\n".join(f"\t\t<string>{escape(a)}</string>" for a in args)


def narrative_args(cfg: AgentConfig, tz: str) -> list[str]:
    n, view = cfg.narrative, cfg.shared("view_dir")
    return [
        cfg.host.python, str(view / RUNNER),
        "--view", str(view), "--out", str(cfg.shared("narratives_dir")),
        "--logs", str(cfg.shared("agent_logs_dir")), "--claude", cfg.host.claude_bin,
        "--model", n.model, "--max-turns", str(n.max_turns), "--timeout", str(n.timeout_s),
        "--prompt-file", n.prompt_file, "--output-format", n.output_format,
        "--min-words", str(n.min_words), "--config-hash", cfg.config_hash, "--tz", tz,
        "--audit", str(cfg.audit_path),
        "--transcripts", f"/Users/{cfg.host.os_user}/.claude/projects",
        "--allowed", *n.allowed_tools, "--disallowed", *n.disallowed_tools,
    ]  # fmt: skip


def claude_settings(cfg: AgentConfig) -> dict:
    """The agent's own Claude Code permissions: an allowlist of read tools, everything else off."""
    i = cfg.interactive
    return {"permissions": {"allow": i.allowed_tools, "deny": i.disallowed_tools}}


def _fill(tmpl: str, values: dict[str, str]) -> str:
    text = (TEMPLATES / tmpl).read_text()
    for k, v in values.items():
        text = text.replace(k, v)
    left = [w for w in text.split() if w.startswith("__") and w.endswith("__")]
    if left:
        raise ValueError(f"{tmpl}: unfilled placeholders {left}")
    return text


def render_agent(
    cfg: AgentConfig, tz: str, owner: str, home: Path, repo: Path, uv: str, out: Path
) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    agent_home = f"/Users/{cfg.host.os_user}"
    shared = cfg.host.shared_root
    files = {
        "agent-narrative.plist": _fill(
            "agent-narrative.plist.tmpl",
            {
                "__AGENT_USER__": cfg.host.os_user,
                "__AGENT_HOME__": agent_home,
                "__VIEW__": str(cfg.shared("view_dir")),
                "__SHARED__": shared,
                "__ARGS__": _args_xml(narrative_args(cfg, tz)),
                "__CALENDAR__": calendar_xml(cfg.narrative.start_time, cfg.narrative.weekdays_only),
            },
        ),
        "agent-view.plist": _fill(
            "agent-view.plist.tmpl",
            {
                "__USER__": owner,
                "__HOME__": str(home),
                "__REPO__": str(repo),
                "__UV__": uv,
                "__LOGS__": str(home / "Library" / "Logs" / "options-tournament"),
                "__VIEW_CALENDAR__": calendar_xml(cfg.view.publish_time, cfg.view.weekdays_only),
            },
        ),
        "claude-settings.json": json.dumps(claude_settings(cfg), indent=2) + "\n",
        "agent-grants.sql": grant_sql(cfg),
        "pg_hba.conf": hba_text(cfg),
    }
    paths = []
    for name, text in files.items():
        (out / name).write_text(text)
        paths.append(out / name)
    return paths
