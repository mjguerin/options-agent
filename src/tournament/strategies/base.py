"""Common strategy contract (BRD 6.1; specs/shared.md SD-10 to SD-13; schemas.md S-5 to S-7).

The replay harness (and later the engine) owns everything common: the session floor, last entry,
cutoff, the one-reference-position rule, cooldown, data-status handling, reference exits and all
records. A worker owns only its own state machine and decides, per slot, its outcome, its setup
transitions and whether a candidate fires.

Workers see a CAUSAL context: features for slots 1..m only. They can't read the future.
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import pandas as pd

from tournament.features.engine import SessionLevels


class Outcome(StrEnum):
    """The five outcomes every worker must expose; finer internal states are allowed."""

    NOT_READY = "NOT_READY"
    INELIGIBLE = "INELIGIBLE"
    WATCHING = "WATCHING"
    CANDIDATE = "CANDIDATE"
    INVALIDATED = "INVALIDATED"


class Block(StrEnum):
    """Common reasons the harness forbids starting a setup or emitting a candidate."""

    BEFORE_FLOOR = "BEFORE_FLOOR"
    AFTER_LAST_ENTRY = "AFTER_LAST_ENTRY"
    REF_POSITION_OPEN = "REF_POSITION_OPEN"
    COOLDOWN = "COOLDOWN"
    NO_PRICE = "NO_PRICE"  # KNOWN_NO_ELIGIBLE_TRADES: setups continue, no entries (SD-01)


@dataclass(frozen=True)
class SlotContext:
    session_id: str
    slot: int
    n_slots: int
    floor_slot: int
    last_entry_slot: int
    cutoff_slot: int
    history: pd.DataFrame  # features for slots 1..slot, in order (causal)
    levels: SessionLevels
    blocks: tuple[Block, ...]  # empty = may start setups and emit candidates
    cancel_reason: str | None  # DATA_MISSING_OR_UNCERTAIN / HALTED: cancel setups now (SD-01)

    @property
    def row(self) -> pd.Series:
        return self.history.iloc[-1]

    @property
    def may_emit(self) -> bool:
        return not self.blocks and self.cancel_reason is None


@dataclass(frozen=True)
class Transition:
    from_state: str
    to_state: str
    outcome: Outcome
    reason_codes: tuple[str, ...]
    setup_id: str | None
    anchors: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CandidateSpec:
    underlying_direction: str  # bullish | bearish
    entry_reference: float
    trigger_level: float
    invalidation_level: float
    target: dict[str, Any]  # {"type": "fixed_level"|"r_multiple", "price": float, ...}
    trail: dict[
        str, Any
    ]  # {"type": "none"} or {"type": "swing_trail", "activate_r": R, "buffer": price}
    timers: dict[str, Any]  # ft_bars, ft_r, stag_bars, max_hold (None = not used)
    features: dict[str, Any]
    reason_codes: tuple[str, ...]
    setup_id: str
    setup_ordinal: int


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    setup_state: str
    reason_codes: tuple[str, ...] = ()
    setup_id: str | None = None
    transitions: tuple[Transition, ...] = ()
    candidate: CandidateSpec | None = None


@dataclass(frozen=True)
class ReferenceExitEvent:
    candidate_id: str
    setup_id: str
    underlying_direction: str
    slot: int
    primary_reason: str
    triggered_reasons: tuple[str, ...]


class StrategyWorker(ABC):
    strategy_id: str
    version: str
    required_features: tuple[str, ...] = ()

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.config_hash = hashlib.sha256(
            json.dumps(config, sort_keys=True, default=str).encode()
        ).hexdigest()

    @abstractmethod
    def reset_session(self, session_id: str) -> None:
        """Start of session: clear setups, per-session quotas and brakes."""

    @abstractmethod
    def evaluate(self, ctx: SlotContext) -> Decision:
        """One finalized slot. Must not return a candidate unless ctx.may_emit."""

    def on_reference_exit(self, event: ReferenceExitEvent) -> None:  # noqa: B027
        """Reference position closed (never called for execution-only exits)."""
