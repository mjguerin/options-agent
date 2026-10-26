"""What the dashboard shows, as plain functions of files and API answers (testable without a UI).

Reads only. The engine's published state and the API are the live sources; a session's folder
(orders, bars, summary) is the record. Nothing here changes a thing.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import httpx
import pandas as pd


class ApiError(RuntimeError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"{status}: {detail}")
        self.status, self.detail = status, detail


class ApiClient:
    """The control API over HTTP. Reads use the viewer token, controls the owner token."""

    def __init__(self, base_url: str, timeout_s: float, viewer: str, owner: str, http=None) -> None:
        self._http = http or httpx.Client(base_url=base_url, timeout=timeout_s)
        self._viewer = {"Authorization": f"Bearer {viewer}"}
        self._owner = {"Authorization": f"Bearer {owner}"}

    def _call(self, method: str, path: str, headers: dict, **kw) -> dict:
        try:
            r = self._http.request(method, path, headers=headers, **kw)
        except httpx.HTTPError as exc:
            raise ApiError(0, f"API unreachable: {type(exc).__name__}") from exc
        if r.status_code >= 400:
            detail = (
                r.json().get("detail", r.text)
                if r.headers.get("content-type", "").startswith("application/json")
                else r.text
            )
            raise ApiError(r.status_code, str(detail))
        return r.json()

    def status(self) -> dict:
        return self._call("GET", "/v1/status", self._viewer)

    def incidents(self, limit: int = 50) -> list[dict]:
        return self._call("GET", "/v1/incidents", self._viewer, params={"limit": limit})[
            "incidents"
        ]

    def audit(self, limit: int = 100) -> list[dict]:
        return self._call("GET", "/v1/audit", self._viewer, params={"limit": limit})["actions"]

    def command(self, command_id: str) -> dict:
        return self._call("GET", f"/v1/commands/{command_id}", self._viewer)

    def control(self, body: dict) -> dict:
        return self._call("POST", "/v1/controls", self._owner, json=body)


# ---- health ------------------------------------------------------------------------------------


def account_status(a: dict) -> str:
    """One word per account, the most serious first."""
    if a.get("entries_blocked"):
        return "BLOCKED"
    if a.get("halted"):
        return "HALTED"
    oh = a.get("operator_halt")
    if oh:
        return "FLATTENED" if "FLATTEN" in oh or "EMERGENCY" in oh else "PAUSED"
    return "OK"


def engine_light(status: dict) -> tuple[str, str]:
    """(colour, words) for the engine indicator."""
    if not status.get("engine_reporting"):
        return "red", "engine not reporting"
    feeds = (status.get("state") or {}).get("feeds", {})
    down = [k for k, v in feeds.items() if not v.get("connected", False)]
    if down:
        return "amber", "feed down: " + ", ".join(down)
    return "green", "engine reporting"


def accounts_table(state: dict | None) -> pd.DataFrame:
    rows = []
    for binding, a in sorted(((state or {}).get("accounts") or {}).items()):
        unreal = sum((p.get("unrealized_usd") or 0.0) for p in a.get("positions_detail", []))
        rows.append(
            {
                "account": binding,
                "status": account_status(a),
                "positions": sum(a.get("positions", {}).values()),
                "exposure $": a.get("exposure_usd", 0.0),
                "realized $": a.get("realized_pnl_usd", 0.0),
                "unrealized $": round(unreal, 2),
                "risk $": (a.get("pnl_view") or {}).get("risk_usd"),
                "unmarked": (a.get("pnl_view") or {}).get("unmarked_positions", 0),
                "pending orders": a.get("pending_orders", 0),
                "decisions": a.get("decisions", 0),
            }
        )
    return pd.DataFrame(rows)


# ---- the curves --------------------------------------------------------------------------------


def load_history(path: Path) -> pd.DataFrame:
    """Per-account total profit and loss over the session, one column per account."""
    rows = []
    try:
        for line in path.read_text().splitlines():
            r = json.loads(line)
            row = {"at": pd.Timestamp(r["at"])}
            for b, v in r["accounts"].items():
                row[b] = v["realized_pnl_usd"] + v["unrealized_usd"]
            rows.append(row)
    except (OSError, ValueError, KeyError):
        pass
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).set_index("at").sort_index()


def drawdown(pnl: pd.DataFrame, start_balance: float) -> pd.DataFrame:
    """Drop from each account's running peak equity, in dollars (zero or negative)."""
    equity = pnl + start_balance
    return equity - equity.cummax()


# ---- session folders ---------------------------------------------------------------------------


def session_dirs(live_root: Path, session: str | None = None) -> list[Path]:
    """Run folders of one session (newest first). Default: the latest session on disk."""
    days = sorted((p for p in live_root.glob("*") if p.is_dir()), reverse=True)
    if session:
        days = [d for d in days if d.name == session]
    if not days:
        return []
    return sorted((p for p in days[0].iterdir() if p.is_dir()), reverse=True)


def read_orders(db: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The modeled accounts' orders and their event log, read-only."""
    if not db.exists():
        return pd.DataFrame(), pd.DataFrame()
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        orders = pd.read_sql_query("select * from orders order by created_at", con)
        events = pd.read_sql_query("select * from order_events order by id", con)
    finally:
        con.close()
    return orders, events


def fill_latencies(orders: pd.DataFrame, events: pd.DataFrame) -> pd.DataFrame:
    """Seconds from submission to the first fill, per order (NFR-02)."""
    if orders.empty or events.empty:
        return pd.DataFrame(columns=["client_order_id", "latency_s"])
    ev = events.copy()
    ev["at"] = pd.to_datetime(ev["at"], utc=True)
    out = []
    for cid, g in ev.groupby("client_order_id"):
        sub = g[g["kind"] == "submit"]["at"]
        filled = g[(g["kind"] == "status") & g["detail"].str.contains("FILLED")]["at"]
        if len(sub) and len(filled):
            out.append(
                {
                    "client_order_id": cid,
                    "latency_s": (filled.iloc[0] - sub.iloc[0]).total_seconds(),
                }
            )
    return pd.DataFrame(out, columns=["client_order_id", "latency_s"])


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def ops_reports(ops_root: Path, limit: int = 7) -> list[dict]:
    files = sorted(ops_root.glob("nightly_*.json"), reverse=True)[:limit]
    return [r for r in (read_json(f) for f in files) if r]


@dataclass(frozen=True)
class Prepared:
    """A control waiting for the owner's typed confirmation."""

    body: dict
    confirm_id: str
    expires_at: str
    will_do: str
    positions: dict
    phrase: str
