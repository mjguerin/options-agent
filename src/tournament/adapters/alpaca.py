"""Alpaca adapter. Paper endpoints only in the initial release (NFR-06, AC-15)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed, OptionsFeed
from alpaca.data.historical import OptionHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import OptionLatestQuoteRequest, StockLatestQuoteRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOptionContractsRequest

from tournament.secrets import AlpacaCredentials

PAPER_TRADING_URL = "https://paper-api.alpaca.markets"

# Market data entitlements belong to the Alpaca login, not to each paper account.
# Algo Trader Plus (SIP + OPRA, MD-09) is on the second login, so market data is
# read with keys from a paper account under that login, stored as this alias.
DATA_ACCOUNT_ALIAS = "data"


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


@dataclass(frozen=True)
class FeedCheck:
    feed: str
    ok: bool
    detail: str


def check_data_entitlements(
    stock: StockHistoricalDataClient,
    option: OptionHistoricalDataClient,
    trading: TradingClient,
    underlying: str = "SPY",
) -> list[FeedCheck]:
    """Ask for real-time SIP and OPRA quotes; a plan without them gets an API error (MD-09)."""
    checks = []
    try:
        quote = stock.get_stock_latest_quote(
            StockLatestQuoteRequest(symbol_or_symbols=underlying, feed=DataFeed.SIP)
        )[underlying]
        checks.append(
            FeedCheck(
                "sip",
                True,
                f"{underlying} {quote.bid_price}/{quote.ask_price} at {quote.timestamp}",
            )
        )
    except APIError as exc:
        checks.append(FeedCheck("sip", False, str(exc).strip()))

    try:
        contracts = trading.get_option_contracts(
            GetOptionContractsRequest(underlying_symbols=[underlying], limit=1)
        ).option_contracts
        if not contracts:
            checks.append(FeedCheck("opra", False, f"no {underlying} option contracts returned"))
        else:
            symbol = contracts[0].symbol
            quote = option.get_option_latest_quote(
                OptionLatestQuoteRequest(symbol_or_symbols=symbol, feed=OptionsFeed.OPRA)
            )[symbol]
            checks.append(
                FeedCheck(
                    "opra",
                    True,
                    f"{symbol} {quote.bid_price}/{quote.ask_price} at {quote.timestamp}",
                )
            )
    except APIError as exc:
        checks.append(FeedCheck("opra", False, str(exc).strip()))
    return checks


def data_clients(
    creds: AlpacaCredentials,
) -> tuple[StockHistoricalDataClient, OptionHistoricalDataClient, TradingClient]:
    return (
        StockHistoricalDataClient(creds.key_id, creds.secret_key),
        OptionHistoricalDataClient(creds.key_id, creds.secret_key),
        paper_trading_client(creds),
    )
