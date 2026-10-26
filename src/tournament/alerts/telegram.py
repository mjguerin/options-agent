"""Sending to Telegram (the bot API's sendMessage). One chat only, no incoming commands.

The token never appears in a log line, a traceback or a stored error: every error text is
scrubbed of it first. Failures come back as results so the bot can back off and retry; nothing
here raises into the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import requests

from tournament.alerts.config import Telegram
from tournament.secrets import TelegramCredentials

MAX_TEXT = 4000  # the API's limit is 4096 characters


@dataclass(frozen=True)
class SendResult:
    ok: bool
    retry_after_s: float | None = None
    error: str | None = None


class Sender(Protocol):
    def send(self, text: str) -> SendResult: ...


class TelegramSender:
    def __init__(self, creds: TelegramCredentials, cfg: Telegram, http=requests) -> None:
        self._creds, self._cfg, self._http = creds, cfg, http

    def _scrub(self, text: str) -> str:
        return text.replace(self._creds.bot_token, "***")

    def send(self, text: str) -> SendResult:
        url = f"{self._cfg.api_base}/bot{self._creds.bot_token}/sendMessage"
        body = {
            "chat_id": self._creds.chat_id,
            "text": text[:MAX_TEXT],
            "disable_web_page_preview": True,
        }
        try:
            r = self._http.post(url, json=body, timeout=self._cfg.timeout_s)
        except Exception as exc:  # noqa: BLE001 - network errors of every kind become a result
            return SendResult(False, None, self._scrub(f"{type(exc).__name__}: {exc}"))
        if r.status_code == 200:
            return SendResult(True)
        retry = None
        try:
            retry = float(r.json().get("parameters", {}).get("retry_after"))
        except (ValueError, TypeError, AttributeError):
            pass
        return SendResult(False, retry, self._scrub(f"HTTP {r.status_code}: {r.text[:200]}"))
