"""The dashboard config: validated, loopback-only, no defaults in code (D-59)."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
from pathlib import Path
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PATH = REPO_ROOT / "config" / "dashboard" / "dashboard-v1.yaml"
ENV_VAR = "TOURNAMENT_DASHBOARD_CONFIG"


class DashboardConfigError(RuntimeError):
    pass


class _S(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


class Server(_S):
    host: str
    port: int = Field(ge=1024, le=65535)

    @field_validator("host")
    @classmethod
    def _local(cls, v: str) -> str:
        if not _loopback(v):
            raise ValueError(f"the dashboard may only bind a loopback address, not {v!r}")
        return v


class Api(_S):
    base_url: str
    timeout_s: float = Field(gt=0)

    @field_validator("base_url")
    @classmethod
    def _local(cls, v: str) -> str:
        if not _loopback(urlparse(v).hostname or ""):
            raise ValueError(f"the API address must be a loopback address, not {v!r}")
        return v.rstrip("/")


class Data(_S):
    live_root: str
    ops_root: str


class Tables(_S):
    max_rows: int = Field(ge=1)


class Health(_S):
    engine_stale_s: float = Field(gt=0)
    lag_warn_s: float = Field(gt=0)


class Controls(_S):
    confirm_phrase: str

    @field_validator("confirm_phrase")
    @classmethod
    def _fields(cls, v: str) -> str:
        v.format(command="C", target="T")
        return v


class DashboardConfig(_S):
    version: str
    server: Server
    api: Api
    refresh_s: float = Field(gt=0)
    data: Data
    tables: Tables
    health: Health
    controls: Controls

    @property
    def config_hash(self) -> str:
        blob = json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()


def load_dashboard_config(path: Path | str | None = None) -> DashboardConfig:
    chosen = Path(path or os.environ.get(ENV_VAR) or DEFAULT_PATH)
    if not chosen.is_absolute():
        chosen = REPO_ROOT / chosen
    try:
        raw = yaml.safe_load(chosen.read_text())
    except OSError as exc:
        raise DashboardConfigError(f"cannot read dashboard config {chosen}: {exc}") from exc
    try:
        return DashboardConfig.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise DashboardConfigError(f"{chosen.name}: {problems}") from exc
