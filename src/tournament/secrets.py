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


@dataclass(frozen=True)
class TelegramCredentials:
    bot_token: str
    chat_id: str

    def __repr__(self) -> str:
        return "TelegramCredentials(bot_token=***, chat_id=***)"

    __str__ = __repr__


@dataclass(frozen=True)
class ApiTokens:
    owner: str
    viewer: str

    def __repr__(self) -> str:
        return "ApiTokens(owner=***, viewer=***)"

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


class LiveCredentialsRejectedError(RuntimeError):
    """A key that is not a paper key: this system trades paper only (AC-15)."""


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
    if key_id.strip().upper().startswith("AK"):  # Alpaca live keys begin AK, paper keys PK
        raise LiveCredentialsRejectedError(
            f"{key_var} looks like a live-trading key; only paper keys are accepted"
        )
    return AlpacaCredentials(account_alias, key_id, secret_key)


def load_telegram_credentials(path: Path | None = None) -> TelegramCredentials:
    """The alert bot's token and the owner's chat ID (alerts go to one chat only)."""
    path = path or env_file()
    if not path.is_file():
        raise MissingCredentialsError(f"No {path}. See .env.example for the Telegram variables")
    _check_permissions(path)
    values = dotenv_values(path)
    token, chat = values.get("TELEGRAM_BOT_TOKEN"), values.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        raise MissingCredentialsError(f"{path} has no TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
    return TelegramCredentials(token, chat)


def load_api_tokens(path: Path | None = None) -> ApiTokens:
    """Bearer tokens for the control API: `owner` may control, `viewer` may only read. The two
    must differ, so a token handed to a read-only tool can never control anything."""
    path = path or env_file()
    if not path.is_file():
        raise MissingCredentialsError(f"No {path}. See .env.example for the API tokens")
    _check_permissions(path)
    values = dotenv_values(path)
    owner, viewer = values.get("API_OWNER_TOKEN"), values.get("API_VIEWER_TOKEN")
    if not owner or not viewer:
        raise MissingCredentialsError(f"{path} has no API_OWNER_TOKEN and API_VIEWER_TOKEN")
    if owner == viewer:
        raise MissingCredentialsError("API_OWNER_TOKEN and API_VIEWER_TOKEN must differ")
    if min(len(owner), len(viewer)) < 32:
        raise MissingCredentialsError("API tokens must be at least 32 characters")
    return ApiTokens(owner, viewer)
