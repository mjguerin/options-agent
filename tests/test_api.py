"""The control API: authentication, roles, audit, two-step confirmation, the command queue, and
the engine applying commands. The API only queues; the engine decides."""

import json
import os

import pandas as pd
import pytest
import yaml
from fastapi.testclient import TestClient
from test_account_runtime import CALL, build, cand, prices

from tournament.alerts.config import load_alerts_config
from tournament.alerts.store import AlertStore
from tournament.api.app import create_app
from tournament.api.config import DEFAULT_PATH, ApiConfigError, load_api_config
from tournament.api.store import ControlStore
from tournament.live.controls import ControlPlane
from tournament.orders.store import memory_engine
from tournament.secrets import ApiTokens, MissingCredentialsError, load_api_tokens

CFG = load_api_config()
OWNER, VIEWER = "o" * 40, "v" * 40
T0 = pd.Timestamp("2026-10-26T15:00:00Z")
ACCT = "acct:vpc"
H = {"owner": {"Authorization": f"Bearer {OWNER}"}, "viewer": {"Authorization": f"Bearer {VIEWER}"}}


def at(s: float) -> pd.Timestamp:
    return T0 + pd.Timedelta(seconds=s)


class Clock:
    def __init__(self):
        self.t = at(0)

    def __call__(self):
        return self.t


@pytest.fixture()
def env(tmp_path):
    engine = memory_engine()
    store, clock = ControlStore(engine), Clock()
    state = tmp_path / "state.json"
    app = create_app(
        store, CFG, ApiTokens(OWNER, VIEWER), state, AlertStore(engine, load_alerts_config()), clock
    )
    return SimpleEnv(TestClient(app), store, clock, state, engine)


class SimpleEnv:
    def __init__(self, client, store, clock, state, engine):
        self.c, self.store, self.clock, self.state, self.engine = (
            client,
            store,
            clock,
            state,
            engine,
        )

    def post(self, body, who="owner"):
        return self.c.post("/v1/controls", json=body, headers=H[who])

    def audit(self):
        return self.store.recent_actions(50)

    def publish(self, accounts=None, age=0.0):
        self.state.write_text(
            json.dumps(
                {
                    "at": (self.clock() - pd.Timedelta(seconds=age)).isoformat(),
                    "accounts": accounts or {},
                }
            )
        )


# ---- config and tokens ----


def test_the_server_may_only_bind_a_loopback_address(tmp_path):  # NFR-14
    raw = yaml.safe_load(DEFAULT_PATH.read_text())
    raw["server"]["host"] = "0.0.0.0"
    p = tmp_path / "a.yaml"
    p.write_text(yaml.safe_dump(raw))
    with pytest.raises(ApiConfigError, match="loopback"):
        load_api_config(p)
    raw["server"]["host"] = "::1"
    p.write_text(yaml.safe_dump(raw))
    assert load_api_config(p).server.host == "::1"


def test_tokens_must_be_present_long_and_different(tmp_path):
    env_file = tmp_path / ".env"
    for body in (
        "API_OWNER_TOKEN=\n",
        f"API_OWNER_TOKEN={'a' * 40}\nAPI_VIEWER_TOKEN={'a' * 40}\n",
        "API_OWNER_TOKEN=short\nAPI_VIEWER_TOKEN=tiny\n",
    ):
        env_file.write_text(body)
        os.chmod(env_file, 0o600)
        with pytest.raises(MissingCredentialsError):
            load_api_tokens(env_file)
    env_file.write_text(f"API_OWNER_TOKEN={'a' * 40}\nAPI_VIEWER_TOKEN={'b' * 40}\n")
    t = load_api_tokens(env_file)
    assert t.owner == "a" * 40 and "aaaa" not in repr(t)


# ---- authentication and roles ----


