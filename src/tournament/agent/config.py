"""The agent config: one validated, hashed YAML file, same rules as the others (D-59)."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PATH = REPO_ROOT / "config" / "agent" / "agent-v1.yaml"
ENV_VAR = "TOURNAMENT_AGENT_CONFIG"
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class AgentConfigError(RuntimeError):
    pass


class _S(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Host(_S):
    os_user: str
    db_role: str
    database: str
    owner_db_role: str
    db_connection_limit: int = Field(ge=1)
    db_statement_timeout_s: int = Field(ge=1)
    shared_root: str
    view_dir: str
    narratives_dir: str
    agent_logs_dir: str
    python: str
    claude_bin: str

    @field_validator("os_user", "db_role", "database", "owner_db_role")
    @classmethod
    def _identifier(cls, v: str) -> str:
        # These end up in SQL and shell commands: only plain identifiers are accepted.
        if not IDENT.match(v):
            raise ValueError(f"{v!r} is not a plain identifier")
        return v

    @field_validator("view_dir", "narratives_dir", "agent_logs_dir")
    @classmethod
    def _relative(cls, v: str) -> str:
        if v.startswith("/") or ".." in Path(v).parts:
            raise ValueError("must be a relative path under shared_root")
        return v

    @model_validator(mode="after")
    def _peer(self):
        if self.os_user != self.db_role:
            raise ValueError("peer authentication needs db_role to equal os_user")
        return self


class View(_S):
    publish_time: str
    weekdays_only: bool
    include_dirs: list[str]
    include_files: list[str]
    live_files: list[str]
    exclude_names: list[str]
    max_file_mb: float = Field(gt=0)
    sessions_back: int = Field(ge=1)
    secret_scan_min_chars: int = Field(ge=4)

    @field_validator("publish_time")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if not HHMM.match(v):
            raise ValueError("expected HH:MM")
        return v


class Narrative(_S):
    start_time: str
    weekdays_only: bool
    model: str
    max_turns: int = Field(ge=1)
    timeout_s: int = Field(ge=30)
    prompt_file: str
    allowed_tools: list[str]
    disallowed_tools: list[str]
    output_format: str
    min_words: int = Field(ge=0)

    @field_validator("start_time")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if not HHMM.match(v):
            raise ValueError("expected HH:MM")
        return v


class Interactive(_S):
    allowed_tools: list[str]
    disallowed_tools: list[str]


class AgentConfig(_S):
    version: str
    host: Host
    view: View
    narrative: Narrative
    interactive: Interactive

    @property
    def config_hash(self) -> str:
        blob = json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()

    def shared(self, which: str) -> Path:
        return Path(self.host.shared_root) / getattr(self.host, which)


def load_agent_config(path: Path | str | None = None) -> AgentConfig:
    chosen = Path(path or os.environ.get(ENV_VAR) or DEFAULT_PATH)
    if not chosen.is_absolute():
        chosen = REPO_ROOT / chosen
    try:
        raw = yaml.safe_load(chosen.read_text())
    except OSError as exc:
        raise AgentConfigError(f"cannot read agent config {chosen}: {exc}") from exc
    try:
        return AgentConfig.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise AgentConfigError(f"{chosen.name}: {problems}") from exc
