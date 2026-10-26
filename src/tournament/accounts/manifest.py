"""The experiment manifest and the startup preflight (BRD 9.3, 10.1, 10.2; FR-01, FR-06,
FR-13; NFR-15; AC-21, AC-23).

A manifest is a committed YAML file naming the experiment, its mode, one account binding per
strategy, the equal starting balance, the git tag of the code, and the hash of every config
file the run depends on. The preflight recomputes those hashes and compares them with the
manifest, so a config edited after approval stops the run instead of silently changing it
(FR-09). Paper mode additionally needs a clean working tree whose HEAD is the manifest's tag
and an approval. Shadow mode records the same facts and treats git findings as warnings.
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from tournament.appconfig import app_config
from tournament.features.engine import FeatureConfig
from tournament.live.option_stream import check_option_feed
from tournament.live.stream import FeedGuardError
from tournament.orders.config import load_orders_config
from tournament.replay.ids import digest
from tournament.risk.config import load_risk_config

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG = REPO_ROOT / "config"
EXPERIMENTS = CONFIG / "experiments"
STRATEGIES = ("vpc", "orb", "brt", "fbr", "vmr")


class ManifestError(RuntimeError):
    pass


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Binding(_Model):
    strategy_id: str
    alias: str = Field(min_length=1)  # credential alias in .env; the keys never appear here

    @property
    def account_binding(self) -> str:
        return f"acct:{self.strategy_id}"


class Approval(_Model):
    by: str | None
    at: str | None


class Manifest(_Model):
    experiment_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    mode: Literal["shadow", "paper"]
    git_tag: str | None
    starting_balance_usd: float = Field(gt=0)
    accounts: list[Binding] = Field(min_length=1)
    config_hashes: dict[str, str]
    approved: Approval

    @model_validator(mode="after")
    def _paper_needs_a_tag(self):
        if self.mode == "paper" and not self.git_tag:
            raise ValueError("a paper manifest must name its git tag")
        return self

    @property
    def manifest_hash(self) -> str:
        """Identity of the experiment: everything except who approved it and when."""
        body = self.model_dump(exclude={"approved"})
        return digest(body)


# ---- hashes of every file a run depends on ---------------------------------------------------


def _file_hash(*paths: Path) -> str:
    h = hashlib.sha256()
    for p in paths:
        h.update(p.read_bytes())
    return h.hexdigest()


def current_hashes() -> dict[str, str]:
    from tournament.strategies.brt import BRT
    from tournament.strategies.fbr import FBR
    from tournament.strategies.orb import ORB
    from tournament.strategies.vmr import VMR
    from tournament.strategies.vpc import VPC

    workers = {w.strategy_id: w for w in (VPC(), ORB(), BRT(), FBR(), VMR())}
    elig = CONFIG / "trade_eligibility"
    out = {
        "app": app_config().config_hash,
        "feed": _file_hash(CONFIG / "live" / "feed-v1.yaml"),
        "risk": load_risk_config().config_hash,
        "orders": load_orders_config().config_hash,
        "features": FeatureConfig.load().config_hash,
        "common": _file_hash(CONFIG / "strategies" / "common-v1.yaml"),
        "eligibility": _file_hash(
            elig / "minute_bar_adapter.yaml", elig / "notes.yaml", elig / "cts_sale_conditions.csv"
        ),
    }
    for sid in STRATEGIES:
        out[f"strategy_{sid}"] = workers[sid].config_hash
    return out


# ---- git -----------------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def git_state(repo: Path = REPO_ROOT, tag: str | None = None) -> dict:
    try:
        sha = _git(repo, "rev-parse", "HEAD")
        clean = _git(repo, "status", "--porcelain") == ""
        tag_sha = _git(repo, "rev-list", "-n", "1", tag) if tag else None
    except (OSError, subprocess.CalledProcessError):
        return {"sha": None, "clean": False, "tag_sha": None}
    return {"sha": sha, "clean": clean, "tag_sha": tag_sha}


# ---- creating and loading ------------------------------------------------------------------


def draft_manifest(
    experiment_id: str, mode: str, git_tag: str | None, starting_balance_usd: float
) -> Manifest:
    """A manifest for today's configs, unapproved. The owner approves by editing the file."""
    return Manifest(
        experiment_id=experiment_id,
        mode=mode,
        git_tag=git_tag,
        starting_balance_usd=starting_balance_usd,
        accounts=[Binding(strategy_id=s, alias=s) for s in STRATEGIES],
        config_hashes=current_hashes(),
        approved=Approval(by=None, at=None),
    )


