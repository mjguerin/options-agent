"""Validate the D-04 trade-eligibility configuration (config/trade_eligibility)."""

import csv
from collections import defaultdict
from datetime import date
from pathlib import Path

import pytest
import yaml

CFG = Path(__file__).resolve().parents[1] / "config" / "trade_eligibility"
HISTORY_START = date(2024, 2, 1)
RULE_VALUES = {"YES", "NO", "NOTE_2", "NOTE_3", "NOTE_4", "TBD", "UNVERIFIED"}
RULE_COLUMNS = [
    "consolidated_last_rule",
    "consolidated_high_low_rule",
    "participant_open_rule",
    "participant_last_rule",
    "participant_high_low_rule",
    "volume_rule",
]


@pytest.fixture(scope="module")
def rows():
    with open(CFG / "cts_sale_conditions.csv", newline="") as f:
        return list(csv.DictReader(f))


@pytest.fixture(scope="module")
def notes():
    return yaml.safe_load((CFG / "notes.yaml").read_text())


@pytest.fixture(scope="module")
def adapter():
    return yaml.safe_load((CFG / "minute_bar_adapter.yaml").read_text())


def test_yaml_files_parse():
    for name in ("source_register", "notes", "minute_bar_adapter"):
        assert yaml.safe_load((CFG / f"{name}.yaml").read_text())


def test_source_urls_are_plain_strings():
    register = yaml.safe_load((CFG / "source_register.yaml").read_text())
    for src in register["sources"]:
        assert isinstance(src["url"], str) and src["url"].startswith("https://")


def test_rule_values_are_known(rows):
    for r in rows:
        for col in RULE_COLUMNS:
            assert r[col] in RULE_VALUES, (r["condition_code"], col, r[col])


def test_notes_are_defined(rows, notes):
    for r in rows:
        for n in filter(None, r["conditional_note_id"].split(";")):
            assert n in notes["notes"] or n.startswith("OPEN-SRC"), n


def test_effective_intervals_cover_history_without_overlap(rows):
    """effective_from inclusive, effective_to exclusive; per code, contiguous from history start."""
    by_code = defaultdict(list)
    for r in rows:
        lo = date.fromisoformat(r["effective_from"])
        hi = date.fromisoformat(r["effective_to"]) if r["effective_to"] else date.max
        assert lo < hi, r["condition_code"]
        by_code[(r["tape"], r["condition_code"])].append((lo, hi))
    for key, spans in by_code.items():
        spans.sort()
        assert spans[0][0] == HISTORY_START, key
        assert spans[-1][1] == date.max, key
        for (_, a_hi), (b_lo, _) in zip(spans, spans[1:], strict=False):
            assert a_hi == b_lo, f"gap or overlap for {key}"


def test_unverified_rows_have_scoped_assumptions(rows, adapter):
    assumed = {
        (a["code"], str(a["window"]["from"]), str(a["window"]["to_exclusive"])): a
        for a in adapter["assumptions"]
    }
    for r in rows:
        if r["verification_status"] == "unverified_source_gap":
            a = assumed.get((r["condition_code"], r["effective_from"], r["effective_to"]))
            assert a, f"no assumption covers {r['condition_code']} {r['effective_from']}"
            assert a["verification_status"] == "assumed"
            assert a["allowed_use"] == ["development"]


def test_vwap_population_is_named(adapter):
    assert adapter["flags"]["upd_vwap"]["population_name"] == "last_eligible_execution_trades"
