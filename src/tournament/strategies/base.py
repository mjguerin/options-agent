"""Common strategy contract (BRD 6.1). Candidate and snapshot schemas land in Step 2 (D05)."""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol


class Outcome(StrEnum):
    """The five outcomes every worker must expose; finer internal states are allowed."""

    NOT_READY = "NOT_READY"
    INELIGIBLE = "INELIGIBLE"
    WATCHING = "WATCHING"
    CANDIDATE = "CANDIDATE"
    INVALIDATED = "INVALIDATED"


class StrategyWorker(Protocol):
    strategy_id: str
    version: str

    @property
    def outcome(self) -> Outcome: ...
