"""Alpaca adapter. Paper endpoints only in the initial release (NFR-06, AC-15)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from alpaca.trading.client import TradingClient

from tournament.secrets import AlpacaCredentials

PAPER_TRADING_URL = "https://paper-api.alpaca.markets"


class LiveEndpointRejectedError(RuntimeError):
    pass


def assert_paper_url(url: str) -> None:
    """Fail closed on anything that is not the Alpaca paper trading host."""
    if url.rstrip("/") != PAPER_TRADING_URL:
        raise LiveEndpointRejectedError(f"Refusing non-paper trading endpoint: {url}")


def paper_trading_client(creds: AlpacaCredentials) -> TradingClient:
    # paper=True is hard-coded; there is deliberately no parameter to turn it off.
    client = TradingClient(creds.key_id, creds.secret_key, paper=True)
    assert_paper_url(trading_base_url(client))
    return client


def trading_base_url(client: TradingClient) -> str:
    # alpaca-py stores a BaseURL enum; str() of it is the member name, not the URL.
    url = client._base_url
    return str(getattr(url, "value", url))


@dataclass(frozen=True)
class AccountSummary:
    """Non-secret account facts. Uses buying_power, never the deprecated PDT fields (RK-08)."""

    account_number_masked: str
    status: str
    currency: str
    equity: Decimal
    cash: Decimal
    buying_power: Decimal
    options_trading_level: int | None
    trading_blocked: bool


def _mask(account_number: str) -> str:
    return f"****{account_number[-4:]}" if account_number else "****"


def get_account_summary(client: TradingClient) -> AccountSummary:
    acct = client.get_account()
    return AccountSummary(
        account_number_masked=_mask(acct.account_number),
        status=str(acct.status.value if hasattr(acct.status, "value") else acct.status),
        currency=acct.currency or "USD",
        equity=Decimal(acct.equity or "0"),
        cash=Decimal(acct.cash or "0"),
        buying_power=Decimal(acct.buying_power or "0"),
        options_trading_level=getattr(acct, "options_trading_level", None),
        trading_blocked=bool(acct.trading_blocked),
    )
