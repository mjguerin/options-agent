import os
from types import SimpleNamespace

import pytest
from alpaca.common.exceptions import APIError

from tournament.adapters.alpaca import (
    DATA_ACCOUNT_ALIAS,
    check_data_entitlements,
    data_clients,
)
from tournament.secrets import load_alpaca_credentials

QUOTE = SimpleNamespace(bid_price=1.0, ask_price=1.1, timestamp="2026-10-23T15:00:00Z")
DENIED = APIError('{"message": "subscription does not permit querying recent SIP data"}')


class Stock:
    def __init__(self, error=None):
        self.error = error
        self.feed = None

    def get_stock_latest_quote(self, request):
        self.feed = request.feed
        if self.error:
            raise self.error
        return {"SPY": QUOTE}


class Option:
    def __init__(self, error=None):
        self.error = error
        self.feed = None

    def get_option_latest_quote(self, request):
        self.feed = request.feed
        if self.error:
            raise self.error
        return {request.symbol_or_symbols: QUOTE}


class Trading:
    def __init__(self, contracts=("SPY261023C00500000",)):
        self.contracts = [SimpleNamespace(symbol=s) for s in contracts]

    def get_option_contracts(self, request):
        return SimpleNamespace(option_contracts=self.contracts)


def test_entitled_plan_passes_and_requests_real_time_feeds():
    stock, option = Stock(), Option()
    checks = check_data_entitlements(stock, option, Trading())
    assert [(c.feed, c.ok) for c in checks] == [("sip", True), ("opra", True)]
    assert stock.feed.value == "sip"
    assert option.feed.value == "opra"


def test_free_plan_fails_both_feeds():
    checks = check_data_entitlements(Stock(DENIED), Option(DENIED), Trading())
    assert [(c.feed, c.ok) for c in checks] == [("sip", False), ("opra", False)]
    assert "subscription" in checks[0].detail


def test_no_contracts_is_a_failure():
    checks = check_data_entitlements(Stock(), Option(), Trading(contracts=()))
    assert checks[1].ok is False


@pytest.mark.network
@pytest.mark.skipif(
    not os.environ.get("TOURNAMENT_NETWORK_TESTS"),
    reason="set TOURNAMENT_NETWORK_TESTS=1 to call the real data API",
)
def test_data_account_has_sip_and_opra():
    """D03 / MD-09: the data keys reach real-time SIP and OPRA quotes."""
    creds = load_alpaca_credentials(DATA_ACCOUNT_ALIAS)
    checks = check_data_entitlements(*data_clients(creds))
    assert all(c.ok for c in checks), checks
