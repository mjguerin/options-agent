"""The ledger config: validated, hashed, no defaults in code (D-59)."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date
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


class CostRate(_S):
    effective_from: date
    commission_per_contract: float = Field(ge=0)
    regulatory_fee_per_contract: float = Field(ge=0)
    source: str  # where the rates came from, so an auditor can check them


class Costs(_S):
    status: Literal["unapproved", "approved"]
    schedule: list[CostRate] = Field(min_length=1)  # effective-dated, each side of a trade

    def rate_on(self, day: date | str | None) -> CostRate:
        """The rate in force on a session day (the latest schedule entry if no day is given)."""
        ordered = sorted(self.schedule, key=lambda r: r.effective_from)
        if day is None:
            return ordered[-1]
        d = date.fromisoformat(day) if isinstance(day, str) else day
        found = [r for r in ordered if r.effective_from <= d]
        if not found:
            raise ValueError(f"no cost rate is effective on {d}")
        return found[-1]

    @property
    def label(self) -> str:
        return "net of approved costs" if self.status == "approved" else "gross of unmodeled costs"


class Scenario(_S):
    name: str
    extra_slippage_ticks: float = Field(ge=0)
    fee_multiplier: float = Field(gt=0)


class Reconciliation(_S):
    cash_usd: float = Field(ge=0)  # money: the documented rounding of cents, nothing more
    realized_usd: float = Field(ge=0)
    position_contracts: int = Field(ge=0)  # quantities and counts must match exactly
    record_count: int = Field(ge=0)

    @model_validator(mode="after")
    def _exact_counts(self):
        if self.position_contracts != 0 or self.record_count != 0:
            raise ValueError("contract quantities and record counts require exact equality (0)")
        return self


class Snapshots(_S):
    interval_s: float = Field(gt=0)


class Report(_S):
    win_threshold_usd: float
    annualization: Literal["none"]
    profit_factor_when_no_losses: Literal["undefined", "infinity"]


class Gates(_S):
    """Every gate is mandatory. A gate whose threshold is unset is NOT_EVALUABLE and blocks
    qualification: leaving a value out never removes a requirement."""

    min_net_pnl_usd: float | None  # strictly above, after approved costs
    max_unresolved_accounting_incidents: int | None
    min_harsh_net_pnl_usd: float | None  # strictly above, on the harshest scenario
    max_drawdown_pct_of_start: float | None
    min_trades: int | None  # closed trades
    min_active_days: int | None  # days with at least one executed entry
    confirmation_policy: Literal["unset", "approved"]


class LedgerConfig(_S):
    version: str
    evaluation: Evaluation
    costs: Costs
    stress: list[Scenario] = Field(min_length=1)
    harshest_scenario: str
    reconciliation: Reconciliation
    snapshots: Snapshots
    report: Report
    promotion_gates: Gates

    @model_validator(mode="after")
    def _base_scenario(self):
        names = [s.name for s in self.stress]
        if "base" not in names:
            raise ValueError("the stress list must include a scenario named 'base'")
        if self.harshest_scenario not in names:
            raise ValueError("harshest_scenario must name a scenario in the stress list")
        return self

    @property
    def config_hash(self) -> str:
        blob = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
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
