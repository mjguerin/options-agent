"""Trade eligibility from the D-04 configuration (specs/eligibility.md, SD-03).

Stage 1 of the adapter: classify each trade's sale conditions against the CTS matrix for its
date, then resolve consolidated-last eligibility against SIP state, updating that state from the
RESOLVED result. Application inclusion (stage 2) lives in bars.py and never writes SIP state.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

import yaml

CONFIG_DIR = Path(__file__).resolve().parents[3] / "config" / "trade_eligibility"
SPACE = "SPACE"
NOTES = ("NOTE_2", "NOTE_3")
FAIL_CLOSED = ("UNVERIFIED", "TBD")


class Usage(StrEnum):
    """What the bars are for. Development-only assumptions (E-5) apply only in DEVELOPMENT."""

    DEVELOPMENT = "development"
    HOLDOUT = "holdout"
    LIVE = "live"


@dataclass(frozen=True)
class Row:
    code: str
    category: int
    start: date
    end: date | None  # exclusive
    last: str
    high_low: str
    volume: str


@dataclass(frozen=True)
class Classified:
    """One trade's stage-1 classification, before stateful notes are resolved."""

    last: str  # YES, NO, or a "|"-joined set of notes
    volume: str  # YES or NO
    informational: bool
    unknown: bool  # quarantine: the slot becomes DATA_MISSING_OR_UNCERTAIN (D-36)
    flags: frozenset[str] = field(default_factory=frozenset)


class EligibilityConfig:
    def __init__(self, config_dir: Path = CONFIG_DIR) -> None:
        self.rows: dict[str, list[Row]] = {}
        with open(config_dir / "cts_sale_conditions.csv", newline="") as f:
            for r in csv.DictReader(f):
                row = Row(
                    code=r["condition_code"],
                    category=int(r["cts_category"]),
                    start=date.fromisoformat(r["effective_from"]),
                    end=date.fromisoformat(r["effective_to"]) if r["effective_to"] else None,
                    last=r["consolidated_last_rule"],
                    high_low=r["consolidated_high_low_rule"],
                    volume=r["volume_rule"],
                )
                self.rows.setdefault(row.code, []).append(row)
        adapter = yaml.safe_load((config_dir / "minute_bar_adapter.yaml").read_text())
        self.informational = frozenset(adapter["informational_conditions"]["codes"])
        self.listing_market: dict[str, str] = dict(adapter["listing_market"])
        self.assumptions = {
            (a["code"], a["window"]["from"], a["window"]["to_exclusive"]): a
            for a in adapter["assumptions"]
        }
        self._classify = lru_cache(maxsize=4096)(self._classify_uncached)

    def row(self, code: str, day: date) -> Row | None:
        for r in self.rows.get(code, ()):
            if r.start <= day and (r.end is None or day < r.end):
                return r
        return None

    def _assumed_row(self, row: Row, usage: Usage) -> tuple[Row | None, str | None]:
        """Apply a scoped E-5 assumption to an UNVERIFIED row, if this usage allows it."""
        a = self.assumptions.get((row.code, row.start, row.end))
        if a is None or usage.value not in a["allowed_use"]:
            return None, None
        if row.code == "N":  # price NO, volume YES (OPEN-SRC-2)
            return Row(
                row.code, row.category, row.start, row.end, "NO", "NO", "YES"
            ), "provider_evidence"
        if row.code == "O":  # the later row's rules (OPEN-SRC-1)
            later = self.row(row.code, row.end) if row.end else None
            return (later, "rule_backfilled") if later else (None, None)
        return None, None

    def classify(self, conditions: tuple[str, ...], day: date, usage: Usage) -> Classified:
        return self._classify(tuple(conditions), day, usage)

    def _classify_uncached(
        self, conditions: tuple[str, ...], day: date, usage: Usage
    ) -> Classified:
        codes = [SPACE if c == " " else c for c in conditions] or [SPACE]
        rows, flags = [], set()
        for c in codes:
            r = self.row(c, day)
            if r is None:
                return Classified("NO", "NO", False, True, frozenset({f"unknown_condition:{c}"}))
            if FAIL_CLOSED[0] in (r.last, r.high_low, r.volume):
                r, flag = self._assumed_row(r, usage)
                if r is None:
                    return Classified("NO", "NO", False, True, frozenset({f"unverified_rule:{c}"}))
                flags.add(f"{flag}:{c}")
            rows.append(r)
        if violation := _constraint_violation(rows):
            return Classified("NO", "NO", False, True, frozenset({f"combination:{violation}"}))
        info = any(r.code in self.informational for r in rows)
        last = _combine([r.last for r in rows])
        volume = _combine([r.volume for r in rows])
        if "UNKNOWN" in (last, volume):
            return Classified("NO", "NO", info, True, frozenset({"tbd_rule"}))
        return Classified(last, "YES" if volume == "YES" else "NO", info, False, frozenset(flags))


def _combine(values: list[str]) -> str:
    """CTS p83: any NO wins; a fail-closed value is UNKNOWN; else notes decide, else YES."""
    if "NO" in values:
        return "NO"
    if any(v in FAIL_CLOSED for v in values):
        return "UNKNOWN"
    notes = sorted({v for v in values if v in NOTES})
    return "|".join(notes) if notes else "YES"


def _constraint_violation(rows: list[Row]) -> str | None:
    """CTS p85 combination constraints (C1-C3). SPACE never counts."""
    real = [r for r in rows if r.code != SPACE]
    cats = [r.category for r in real]
    if len(cats) != len(set(cats)):
        return "C1_same_category"
    codes = {r.code for r in real}
    if "U" in codes and any(r.category == 2 for r in real):
        return "C2_U_with_category_2"
    if len(codes & {"L", "O", "P", "Z"}) > 1:
        return "C3_L_O_P_Z"
    return None


@dataclass
class LastState:
    """Per security and day. Updated only from RESOLVED eligibility (notes.yaml)."""

    listing_market: str
    has_qualifying_last: bool = False
    last_participant: str | None = None

    def resolve(self, last_rule: str, participant: str) -> bool:
        if last_rule == "NO":
            return False
        ok = True
        if "NOTE_2" in last_rule:
            ok &= not self.has_qualifying_last
        if "NOTE_3" in last_rule:
            ok &= (
                not self.has_qualifying_last
                or participant == self.last_participant
                or participant == self.listing_market
            )
        if ok:
            self.has_qualifying_last = True
            self.last_participant = participant
        return ok
