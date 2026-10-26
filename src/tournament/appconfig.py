"""Application settings from one validated YAML file (config/app/app-v1.yaml).

Same rules as the risk config: no defaults in code, unknown keys refused, the file hashed. The
path comes from TOURNAMENT_APP_CONFIG. The result is cached per path, so every module reads
the same values without passing them around.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date, time
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = REPO_ROOT / "config" / "app" / "app-v1.yaml"
ENV_VAR = "TOURNAMENT_APP_CONFIG"


class AppConfigError(RuntimeError):
    pass


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _clock(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


class Exchange(_Section):
    timezone: str
    regular_close: str
    extended_start: str
    after_hours_hours: int = Field(ge=0, le=8)

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown time zone {v!r}") from exc
        return v

    @field_validator("regular_close", "extended_start")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        _clock(v)
        return v

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def close_time(self) -> time:
        return _clock(self.regular_close)

    @property
    def extended_start_time(self) -> time:
        return _clock(self.extended_start)


class Accounts(_Section):
    data_alias: str
    dev_alias: str


class StockStream(_Section):
    sip: str
    test: str


class Endpoints(_Section):
    stock_stream: StockStream
    option_stream_base: str


class Database(_Section):
    url: str


class Universe(_Section):
    max_dte: int = Field(ge=0)
    strike_pct: float = Field(gt=0, lt=1)


class DownloadGuard(_Section):
    start: str
    end: str
    timezone_weekdays_only: bool

    @property
    def start_time(self) -> time:
        return _clock(self.start)

    @property
    def end_time(self) -> time:
        return _clock(self.end)


class History(_Section):
    start_date: date
    universe: Universe
    trade_slice_minutes: int = Field(gt=0)
    parallel_requests: int = Field(gt=0)
    option_symbols_per_request: int = Field(gt=0)
    contracts_page_limit: int = Field(gt=0)
    download_guard: DownloadGuard


class Performance(_Section):
    classify_cache_size: int = Field(gt=0)
    rvol_history_depth_factor: int = Field(ge=1)


class AppConfig(_Section):
    version: str
    underlying: str
    exchange: Exchange
    accounts: Accounts
    endpoints: Endpoints
    database: Database
    history: History
    performance: Performance

    @property
    def config_hash(self) -> str:
        blob = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()


def load_app_config(path: Path | str | None = None) -> AppConfig:
    chosen = Path(path or os.environ.get(ENV_VAR) or DEFAULT_PATH)
    if not chosen.is_absolute():
        chosen = REPO_ROOT / chosen
    try:
        raw = yaml.safe_load(chosen.read_text())
    except OSError as exc:
        raise AppConfigError(f"cannot read app config {chosen}: {exc}") from exc
    try:
        return AppConfig.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise AppConfigError(f"{chosen.name}: {problems}") from exc


@lru_cache(maxsize=8)
def _cached(path: str | None) -> AppConfig:
    return load_app_config(path)


def app_config() -> AppConfig:
    """The active settings. The cache is keyed by the env var, so tests can switch files."""
    return _cached(os.environ.get(ENV_VAR))
