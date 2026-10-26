"""The ledger config: validated, hashed, no defaults in code (D-59)."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PATH = REPO_ROOT / "config" / "ledger" / "ledger-v1.yaml"
ENV_VAR = "TOURNAMENT_LEDGER_CONFIG"


class LedgerConfigError(RuntimeError):
    pass


class _S(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Evaluation(_S):
    entry_price: Literal["ask", "mid"]
    exit_price: Literal["bid", "mid"]
    entry_slippage_ticks: float = Field(ge=0)
    exit_slippage_ticks: float = Field(ge=0)
    tick_size: float = Field(gt=0)
    contract_multiplier: int = Field(gt=0)
    missing_quote: Literal["flag_and_use_broker", "exclude"]


class Costs(_S):
    commission_per_contract: float = Field(ge=0)
    regulatory_fee_per_contract: float = Field(ge=0)
    fee_multiplier_stress: float = Field(gt=0)


class Scenario(_S):
    name: str
    extra_slippage_ticks: float = Field(ge=0)
    fee_multiplier: float = Field(gt=0)


class Reconciliation(_S):
    cash_usd: float = Field(ge=0)
    trade_pnl_usd: float = Field(ge=0)
    position_contracts: int = Field(ge=0)


class Snapshots(_S):
    interval_s: float = Field(gt=0)


class Report(_S):
    win_threshold_usd: float
    annualization: Literal["none"]
    profit_factor_when_no_losses: Literal["undefined", "infinity"]


class Gates(_S):
    min_net_pnl_usd: float | None
    max_drawdown_pct_of_start: float | None
    min_trades: int | None
    min_active_days: int | None
    max_unresolved_accounting_incidents: int | None
    min_conservative_net_pnl_usd: float | None


class LedgerConfig(_S):
    version: str
    evaluation: Evaluation
    costs: Costs
    stress: list[Scenario] = Field(min_length=1)
    reconciliation: Reconciliation
    snapshots: Snapshots
    report: Report
    promotion_gates: Gates

    @model_validator(mode="after")
    def _base_scenario(self):
        if not any(s.name == "base" for s in self.stress):
            raise ValueError("the stress list must include a scenario named 'base'")
        return self

    @property
    def config_hash(self) -> str:
        blob = json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()


def load_ledger_config(path: Path | str | None = None) -> LedgerConfig:
    chosen = Path(path or os.environ.get(ENV_VAR) or DEFAULT_PATH)
    if not chosen.is_absolute():
        chosen = REPO_ROOT / chosen
    try:
        raw = yaml.safe_load(chosen.read_text())
    except OSError as exc:
        raise LedgerConfigError(f"cannot read ledger config {chosen}: {exc}") from exc
    try:
        return LedgerConfig.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise LedgerConfigError(f"{chosen.name}: {problems}") from exc
