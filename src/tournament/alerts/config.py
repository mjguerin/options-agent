"""The alerts config: one validated, hashed YAML file, same rules as the others (D-59)."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PATH = REPO_ROOT / "config" / "alerts" / "alerts-v1.yaml"
ENV_VAR = "TOURNAMENT_ALERTS_CONFIG"
Severity = Literal["info", "warning", "critical"]
RANK = {"info": 0, "warning": 1, "critical": 2}


class AlertsConfigError(RuntimeError):
    pass


class _S(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Telegram(_S):
    api_base: str
    timeout_s: float = Field(gt=0)


class Delivery(_S):
    poll_s: float = Field(gt=0)
    max_per_minute: int = Field(ge=1)
    retry_backoff_s: list[float] = Field(min_length=1)
    expire_after_s: float = Field(gt=0)
    min_severity: Severity
    repeat_open_critical_s: float = Field(gt=0)
    session_summary: bool


class Monitor(_S):
    feed_down_grace_s: float = Field(ge=0)
    flat_deadline_min_before_close: float = Field(ge=0)


class Heartbeat(_S):
    file: str
    every_s: float = Field(gt=0)


class Watchdog(_S):
    check_every_s: float = Field(gt=0)
    engine_heartbeat_max_age_s: float = Field(gt=0)
    engine_grace_after_start_s: float = Field(ge=0)
    nightly_deadline_after_s: float = Field(gt=0)
    backup_log: str
    backup_max_age_h: float = Field(gt=0)
    min_free_disk_gb: float = Field(ge=0)
    disk_path: str


class Kind(_S):
    severity: Severity
    open: str
    resolved: str


class AlertsConfig(_S):
    version: str
    telegram: Telegram
    delivery: Delivery
    monitor: Monitor
    heartbeat: Heartbeat
    watchdog: Watchdog
    kinds: dict[str, Kind]

    @model_validator(mode="after")
    def _templates_format(self):
        for name, k in self.kinds.items():
            for t in (k.open, k.resolved):
                try:
                    t.format(account="a", detail="d")
                except (KeyError, IndexError, ValueError) as exc:
                    raise ValueError(f"kind {name}: bad template {t!r}: {exc}") from exc
        return self

    @property
    def config_hash(self) -> str:
        blob = json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()


def load_alerts_config(path: Path | str | None = None) -> AlertsConfig:
    chosen = Path(path or os.environ.get(ENV_VAR) or DEFAULT_PATH)
    if not chosen.is_absolute():
        chosen = REPO_ROOT / chosen
    try:
        raw = yaml.safe_load(chosen.read_text())
    except OSError as exc:
        raise AlertsConfigError(f"cannot read alerts config {chosen}: {exc}") from exc
    try:
        return AlertsConfig.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise AlertsConfigError(f"{chosen.name}: {problems}") from exc
