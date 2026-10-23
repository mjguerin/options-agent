import os

import pytest

from tournament.adapters.alpaca import (
    PAPER_TRADING_URL,
    LiveEndpointRejectedError,
    assert_paper_url,
    get_account_summary,
    paper_trading_client,
    trading_base_url,
)
from tournament.secrets import AlpacaCredentials, load_alpaca_credentials


def test_live_endpoint_rejected():
    with pytest.raises(LiveEndpointRejectedError):
        assert_paper_url("https://api.alpaca.markets")


def test_paper_endpoint_accepted():
    assert_paper_url(PAPER_TRADING_URL + "/")


def test_client_is_always_paper():
    client = paper_trading_client(AlpacaCredentials("dev", "PKTEST", "s3cret"))
    assert trading_base_url(client).rstrip("/") == PAPER_TRADING_URL


@pytest.mark.network
@pytest.mark.skipif(
    not os.environ.get("TOURNAMENT_NETWORK_TESTS"),
    reason="set TOURNAMENT_NETWORK_TESTS=1 to call the real paper API",
)
def test_hello_world_against_paper_api():
    """Step 1 gate: read the dev paper account with keys from .env."""
    summary = get_account_summary(paper_trading_client(load_alpaca_credentials("dev")))
    assert summary.status == "ACTIVE"
    assert summary.equity > 0
