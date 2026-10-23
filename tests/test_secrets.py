import os

import pytest

from tournament.secrets import (
    InsecureEnvFileError,
    MissingCredentialsError,
    load_alpaca_credentials,
)

DEV = "ALPACA_DEV_KEY_ID=PKTEST\nALPACA_DEV_SECRET_KEY=s3cret\n"


def test_loads_from_env_file(env_file):
    env_file(DEV)
    creds = load_alpaca_credentials("dev")
    assert (creds.key_id, creds.secret_key) == ("PKTEST", "s3cret")


def test_does_not_leak_into_process_environment(env_file):
    env_file(DEV)
    load_alpaca_credentials("dev")
    assert "ALPACA_DEV_SECRET_KEY" not in os.environ


def test_accounts_are_isolated(env_file):
    env_file(DEV)
    with pytest.raises(MissingCredentialsError):
        load_alpaca_credentials("vwap_pullback")


def test_missing_file(env_file):
    with pytest.raises(MissingCredentialsError):
        load_alpaca_credentials("dev")


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604])
def test_rejects_file_readable_by_others(env_file, mode):
    env_file(DEV, mode)
    with pytest.raises(InsecureEnvFileError):
        load_alpaca_credentials("dev")


def test_rejects_empty_values(env_file):
    env_file("ALPACA_DEV_KEY_ID=PKTEST\nALPACA_DEV_SECRET_KEY=\n")
    with pytest.raises(MissingCredentialsError):
        load_alpaca_credentials("dev")


def test_secret_never_in_repr(env_file):
    env_file(DEV)
    creds = load_alpaca_credentials("dev")
    assert "s3cret" not in repr(creds)
    assert "PKTEST" not in str(creds)
