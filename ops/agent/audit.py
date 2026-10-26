"""Audit records for every tool call the agent attempts (AC-13, AG-08).

Standard library only: it runs as the agent's account. One line per attempted call: who, what
class of action, the target, the outcome and the reason, with a time. Tool outputs and file
contents are never copied, and targets are cut short, so the audit file cannot leak a secret it
was never shown. A denied call is recorded the same way as an allowed one.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

CLASSES = {
    "Read": "read", "Grep": "search", "Glob": "search", "Bash": "exec", "Write": "write",
    "Edit": "write", "NotebookEdit": "write", "WebFetch": "network", "WebSearch": "network",
    "Task": "delegate",
}  # fmt: skip
DENIAL_WORDS = ("permission", "denied", "not allowed", "disallowed", "not permitted", "blocked")
TARGET_MAX, REASON_MAX = 200, 120


def _target(name: str, inp: dict) -> str:
    for key in ("file_path", "path", "pattern", "command", "url", "query", "notebook_path"):
        if key in inp:
            return str(inp[key])[:TARGET_MAX]
    return ""


def _result_text(content) -> str:
    if isinstance(content, list):
        return " ".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
    return str(content or "")


def records(lines, actor: str, session: str | None = None, now: str | None = None) -> list[dict]:
    """Audit records from a stream of Claude Code events (a -p stream-json log or a transcript)."""
    calls: dict[str, dict] = {}
    order: list[str] = []
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        msg = ev.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "tool_use":
                name = item.get("name", "")
                calls[item["id"]] = {
                    "at": ev.get("timestamp") or now or datetime.now(UTC).isoformat(),
                    "actor": actor,
                    "session": session,
                    "tool": name,
                    "action_class": CLASSES.get(name, "other"),
                    "target": _target(name, item.get("input") or {}),
                    "outcome": "no_result",
                    "reason": "",
                    "call_id": item["id"],
                }
                order.append(item["id"])
            elif item.get("type") == "tool_result" and item.get("tool_use_id") in calls:
                rec = calls[item["tool_use_id"]]
                if item.get("is_error"):
                    text = _result_text(item.get("content")).lower()
                    denied = any(w in text for w in DENIAL_WORDS)
                    rec["outcome"] = "denied" if denied else "error"
                    rec["reason"] = _result_text(item.get("content"))[:REASON_MAX]
                else:
                    rec["outcome"] = "allowed"
    return [calls[i] for i in order]


def append(path: Path, recs: list[dict]) -> None:
    if not recs:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for r in recs:
            f.write(json.dumps(r, sort_keys=True) + "\n")


def sweep_transcripts(projects: Path, state_file: Path, audit_file: Path, actor: str) -> int:
    """Record interactive sessions' tool calls from Claude Code's own transcripts, once each."""
    try:
        seen = set(json.loads(state_file.read_text()).get("seen", []))
    except (OSError, ValueError):
        seen = set()
    added = 0
    for f in sorted(projects.rglob("*.jsonl")) if projects.is_dir() else []:
        try:
            recs = records(f.read_text().splitlines(), actor, session=f.stem)
        except OSError:
            continue
        fresh = [r for r in recs if r["call_id"] not in seen]
        append(audit_file, fresh)
        seen.update(r["call_id"] for r in fresh)
        added += len(fresh)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({"seen": sorted(seen)}))
    return added


def offered_tools(lines) -> set[str] | None:
    """The tools the session offered the model, from the stream's init event (None if absent)."""
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "system" and ev.get("subtype") == "init" and "tools" in ev:
            return {t if isinstance(t, str) else str(t.get("name")) for t in ev["tools"]}
    return None
