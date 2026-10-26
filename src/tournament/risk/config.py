"""The risk config: one validated, hashed file (RK-07, FR-09, NFR-11).

No field has a default. A missing or out-of-range value raises RiskConfigError, so an account
can't be activated on a half-set budget. The file is chosen by TOURNAMENT_RISK_CONFIG (a path),
else config/risk/risk-v1.yaml. Values live in the file, never in code or in `.env`: they are
versioned, hashed into the manifest and checked by the preflight.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PATH = REPO_ROOT / "config" / "risk" / "risk-v1.yaml"
ENV_VAR = "TOURNAMENT_RISK_CONFIG"


class RiskConfigError(RuntimeError):
    pass


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Sizing(_Section):
    premium_budget_pct: float = Field(gt=0, le=1)
    equity_basis: Literal["start_of_day", "initial"]
    rounding: Literal["floor"]
    min_quantity: int = Field(ge=1)
    on_quote_size_shortfall: Literal["cap", "reject"]
    contract_multiplier: int = Field(gt=0)


class PremiumStop(_Section):
    loss_pct: float = Field(gt=0, lt=1)


class Limits(_Section):
    max_open_positions: int = Field(ge=1)
    max_open_premium_pct: float = Field(gt=0, le=1)
    reservation_ttl_s: float = Field(gt=0)


class DailyLoss(_Section):
    limit_usd: float = Field(gt=0)
    measure: Literal["realized_only", "realized_plus_open_at_bid"]
    on_breach: Literal["halt_entries", "halt_and_flatten"]
    unmarked_position: Literal["full_loss", "entry_cost"]


class ContractSelection(_Section):
    expiry_days: int = Field(ge=0, le=7)
    strike_offset: int = Field(ge=-20, le=20)
    max_spread_abs: float = Field(gt=0)
    max_spread_pct: float = Field(gt=0, le=1)
    min_ask: float = Field(ge=0)
    min_ask_size: int = Field(ge=1)


class RiskConfig(_Section):
    version: str
    sizing: Sizing
    premium_stop: PremiumStop
    limits: Limits
    daily_loss: DailyLoss
    contract_selection: ContractSelection

    @property
    def config_hash(self) -> str:
        blob = json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()


def load_risk_config(path: Path | str | None = None) -> RiskConfig:
    chosen = Path(path or os.environ.get(ENV_VAR) or DEFAULT_PATH)
    if not chosen.is_absolute():
        chosen = REPO_ROOT / chosen
    try:
        raw = yaml.safe_load(chosen.read_text())
    except OSError as exc:
        raise RiskConfigError(f"cannot read risk config {chosen}: {exc}") from exc
    try:
        return RiskConfig.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise RiskConfigError(f"{chosen.name}: {problems}") from exc
