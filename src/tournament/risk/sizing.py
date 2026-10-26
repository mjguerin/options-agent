"""Quantity from the premium budget (CS-04, CS-05; D-55).

quantity = floor(budget / premium per contract), where budget = premium_budget_pct times the
equity basis and the premium per contract is the ask times the multiplier. Then it is checked
against the cash available and the quoted ask size. A contract that costs more than the budget
is rejected with a reason, never sized up. Premium at risk and planned stop risk are reported
separately (CS-04); a stop-based loss is not a guaranteed cap.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

from tournament.risk.config import RiskConfig

PREMIUM_EXCEEDS_BUDGET, BELOW_MIN_QUANTITY = "PREMIUM_EXCEEDS_BUDGET", "BELOW_MIN_QUANTITY"
QUOTE_SIZE_SHORTFALL = "QUOTE_SIZE_SHORTFALL"


@dataclass(frozen=True)
class SizeResult:
    quantity: int
    reasons: tuple[str, ...]
    budget: float
    premium_per_contract: float
    premium_at_risk: float
    planned_stop_risk: float
    capped_by: str | None  # "cash" or "ask_size" when the budget allowed more
    requested_quantity: int  # what the budget alone allowed, before cash and quoted size

    @property
    def sized(self) -> bool:
        return not self.reasons


def equity_basis(cfg: RiskConfig, start_of_day_equity: float, initial_equity: float) -> float:
    return start_of_day_equity if cfg.sizing.equity_basis == "start_of_day" else initial_equity


def size_order(
    ask: float, ask_size: int, basis_equity: float, available_cash: float, cfg: RiskConfig
) -> SizeResult:
    s = cfg.sizing
    budget = Decimal(str(s.premium_budget_pct)) * Decimal(str(basis_equity))
    cost = Decimal(str(ask)) * s.contract_multiplier
    capped = None
    requested = 0

    def result(q: int, reasons: tuple[str, ...]) -> SizeResult:
        risk = float(cost * q)
        return SizeResult(
            q,
            reasons,
            float(budget),
            float(cost),
            risk,
            risk * cfg.premium_stop.loss_pct,
            capped,
            requested,
        )

    if cost <= 0 or cost > budget:
        return result(0, (PREMIUM_EXCEEDS_BUDGET,))
    qty = int((budget / cost).to_integral_value(rounding=ROUND_FLOOR))
    requested = qty
    by_cash = int((Decimal(str(available_cash)) / cost).to_integral_value(rounding=ROUND_FLOOR))
    if by_cash < qty:
        qty, capped = by_cash, "cash"
    if ask_size < qty:
        if s.on_quote_size_shortfall == "reject":
            return result(0, (QUOTE_SIZE_SHORTFALL,))
        qty, capped = ask_size, "ask_size"
    if qty < s.min_quantity:
        return result(0, (BELOW_MIN_QUANTITY,))
    return result(qty, ())
