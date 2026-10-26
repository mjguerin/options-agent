"""Publish the read-only view the agent works from (AG-08, AG-11, AC-24).

Run by the owner's account after the close. It copies the project's reading material (specs,
config, source, results) into a shared folder that the agent's account can read and not write,
with the daily facts beside it. It never copies .env or the history store, and it refuses to
finish if a secret value from .env turns up in anything it copied.
"""

from __future__ import annotations

import fnmatch
import json
import os
import shutil
from pathlib import Path

from tournament.agent.config import AgentConfig


class SecretLeak(RuntimeError):
    pass


def env_values(env_file: Path, min_chars: int) -> list[str]:
    if not env_file.exists():
        return []
    out = []
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        v = line.split("=", 1)[1].strip().strip("\"'")
        if len(v) >= min_chars:
            out.append(v)
    return out


def _excluded(path: Path, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(part, p) for part in path.parts for p in patterns)


def _copy_tree(
    src: Path, dest: Path, cfg: AgentConfig, skipped: list[str], label: str
) -> list[Path]:
    limit = cfg.view.max_file_mb * 1024 * 1024
    out = []
    for root, dirs, files in os.walk(src):
        rel_root = Path(root).relative_to(src)
        dirs[:] = sorted(d for d in dirs if not _excluded(rel_root / d, cfg.view.exclude_names))
        for name in sorted(files):
            rel = rel_root / name
            if _excluded(rel, cfg.view.exclude_names):
                continue
            f = Path(root) / name
            if f.is_symlink() or f.stat().st_size > limit:
                skipped.append(str(Path(label) / rel))
                continue
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f, target)
            out.append(target)
    return out


def _live(root: Path, repo: Path, dest: Path, cfg: AgentConfig, skipped: list[str]) -> list[Path]:
    out = []
    for pattern in cfg.view.live_files:
        for f in sorted(root.glob(f"*/*/{pattern}")):
            if f.stat().st_size > cfg.view.max_file_mb * 1024 * 1024:
                skipped.append(str(Path("data/live") / f.relative_to(root)))
                continue
            target = dest / "data" / "live" / f.relative_to(root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f, target)
            out.append(target)
    return out


def publish_view(
    repo: Path,
    cfg: AgentConfig,
    dest: Path,
    facts_dir: Path | None,
    live_root: Path,
    version: dict | None = None,
) -> dict:
    """Build the view in a sibling folder, scan it, then swap it in. Returns the manifest."""
    stage = dest.with_name(dest.name + ".new")
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    skipped: list[str] = []
    copied: list[Path] = []
    for d in cfg.view.include_dirs:
        if (repo / d).is_dir():
            copied += _copy_tree(repo / d, stage / d, cfg, skipped, d)
    for name in cfg.view.include_files:
        if (repo / name).is_file():
            shutil.copyfile(repo / name, stage / name)
            copied.append(stage / name)
    copied += _live(live_root, repo, stage, cfg, skipped)
    if facts_dir is not None and facts_dir.is_dir():
        copied += _copy_tree(facts_dir, stage / "facts", cfg, skipped, "facts")

    secrets = env_values(repo / ".env", cfg.view.secret_scan_min_chars)
    for f in copied:
        try:
            text = f.read_text(errors="ignore")
        except OSError:
            continue
        if any(s in text for s in secrets):
            shutil.rmtree(stage, ignore_errors=True)
            raise SecretLeak(f"a .env value appears in {f.relative_to(stage)}; view not published")

    facts_hashes = {}
    for f in sorted((facts_dir or Path("/nonexistent")).glob("*.json")):
        try:
            facts_hashes[f.stem] = json.loads(f.read_text()).get("content_hash")
        except (OSError, ValueError):
            continue
    manifest = {
        "agent_config_hash": cfg.config_hash,
        "files": len(copied),
        "skipped_too_large": sorted(skipped),
        "facts_content_hashes": facts_hashes,
        **(version or {}),  # git commit, whether the tree was dirty, when it was published
    }
    (stage / "VIEW_MANIFEST.json").write_text(json.dumps(manifest, indent=1, sort_keys=True))
    # Readable by the agent, writable by nobody but the owner.
    for root, _dirs, files in os.walk(stage):
        os.chmod(root, 0o755)
        for name in files:
            os.chmod(Path(root) / name, 0o644)
    old = dest.with_name(dest.name + ".old")
    shutil.rmtree(old, ignore_errors=True)
    if dest.exists():
        dest.rename(old)
    stage.rename(dest)
    shutil.rmtree(old, ignore_errors=True)
    # Each published view leaves a line behind, so what the agent saw on any day can be identified.
    with (dest.parent / "view-history.jsonl").open("a") as h:
        h.write(json.dumps(manifest, sort_keys=True) + "\n")
    return manifest
