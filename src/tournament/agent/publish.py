"""`tournament agent publish`: facts for the day, then the read-only view (run by the owner)."""

from __future__ import annotations

import logging
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import create_engine

from tournament.agent.config import load_agent_config
from tournament.agent.facts import build_facts, write_facts
from tournament.agent.view import publish_view
from tournament.appconfig import app_config
from tournament.db import database_url

log = logging.getLogger("tournament.agent")


def _version(repo: Path) -> dict:
    def git(*args: str) -> str | None:
        try:
            done = subprocess.run(
                ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=20
            )
            return done.stdout.strip() if done.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            return None

    status = git("status", "--porcelain")
    return {
        "git_commit": git("rev-parse", "HEAD"),
        "git_dirty": None if status is None else bool(status),
        "published_at": datetime.now(UTC).isoformat(),
    }


def run_publish(day: date, repo: Path, data_root: Path) -> int:
    cfg = load_agent_config()
    app = app_config()
    engine = create_engine(database_url())
    facts = build_facts(day, data_root / "live", engine, app.exchange.tz, cfg.view.sessions_back)
    facts_dir = data_root / "agent" / "facts"
    write_facts(facts, facts_dir)
    manifest = publish_view(
        repo, cfg, cfg.shared("view_dir"), facts_dir, data_root / "live", _version(repo)
    )
    log.info("view published: %s files, facts %s", manifest["files"], facts["content_hash"][:12])
    return 0
