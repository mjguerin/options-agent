"""Canonical hashing and record identity (schemas.md S-3).

Canonical JSON: sorted keys, no whitespace, UTF-8, explicit nulls; floats rounded (prices to
1e-6, everything else to 1e-9) and never NaN or infinite. SHA-256, lowercase hex.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime
from typing import Any

PRICE_DECIMALS = 6
RATIO_DECIMALS = 9


class NonFiniteValueError(ValueError):
    pass


def _norm(value: Any, price_fields: frozenset[str], key: str | None = None) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, (str, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise NonFiniteValueError(f"non-finite value for {key!r}")
        return round(value, PRICE_DECIMALS if key in price_fields else RATIO_DECIMALS)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _norm(v, price_fields, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_norm(v, price_fields, key) for v in value]
    if hasattr(value, "item"):  # numpy scalar
        return _norm(value.item(), price_fields, key)
    raise TypeError(f"can't canonicalize {type(value).__name__} for {key!r}")


def canonical_json(obj: Any, price_fields: frozenset[str] = frozenset()) -> str:
    return json.dumps(
        _norm(obj, price_fields), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def digest(obj: Any, price_fields: frozenset[str] = frozenset()) -> str:
    return hashlib.sha256(canonical_json(obj, price_fields).encode()).hexdigest()


def signal_key(
    strategy_id: str,
    strategy_version: str,
    config_hash: str,
    dataset_id: str,
    session_id: str,
    setup_ordinal: int,
    slot: int,
) -> str:
    """Stable across reruns, so runs can be compared (S-3)."""
    return digest(
        {
            "strategy_id": strategy_id,
            "strategy_version": strategy_version,
            "config_hash": config_hash,
            "dataset_id": dataset_id,
            "session_id": session_id,
            "setup_ordinal": setup_ordinal,
            "slot": slot,
        }
    )


def run_scoped(run_id: str, kind: str, **parts: Any) -> str:
    """Record IDs unique per run: candidate, evaluation, transition, reference position."""
    return digest({"run_id": run_id, "kind": kind, **parts})
