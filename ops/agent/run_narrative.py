#!/usr/bin/env python3
"""The after-close narrative job (AG-06, AG-13): a headless Claude Code run, as the agent user.

Standard library only: this runs as an account that cannot see the repository, from the
read-only view the owner publishes. All values arrive as arguments, rendered from
config/agent/agent-v1.yaml into the launchd job. A failure here changes nothing else: it only
means no narrative today (AG-09).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def final_text(stream: str) -> str | None:
    """The model's answer: the last `result` event of the stream-json log."""
    answer = None
    for line in stream.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "result" and not ev.get("is_error"):
            answer = ev.get("result")
    return answer


def run(a: argparse.Namespace) -> int:
    view, out, logs = Path(a.view), Path(a.out), Path(a.logs)
    day = a.date or datetime.now(ZoneInfo(a.tz)).date().isoformat()
    facts_path = view / "facts" / f"{day}.json"
    if not facts_path.exists():
        print(f"no facts for {day}; nothing to write", file=sys.stderr)
        return 2
    facts = json.loads(facts_path.read_text())
    if not facts.get("has_session"):
        print(f"{day}: no session ledger, no narrative")
        return 0
    prompt_text = (view / a.prompt_file).read_text().replace("FACTS_DATE", day)
    cmd = [
        a.claude, "-p", prompt_text, "--model", a.model, "--max-turns", str(a.max_turns),
        "--output-format", a.output_format, "--verbose",
        "--allowedTools", ",".join(a.allowed), "--disallowedTools", ",".join(a.disallowed),
    ]  # fmt: skip
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / f"narrative-{day}.jsonl"
    try:
        done = subprocess.run(
            cmd, cwd=view, capture_output=True, text=True, timeout=a.timeout,
            env={**os.environ, "NO_COLOR": "1"},
        )  # fmt: skip
    except subprocess.TimeoutExpired as exc:
        log.write_text(
            (exc.stdout or b"").decode() if isinstance(exc.stdout, bytes) else exc.stdout or ""
        )
        print(f"timed out after {a.timeout}s", file=sys.stderr)
        return 3
    log.write_text(done.stdout)
    text = final_text(done.stdout) if a.output_format == "stream-json" else done.stdout
    if done.returncode != 0 or not text or len(text.split()) < a.min_words:
        print(
            f"no usable narrative (exit {done.returncode}): {done.stderr[-400:]}", file=sys.stderr
        )
        return 4
    header = (
        "---\n"
        f"date: {day}\n"
        f"facts_file: facts/{day}.json\n"
        f"facts_content_hash: {facts['content_hash']}\n"
        f"prompt_sha256: {hashlib.sha256(prompt_text.encode()).hexdigest()}\n"
        f"agent_config_hash: {a.config_hash}\n"
        f"model: {a.model}\n"
        "written_by: headless agent job; the numbers come from the facts file\n"
        "---\n\n"
    )
    out.mkdir(parents=True, exist_ok=True)
    tmp = out / f".{day}.md.part"
    tmp.write_text(header + text.strip() + "\n")
    os.replace(tmp, out / f"{day}.md")
    print(f"wrote {out / f'{day}.md'}")
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--view", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--logs", required=True)
    p.add_argument("--claude", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--max-turns", type=int, required=True)
    p.add_argument("--timeout", type=int, required=True)
    p.add_argument("--prompt-file", required=True)
    p.add_argument("--output-format", required=True)
    p.add_argument("--min-words", type=int, required=True)
    p.add_argument("--allowed", nargs="+", required=True)
    p.add_argument("--disallowed", nargs="+", required=True)
    p.add_argument("--config-hash", required=True)
    p.add_argument("--tz", required=True)
    p.add_argument("--date", default=None)
    return p


if __name__ == "__main__":
    sys.exit(run(parser().parse_args()))