def test_health_is_open_and_everything_else_needs_a_token(env):
    assert env.c.get("/health").json() == {"ok": True}
    for path in ("/v1/status", "/v1/audit"):
        assert env.c.get(path).status_code == 401
        assert env.c.get(path, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert (
        env.c.post("/v1/controls", json={"command": "PAUSE_ENTRIES", "scope": "global"}).status_code
        == 401
    )
    assert [a["outcome"] for a in env.audit()].count("UNAUTHENTICATED") == 5


def test_the_viewer_reads_but_can_never_control(env):
    assert env.c.get("/v1/status", headers=H["viewer"]).status_code == 200
    assert env.c.get("/v1/audit", headers=H["viewer"]).status_code == 200
    r = env.post({"command": "PAUSE_ENTRIES", "scope": "global"}, "viewer")
    assert r.status_code == 403 and env.store.pending(env.clock()) == []
    assert env.audit()[0]["outcome"] == "DENIED" and env.audit()[0]["actor"] == "viewer"


# ---- controls and the two-step confirmation ----


def test_a_pause_is_queued_at_once_and_audited(env):
    r = env.post({"command": "PAUSE_ENTRIES", "scope": "account", "account": ACCT})
    assert r.status_code == 202 and r.json()["status"] == "PENDING"
    assert r.json()["engine_reporting"] is False and "not reporting" in r.json()["note"]
    (cmd,) = env.store.pending(env.clock())
    assert (cmd.command, cmd.account, cmd.actor) == ("PAUSE_ENTRIES", ACCT, "owner")
    assert env.audit()[0]["outcome"] == "ACCEPTED" and env.audit()[0]["command_id"] == cmd.id


def test_an_exposure_changing_control_needs_a_confirmation_naming_what_is_at_stake(env):
    env.publish({ACCT: {"positions": {CALL: 13}}, "acct:orb": {"positions": {}}})
    body = {"command": "FLATTEN_ACCOUNT", "scope": "account", "account": ACCT}
    first = env.post(body)
    j = first.json()
    assert first.status_code == 202 and j["needs_confirmation"] is True
    assert j["positions"] == {ACCT: {CALL: 13}} and "sell every position" in j["will_do"]
    assert env.store.pending(env.clock()) == []  # nothing queued yet
    second = env.post({**body, "confirm_id": j["confirm_id"]})
    assert second.json()["status"] == "PENDING" and len(env.store.pending(env.clock())) == 1
    assert [a["outcome"] for a in env.audit()][:2] == ["ACCEPTED", "CONFIRMATION_ISSUED"]


def test_a_confirmation_works_once_only(env):
    body = {"command": "EMERGENCY", "scope": "global"}
    cid = env.post(body).json()["confirm_id"]
    assert env.post({**body, "confirm_id": cid}).status_code == 202
    again = env.post({**body, "confirm_id": cid})
    assert again.status_code == 409 and "already used" in again.json()["detail"]
    assert len(env.store.pending(env.clock())) == 1


def test_a_confirmation_cannot_be_used_for_a_different_action(env):
    cid = env.post({"command": "FLATTEN_ACCOUNT", "scope": "account", "account": ACCT}).json()[
        "confirm_id"
    ]
    other = env.post(
        {"command": "FLATTEN_ACCOUNT", "scope": "account", "account": "acct:orb", "confirm_id": cid}
    )
    assert other.status_code == 409 and "different action" in other.json()["detail"]
    wrong = env.post({"command": "EMERGENCY", "scope": "global", "confirm_id": cid})
    assert wrong.status_code == 409
    assert env.store.pending(env.clock()) == []
    assert (
        env.post({"command": "EMERGENCY", "scope": "global", "confirm_id": "bogus"}).status_code
        == 409
    )


def test_a_confirmation_expires(env):
    body = {"command": "RESUME_ENTRIES", "scope": "global"}
    cid = env.post(body).json()["confirm_id"]
    env.clock.t = at(CFG.commands.confirmation_ttl_s + 1)
    r = env.post({**body, "confirm_id": cid})
    assert r.status_code == 409 and "expired" in r.json()["detail"]


def test_an_emergency_still_queues_when_the_engine_is_not_reporting(env):
    body = {"command": "EMERGENCY", "scope": "global"}
    cid = env.post(body).json()["confirm_id"]
    r = env.post({**body, "confirm_id": cid}).json()
    assert r["status"] == "PENDING" and r["engine_reporting"] is False


@pytest.mark.parametrize(
    "body",
    [
        {"command": "SELL_EVERYTHING", "scope": "global"},
        {"command": "EMERGENCY", "scope": "account", "account": ACCT},
        {"command": "FLATTEN_ACCOUNT", "scope": "global"},
        {"command": "PAUSE_ENTRIES", "scope": "account", "account": "acct:ghost"},
        {"command": "PAUSE_ENTRIES", "scope": "account"},
        {"command": "PAUSE_ENTRIES", "scope": "global", "account": ACCT},
        {"command": "CLOSE_POSITION", "scope": "account", "account": ACCT},
        {
            "command": "CLOSE_POSITION",
            "scope": "account",
            "account": ACCT,
            "symbol": "AAPL251121C00200000",
        },
        {"command": "PAUSE_ENTRIES", "scope": "global", "symbol": CALL},
        {"command": "PAUSE_ENTRIES", "scope": "global", "surprise": 1},
    ],
)
def test_malformed_controls_are_refused_and_audited(env, body):
    r = env.post(body)
    assert r.status_code == 422 and env.store.pending(env.clock()) == []
    if "surprise" not in body:
        assert env.audit()[0]["outcome"] == "INVALID"


# ---- status ----


def test_status_reports_freshness_commands_and_incidents(env):
    env.publish({ACCT: {"positions": {}}}, age=1)
    s = env.c.get("/v1/status", headers=H["viewer"]).json()
    assert s["engine_reporting"] is True and ACCT in s["state"]["accounts"]
    env.publish({ACCT: {"positions": {}}}, age=CFG.state.max_age_s + 5)
    assert env.c.get("/v1/status", headers=H["viewer"]).json()["engine_reporting"] is False
    AlertStore(env.engine, load_alerts_config()).open(
        T0, "STOCK_FEED_DOWN", "engine", "not connected"
    )
    inc = env.c.get("/v1/status", headers=H["viewer"]).json()["open_incidents"]
    assert [i["kind"] for i in inc] == ["STOCK_FEED_DOWN"]


def test_a_restarted_api_still_has_the_queued_command(env, tmp_path):
    env.post({"command": "PAUSE_ENTRIES", "scope": "global"})
    app2 = create_app(env.store, CFG, ApiTokens(OWNER, VIEWER), env.state, None, env.clock)
    s = TestClient(app2).get("/v1/status", headers=H["viewer"]).json()
    assert [c["command"] for c in s["pending_commands"]] == ["PAUSE_ENTRIES"]


# ---- the engine applies commands ----


@pytest.fixture()
def live(env, tmp_path):
    """One real account runtime holding a position, and the control plane that serves it."""
    rt, sim, market = build()
    prices(market, at(0))
    rt.on_candidate(at(0), cand())
    for s in (1.5, 2.5):
        prices(market, at(s))
        rt.on_tick(at(s))
    assert rt.orders.position(CALL) == 13
    session = type("S", (), {})()
    session.health = type("H", (), {"connected": True})()
    session.day = type("D", (), {"session_date": T0.date()})()
    session.builder = type("B", (), {"finalized": 3})()
    plane = ControlPlane(
        env.store, CFG, {ACCT: rt}, tmp_path / "engine.state.json", session, "shadow"
    )
    return plane, rt, market


def run_command(env, plane, now, body, confirm=True):
    r = env.post(body)
    if confirm and r.json().get("needs_confirmation"):
        r = env.post({**body, "confirm_id": r.json()["confirm_id"]})
    cid = r.json()["command_id"]
    plane.tick(now)
    return env.c.get(f"/v1/commands/{cid}", headers=H["viewer"]).json()


def test_a_pause_stops_new_entries_and_leaves_positions_alone(env, live):
    plane, rt, market = live
    done = run_command(env, plane, at(3), {"command": "PAUSE_ENTRIES", "scope": "global"})
    assert done["status"] == "APPLIED" and rt.operator_halt == "OPERATOR_PAUSE"
    d = rt.on_candidate(at(3), cand("c2", expires_at=at(100)))
    assert d.reasons == ("OPERATOR_PAUSE",) and rt.exits_requested == []
    assert rt.orders.position(CALL) == 13


def test_resume_is_validated_by_the_engine(env, live):
    plane, rt, market = live
    run_command(env, plane, at(3), {"command": "PAUSE_ENTRIES", "scope": "global"})
    rt.halted = "DAILY_LIMIT"
    blocked = run_command(env, plane, at(4), {"command": "RESUME_ENTRIES", "scope": "global"})
    assert blocked["status"] == "REJECTED" and blocked["result"][ACCT]["blocked_by"] == [
        "DAILY_LIMIT"
    ]
    rt.halted = None
    prices(market, at(5))
    ok = run_command(env, plane, at(5), {"command": "RESUME_ENTRIES", "scope": "global"})
    assert ok["status"] == "APPLIED" and rt.operator_halt is None


def test_close_position_sells_that_one_position(env, live):
    plane, rt, market = live
    prices(market, at(6))
    done = run_command(
        env,
        plane,
        at(6),
        {"command": "CLOSE_POSITION", "scope": "account", "account": ACCT, "symbol": CALL},
    )
    assert done["status"] == "APPLIED" and [r for _, _, r in rt.exits_requested] == [
        "OPERATOR_CLOSE"
    ]
    none = run_command(
        env,
        plane,
        at(7),
        {
            "command": "CLOSE_POSITION",
            "scope": "account",
            "account": ACCT,
            "symbol": CALL.replace("765", "766"),
        },
    )
    assert none["status"] == "REJECTED" and "holds no" in none["result"]["error"]


def test_flatten_cancels_sells_and_halts_entries(env, live):
    plane, rt, market = live
    prices(market, at(6))
    done = run_command(
        env, plane, at(6), {"command": "FLATTEN_ACCOUNT", "scope": "account", "account": ACCT}
    )
    assert done["status"] == "APPLIED" and done["result"][ACCT]["exits_placed"] == [CALL]
    assert rt.operator_halt == "OPERATOR_FLATTEN_ACCOUNT"
    for s in (7.5, 8.5):
        prices(market, at(s))
        rt.on_tick(at(s))
    assert rt.orders.position(CALL) == 0


def test_an_emergency_flattens_every_account(env, live):
    plane, rt, market = live
    prices(market, at(6))
    done = run_command(env, plane, at(6), {"command": "EMERGENCY", "scope": "global"})
    assert done["status"] == "APPLIED" and rt.operator_halt == "OPERATOR_EMERGENCY"
    assert [r for _, _, r in rt.exits_requested] == ["OPERATOR_EMERGENCY"]


def test_the_result_and_the_audit_trail_record_what_the_engine_did(env, live):
    plane, rt, market = live
    done = run_command(
        env, plane, at(3), {"command": "PAUSE_ENTRIES", "scope": "account", "account": ACCT}
    )
    rows = env.audit()
    assert rows[0]["outcome"] == "APPLIED" and rows[0]["command_id"] == done["id"]
    assert "paused" in rows[0]["detail"]


def test_an_account_the_session_does_not_have_is_refused_not_ignored(env, live):
    plane, rt, market = live
    done = run_command(
        env, plane, at(3), {"command": "PAUSE_ENTRIES", "scope": "account", "account": "acct:orb"}
    )
    assert done["status"] == "REJECTED" and "no such account" in done["result"]["error"]


def test_a_command_the_engine_never_got_to_expires_and_is_not_applied(env, live):
    plane, rt, market = live
    cid = env.post({"command": "PAUSE_ENTRIES", "scope": "global"}).json()["command_id"]
    plane.tick(at(CFG.commands.ttl_s + 5))  # the engine was away for longer than the command lives
    got = env.c.get(f"/v1/commands/{cid}", headers=H["viewer"]).json()
    assert got["status"] == "EXPIRED" and rt.operator_halt is None


def test_the_engine_publishes_its_state_for_the_api(env, live):
    plane, rt, market = live
    plane.tick(at(3))
    state = json.loads((plane.state_file).read_text())
    assert state["mode"] == "shadow" and state["accounts"][ACCT]["positions"] == {CALL: 13}
    assert (
        state["accounts"][ACCT]["exposure_usd"] > 0 and state["feeds"]["stock"]["connected"] is True
    )


def test_a_failing_command_is_recorded_not_fatal(env, live):
    plane, rt, market = live
    rt.operator_pause = lambda now: (_ for _ in ()).throw(RuntimeError("bug"))
    done = run_command(env, plane, at(3), {"command": "PAUSE_ENTRIES", "scope": "global"})
    assert done["status"] == "REJECTED" and "RuntimeError" in done["result"]["error"]
