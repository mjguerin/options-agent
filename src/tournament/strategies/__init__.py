"""Strategy registry and the five strategy workers (BRD 6, 9 component 4).

Workers receive normalized events and shared feature snapshots, own their setup
state, emit structured candidates, and never call a broker (BRD 6.1, OM-01).
"""

from tournament.strategies.base import Outcome, StrategyWorker

__all__ = ["Outcome", "StrategyWorker"]
