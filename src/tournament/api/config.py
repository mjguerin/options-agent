"""The API config: validated and hashed like the others (D-59)."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PATH = REPO_ROOT / "config" / "api" / "api-v1.yaml"
ENV_VAR = "TOURNAMENT_API_CONFIG"


class ApiConfigError(RuntimeError):
    pass


class _S(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Server(_S):
    host: str
    port: int = Field(ge=1024, le=65535)

    @field_validator("host")
    @classmethod
    def _loopback_only(cls, v: str) -> str:
        """A safety invariant (NFR-14), enforced here and not left to the file."""
        try:
            ok = ipaddress.ip_address(v).is_loopback
        except ValueError:
            ok = v == "localhost"
        if not ok:
            raise ValueError(f"the API may only bind a loopback address, not {v!r}")
        return v


class State(_S):
    file: str
    every_s: float = Field(gt=0)
    history_every_s: float = Field(gt=0)
    history_on_change: bool
    recent_decisions: int = Field(ge=1)
    max_age_s: float = Field(gt=0)


class Commands(_S):
    ttl_s: float = Field(gt=0)
    poll_s: float = Field(gt=0)
    confirmation_ttl_s: float = Field(gt=0)
    audit_page_size: int = Field(ge=1, le=1000)


class Control(_S):
    scopes: list[str] = Field(min_length=1)
    confirm: bool
    summary: str
    needs_symbol: bool = False

    @field_validator("scopes")
    @classmethod
    def _known(cls, v: list[str]) -> list[str]:
        if not set(v) <= {"account", "global"}:
            raise ValueError(f"unknown scope in {v}")
        return v


class ApiConfig(_S):
    version: str
    server: Server
    state: State
    commands: Commands
    controls: dict[str, Control]

    @property
    def config_hash(self) -> str:
        blob = json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()


def load_api_config(path: Path | str | None = None) -> ApiConfig:
    chosen = Path(path or os.environ.get(ENV_VAR) or DEFAULT_PATH)
    if not chosen.is_absolute():
        chosen = REPO_ROOT / chosen
    try:
        raw = yaml.safe_load(chosen.read_text())
    except OSError as exc:
        raise ApiConfigError(f"cannot read api config {chosen}: {exc}") from exc
    try:
        return ApiConfig.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise ApiConfigError(f"{chosen.name}: {problems}") from exc
