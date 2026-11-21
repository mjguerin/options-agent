"""The Alpaca paper broker for one strategy's account (Step 4), with the account facts the
runtime needs: cash for sizing and the checks that decide whether the account may trade at all.

Paper only: the client comes from `paper_trading_client`, which has no live setting. A failure
to read the account is never read as money: cash then reads zero, so sizing refuses the entry.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from tournament.adapters.alpaca import AccountSummary, get_account_summary
from tournament.orders.broker import AlpacaPaperBroker
from tournament.secrets import AlpacaCredentials

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PaperChecks:
    """Owner-set limits for the paper accounts (config/live/feed-v1.yaml, paper_accounts)."""

    min_options_level: int
    expected_equity_usd: float
    equity_tolerance_usd: float
    cash_cache_s: float


class PaperBroker(AlpacaPaperBroker):
    def __init__(self, creds: AlpacaCredentials, cash_cache_s: float) -> None:
        super().__init__(creds)
        self.alias = creds.account_alias
        self._ttl, self._cash, self._cash_at = cash_cache_s, 0.0, float("-inf")

    def summary(self) -> AccountSummary:
        return get_account_summary(self._client)

    def cash(self) -> float:
        now = time.monotonic()
        if now - self._cash_at >= self._ttl:
            try:
                self._cash = float(self.summary().cash)
            except Exception as exc:  # noqa: BLE001 - no reading means no money to size with
                log.error("paper account %s: cash not readable: %s", self.alias, exc)
                self._cash = 0.0
            self._cash_at = now
        return self._cash

    def restore(self, store, account: str) -> int:
        return 0  # the broker keeps its own orders; recovery asks it (OrderManager.recover)

    def advance(self, now) -> None:  # the modeled broker's clock; a real broker has none
        return None


def account_problems(summary: AccountSummary, checks: PaperChecks) -> tuple[list[str], list[str]]:
    """(blocking problems, warnings). A blocking problem keeps the account from trading."""
    block, warn = [], []
    if summary.status.upper() != "ACTIVE":
        block.append(f"account status is {summary.status}")
    if summary.trading_blocked:
        block.append("trading is blocked on the account")
    if summary.currency != "USD":
        block.append(f"currency is {summary.currency}")
    level = summary.options_trading_level
    if level is None or level < checks.min_options_level:
        block.append(f"options trading level {level} is below {checks.min_options_level}")
    gap = abs(float(summary.equity) - checks.expected_equity_usd)
    if gap > checks.equity_tolerance_usd:
        warn.append(
            f"equity {float(summary.equity):,.2f} differs from the expected "
            f"{checks.expected_equity_usd:,.2f} by more than {checks.equity_tolerance_usd:,.2f}"
        )
    return block, warn