def write_manifest(m: Manifest, path: Path | None = None) -> Path:
    path = path or EXPERIMENTS / f"{m.experiment_id}.yaml"
    if path.exists():
        raise ManifestError(f"{path} exists: a manifest is immutable, make a new experiment ID")
    path.parent.mkdir(parents=True, exist_ok=True)
    header = "# Experiment manifest (BRD 10.1). Immutable once committed and tagged.\n"
    path.write_text(header + yaml.safe_dump(m.model_dump(), sort_keys=False))
    return path


def load_manifest(path: Path | str) -> Manifest:
    try:
        return Manifest.model_validate(yaml.safe_load(Path(path).read_text()))
    except (OSError, ValidationError) as exc:
        raise ManifestError(f"{path}: {exc}") from exc


def approve(path: Path, by: str, at: datetime | None = None) -> None:
    """Fill in the approval block. Nothing else in the file may change."""
    raw = yaml.safe_load(path.read_text())
    raw["approved"] = {"by": by, "at": (at or datetime.now(UTC)).isoformat(timespec="seconds")}
    path.write_text(yaml.safe_dump(raw, sort_keys=False))


# ---- preflight ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str = ""
    blocking: bool = True


@dataclass
class Preflight:
    manifest_hash: str
    commit_sha: str | None
    checks: list[Check] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.ok for c in self.checks if c.blocking)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]


def preflight(
    m: Manifest,
    repo: Path = REPO_ROOT,
    options_feed: str | None = None,
    hashes: dict[str, str] | None = None,
) -> Preflight:
    paper = m.mode == "paper"
    git = git_state(repo, m.git_tag)
    out = Preflight(m.manifest_hash, git["sha"])
    add = out.checks.append

    # FR-01, FR-02: one distinct binding and credential alias per strategy
    bindings = [a.account_binding for a in m.accounts]
    aliases = [a.alias for a in m.accounts]
    add(
        Check(
            "unique_account_bindings",
            len(set(bindings)) == len(bindings) and len(set(aliases)) == len(aliases),
            f"{len(bindings)} accounts",
        )
    )
    add(
        Check(
            "all_strategies_bound",
            sorted(a.strategy_id for a in m.accounts) == sorted(STRATEGIES),
            ",".join(sorted(a.strategy_id for a in m.accounts)),
        )
    )
    # configuration frozen as approved (FR-09)
    now = hashes if hashes is not None else current_hashes()
    for name, expected in sorted(m.config_hashes.items()):
        ok = now.get(name) == expected
        add(Check(f"config_{name}", ok, "" if ok else "differs from the manifest"))
    extra = sorted(set(now) - set(m.config_hashes))
    add(Check("config_complete", not extra, f"not in the manifest: {extra}" if extra else ""))
    # MD-09, AC-23: real-time OPRA only
    try:
        feed = options_feed or _feed_option()
        check_option_feed(feed, m.mode)
        add(Check("options_feed", True, feed))
    except FeedGuardError as exc:
        add(Check("options_feed", False, str(exc)))
    # NFR-15, AC-21: release integrity (paper blocks; shadow warns)
    add(Check("git_tree_clean", git["clean"], "" if git["clean"] else "uncommitted changes", paper))
    if m.git_tag:
        ok = git["tag_sha"] is not None and git["tag_sha"] == git["sha"]
        add(Check("head_at_manifest_tag", ok, f"tag {m.git_tag}", paper))
    else:
        add(Check("head_at_manifest_tag", False, "no git tag in the manifest", paper))
    # 10.1: unresolved mandatory values prevent activation
    approved = m.approved.by is not None and m.approved.at is not None
    add(Check("approved", approved, "" if approved else "no approval recorded", paper))
    return out


def _feed_option() -> str:
    from tournament.live.runner import load_feed_config

    return load_feed_config()["options"]["feed"]
