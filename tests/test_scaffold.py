import importlib
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from tournament.strategies import Outcome

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "module", ["adapters", "features", "strategies", "accounts", "orders", "analytics"]
)
def test_modules_import(module):
    importlib.import_module(f"tournament.{module}")


def test_required_strategy_outcomes():
    # BRD 6.1: every worker exposes exactly these five outcomes.
    assert [o.value for o in Outcome] == [
        "NOT_READY",
        "INELIGIBLE",
        "WATCHING",
        "CANDIDATE",
        "INVALIDATED",
    ]


def test_alembic_config_loads():
    script = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert script.get_heads() == ["0001"]  # one linear history, no branches
