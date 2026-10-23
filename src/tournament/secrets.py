"""Broker credentials from a local .env file (owner decision, replaces the Keychain of BRD 9.1).

The file sits in the repo root, is gitignored, and must be readable by its owner
only (mode 600). That keeps it away from the agent's separate macOS user (AC-24).
Values are read into memory, never copied into os.environ, so child processes
and logs don't inherit them (NFR-07).

Each paper account has an alias ("dev", later one per strategy):
    ALPACA_<ALIAS>_KEY_ID=...
    ALPACA_<ALIAS>_SECRET_KEY=...
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

DEFAULT_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class MissingCredentialsError(RuntimeError):
    pass


class InsecureEnvFileError(RuntimeError):
    pass


@dataclass(frozen=True)
class AlpacaCredentials:
    account_alias: str
    key_id: str
    secret_key: str

    def __repr__(self) -> str:
        # Never let the secret reach a log line or traceback.
        return f"AlpacaCredentials({self.account_alias!r}, key_id=***, secret_key=***)"

    __str__ = __repr__


def env_file() -> Path:
    return Path(os.environ.get("TOURNAMENT_ENV_FILE", DEFAULT_ENV_FILE))


def _variable(account_alias: str, field: str) -> str:
    return f"ALPACA_{account_alias.upper()}_{field}"


def _check_permissions(path: Path) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise InsecureEnvFileError(
            f"{path} is readable by other users (mode {mode:o}). Run: chmod 600 {path}"
        )


def load_alpaca_credentials(account_alias: str, path: Path | None = None) -> AlpacaCredentials:
    path = path or env_file()
    if not path.is_file():
        raise MissingCredentialsError(
            f"No {path}. Copy .env.example to .env, fill in the keys, then chmod 600 .env"
        )
    _check_permissions(path)
    values = dotenv_values(path)
    key_var, secret_var = _variable(account_alias, "KEY_ID"), _variable(account_alias, "SECRET_KEY")
    key_id, secret_key = values.get(key_var), values.get(secret_var)
    if not key_id or not secret_key:
        raise MissingCredentialsError(
            f"{path} has no {key_var} and {secret_var} for account alias {account_alias!r}"
        )
    return AlpacaCredentials(account_alias, key_id, secret_key)
