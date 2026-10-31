"""The report config: one validated, hashed YAML file, same rules as the others (D-59)."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PATH = REPO_ROOT / "config" / "report" / "report-v1.yaml"
ENV_VAR = "TOURNAMENT_REPORT_CONFIG"
LOOPBACK = {"127.0.0.1", "::1", "localhost"}


class ReportConfigError(RuntimeError):
    pass


class _S(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Server(_S):
    host: str
    port: int = Field(ge=1024, le=65535)
    poll_s: float = Field(gt=0)

    @field_validator("host")
    @classmethod
    def _loopback(cls, v: str) -> str:
        if v not in LOOPBACK:
            raise ValueError(f"the report is served on loopback only, not {v!r}")
        return v


class Data(_S):
    live_root: str
    state_file: str
    heartbeat_file: str
    state_max_age_s: float = Field(gt=0)
    timezone: str


class Account(_S):
    id: str
    name: str


class Tables(_S):
    trades_max_rows: int = Field(ge=1)
    signals_max_rows: int = Field(ge=1)


class Labels(_S):
    results: str


class ReportConfig(_S):
    version: str
    server: Server
    data: Data
    accounts: list[Account] = Field(min_length=1)
    tables: Tables
    labels: Labels

    @property
    def config_hash(self) -> str:
        blob = json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()

    def path(self, which: str, root: Path = REPO_ROOT) -> Path:
        p = Path(getattr(self.data, which))
        return p if p.is_absolute() else root / p


def load_report_config(path: Path | str | None = None) -> ReportConfig:
    chosen = Path(path or os.environ.get(ENV_VAR) or DEFAULT_PATH)
    if not chosen.is_absolute():
        chosen = REPO_ROOT / chosen
    try:
        raw = yaml.safe_load(chosen.read_text())
    except OSError as exc:
        raise ReportConfigError(f"cannot read report config {chosen}: {exc}") from exc
    try:
        return ReportConfig.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise ReportConfigError(f"{chosen.name}: {problems}") from exc
