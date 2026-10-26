"""The order config (D08): one validated, hashed YAML file, same rules as the risk config."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PATH = REPO_ROOT / "config" / "orders" / "orders-v1.yaml"
ENV_VAR = "TOURNAMENT_ORDERS_CONFIG"


class OrdersConfigError(RuntimeError):
    pass


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ClientOrderId(_Section):
    prefix: str = Field(min_length=1, max_length=12, pattern=r"^[A-Za-z0-9]+$")


class Pricing(_Section):
    tick_size: float = Field(gt=0)


class Entry(_Section):
    order_type: Literal["limit"]
    price_basis: Literal["ask", "mid"]
    price_offset_ticks: int
    time_in_force: Literal["day"]
    fill_timeout_s: float = Field(gt=0)
    max_replacements: int = Field(ge=0, le=10)
    replacement_step_ticks: int = Field(ge=1)
    max_slippage_ticks: int = Field(ge=0)
    partial_fill_policy: Literal["keep_filled"]


class Exit(_Section):
    order_type: Literal["limit"]
    price_basis: Literal["bid", "mid"]
    price_offset_ticks: int
    time_in_force: Literal["day"]
    fill_timeout_s: float = Field(gt=0)
    max_replacements: int = Field(ge=0, le=10)
    replacement_step_ticks: int = Field(ge=1)
    final_fallback: Literal["market", "none"]


class Submission(_Section):
    ack_timeout_s: float = Field(gt=0)
    reconcile_attempts: int = Field(ge=1)
    reconcile_interval_s: float = Field(ge=0)
    resubmit_after_not_found: bool
    cancel_confirm_attempts: int = Field(ge=1)
    cancel_confirm_interval_s: float = Field(ge=0)


class Reconciliation(_Section):
    quantity_tolerance: int = Field(ge=0)
    block_entries_on_mismatch: bool


class OrdersConfig(_Section):
    version: str
    client_order_id: ClientOrderId
    pricing: Pricing
    entry: Entry
    exit: Exit
    submission: Submission
    reconciliation: Reconciliation

    @property
    def config_hash(self) -> str:
        blob = json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()


def load_orders_config(path: Path | str | None = None) -> OrdersConfig:
    chosen = Path(path or os.environ.get(ENV_VAR) or DEFAULT_PATH)
    if not chosen.is_absolute():
        chosen = REPO_ROOT / chosen
    try:
        raw = yaml.safe_load(chosen.read_text())
    except OSError as exc:
        raise OrdersConfigError(f"cannot read orders config {chosen}: {exc}") from exc
    try:
        return OrdersConfig.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise OrdersConfigError(f"{chosen.name}: {problems}") from exc
