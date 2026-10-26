"""Order manager: durable intents, reconcile-before-retry, timeouts, exits, exposure checks."""

import pandas as pd
import pytest
import yaml
from order_fakes import FakeBroker

from tournament.orders.config import (
    DEFAULT_PATH,
    OrdersConfigError,
    load_orders_config,
)
from tournament.orders.manager import EntriesBlocked, OrderManager
from tournament.orders.model import Purpose, Status
from tournament.orders.store import IntentStore, memory_engine

T0 = pd.Timestamp("2026-10-26T15:00:00Z")
SYM = "SPY261026C00765000"


def at(s: float) -> pd.Timestamp:
    return T0 + pd.Timedelta(seconds=s)


def config(tmp_path=None, **sections):
    if not sections:
        return load_orders_config()
    raw = yaml.safe_load(DEFAULT_PATH.read_text())
    for name, values in sections.items():
        raw[name].update(values)
    p = tmp_path / "orders.yaml"
    p.write_text(yaml.safe_dump(raw))
    return load_orders_config(p)


def build(cfg=None):
    broker, store = FakeBroker(), IntentStore(memory_engine())
    sleeps = []
    return broker, store, OrderManager("acct:vpc", broker, store, cfg or config(), sleeps.append)


def enter(m, cand="c1", qty=13, bid=1.43, ask=1.44, now=T0):
    return m.place_entry(now, cand, "vpc", SYM, qty, bid, ask)


# ---- config (D08) -------------------------------------------------------------------------


def test_the_initial_values():
    c = config()
    assert (c.entry.fill_timeout_s, c.entry.max_replacements) == (5, 0)
    assert (c.exit.max_replacements, c.exit.final_fallback) == (3, "market")
    assert len(c.config_hash) == 64


def test_a_missing_or_bad_value_stops_startup(tmp_path):
    raw = yaml.safe_load(DEFAULT_PATH.read_text())
    del raw["submission"]["ack_timeout_s"]
    p = tmp_path / "o.yaml"
    p.write_text(yaml.safe_dump(raw))
    with pytest.raises(OrdersConfigError, match="ack_timeout_s"):
        load_orders_config(p)
    with pytest.raises(OrdersConfigError):
        config(tmp_path, entry={"order_type": "stop"})
    with pytest.raises(OrdersConfigError):
        config(tmp_path, pricing={"tick_size": 0})


# ---- intents and ids (OM-02, OM-08) ----------------------------------------------------------


def test_the_intent_is_stored_before_the_broker_is_called():
    broker, store, m = build()
    seen = {}
    real = broker.submit

    def spy(req):
        seen["row"] = store.get(req.client_order_id)
        return real(req)

    broker.submit = spy
    enter(m)
    assert seen["row"] is not None and seen["row"].status is Status.SUBMITTED
    assert [k for k, _ in store.events(seen["row"].client_order_id)][:2] == ["intent", "submit"]


def test_asking_again_for_the_same_candidate_never_sends_a_second_order():
    broker, store, m = build()
    a = enter(m)
    b = enter(m, now=at(1))
    assert a.client_order_id == b.client_order_id and len(broker.submits) == 1


def test_client_order_id_is_deterministic_and_carries_account_and_purpose():
    _, _, m = build()
    cid = m.client_order_id("cand-123", Purpose.ENTRY, 0)
    assert cid == m.client_order_id("cand-123", Purpose.ENTRY, 0)
    assert cid.startswith("opt-acctvpc-") and cid.endswith("-e0") and len(cid) < 128
    assert m.client_order_id("cand-123", Purpose.EXIT, 2).endswith("-x2")


def test_limit_price_comes_from_the_config(tmp_path):
    _, _, m = build()
    assert m.entry_price(1.43, 1.44) == 1.44
    _, _, up = build(config(tmp_path, entry={"price_offset_ticks": 2}))
    assert up.entry_price(1.43, 1.44) == pytest.approx(1.46)
    _, _, mid = build(config(tmp_path, entry={"price_basis": "mid"}))
    assert mid.entry_price(1.43, 1.44) == pytest.approx(1.44)  # 1.435 rounds up to the tick
    assert m.exit_price(1.43, 1.44) == 1.43 and m.exit_price(None, None) == 0.01


# ---- submission outcomes (OM-03, OM-04) ------------------------------------------------------


def test_a_rejection_is_recorded_and_nothing_else_happens():
    broker, store, m = build()
    broker.plan_submit = ["reject"]
    i = enter(m)
    assert i.status is Status.REJECTED and "buying power" in i.last_error
    assert store.active("acct:vpc") == []


def test_a_lost_reply_is_reconciled_by_client_id_and_not_resubmitted():
    broker, store, m = build()
    broker.plan_submit = ["late"]  # the order exists, the reply never came back
    i = enter(m)
    assert i.status is Status.OPEN and i.broker_order_id == "B1"
    assert len(broker.submits) == 1
    assert "reconciled" in [k for k, _ in store.events(i.client_order_id)]


def test_a_request_that_never_arrived_is_resubmitted_once_with_the_same_id():
    broker, store, m = build()
    broker.plan_submit = ["lost"]  # nothing arrived; the second attempt succeeds
    i = enter(m)
    assert i.status is Status.OPEN
    assert [r.client_order_id for r in broker.submits] == [i.client_order_id] * 2
    kinds = [k for k, _ in store.events(i.client_order_id)]
    assert "not_found" in kinds and kinds.count("submit") == 2


def test_an_unreachable_broker_never_triggers_a_resubmission():
    broker, store, m = build()
    broker.plan_submit = ["lost"]
    broker.unreachable = True  # lookups fail: absence can't be established
    i = enter(m)
    assert i.status is Status.UNCERTAIN and len(broker.submits) == 1
    broker.unreachable = False
    (after,) = m.poll(at(2))  # the next poll asks again, finds nothing, then resubmits
    assert after.status is Status.OPEN and len(broker.submits) == 2


def test_resubmission_can_be_switched_off(tmp_path):
    broker, store, m = build(config(tmp_path, submission={"resubmit_after_not_found": False}))
    broker.plan_submit = ["lost"]
    i = enter(m)
    assert i.status is Status.NOT_FOUND and len(broker.submits) == 1


def test_quantities_are_kept_apart_and_fills_only_grow():
    broker, store, m = build()
    i = enter(m)
    assert (i.qty_intended, i.qty_submitted, i.qty_filled) == (13, 13, 0)
    broker.fill(i.client_order_id, 5, 1.44)
    (p,) = m.poll(at(1))
    assert p.status is Status.PARTIALLY_FILLED and p.qty_filled == 5 and p.avg_fill_price == 1.44
    broker.orders[i.client_order_id]["filled"] = 3  # a stale lookup can't lower a fill
    (q,) = m.poll(at(2))
    assert q.qty_filled == 5


# ---- timeouts, cancels and re-pricing (D08) --------------------------------------------------


def test_an_unfilled_entry_is_cancelled_after_the_timeout_and_not_replaced_by_default():
    broker, store, m = build()
    i = enter(m)
    assert m.poll(at(4))[0].status is Status.OPEN  # 4 s: still waiting
    m.poll(at(5))  # 5 s: cancel
    assert broker.cancels == [i.client_order_id]
    assert store.get(i.client_order_id).status is Status.CANCELED
    assert len(broker.submits) == 1 and store.active("acct:vpc") == []


def test_entry_replacements_step_up_and_stop_at_the_slippage_cap(tmp_path):
    cfg = config(tmp_path, entry={"max_replacements": 5, "max_slippage_ticks": 2})
    broker, store, m = build(cfg)
    enter(m)
    for t in (5, 10, 15, 20):
        m.poll(at(t))
    prices = [r.limit_price for r in broker.submits]
    assert prices == [1.44, 1.45, 1.46]  # 2 ticks over the first limit is the cap
    kinds = [
        k
        for i in store.chain("acct:vpc", "c1", Purpose.ENTRY)
        for k, _ in store.events(i.client_order_id)
    ]
    assert "replacement_capped" in kinds
    assert [r.client_order_id.rsplit("-", 1)[1] for r in broker.submits] == ["e0", "e1", "e2"]


def test_a_fill_that_beats_the_cancel_is_kept_and_nothing_is_replaced(tmp_path):
    cfg = config(tmp_path, entry={"max_replacements": 2})
    broker, store, m = build(cfg)
    i = enter(m)
    broker.fill_on_cancel = 13  # filled while the cancel was in flight
    m.poll(at(5))
    assert store.get(i.client_order_id).status is Status.FILLED
    assert m.position(SYM) == 13 and len(broker.submits) == 1


def test_a_partly_filled_entry_keeps_its_fill_and_is_not_re_priced(tmp_path):
    cfg = config(tmp_path, entry={"max_replacements": 2})
    broker, store, m = build(cfg)
    i = enter(m)
    broker.fill(i.client_order_id, 4, 1.44)
    m.poll(at(5))
    done = store.get(i.client_order_id)
    assert done.status is Status.CANCELED and done.qty_filled == 4
    assert m.position(SYM) == 4 and len(broker.submits) == 1


def test_the_timeout_is_a_config_value(tmp_path):
    broker, store, m = build(config(tmp_path, entry={"fill_timeout_s": 20}))
    enter(m)
    m.poll(at(19))
    assert broker.cancels == []
    m.poll(at(20))
    assert len(broker.cancels) == 1


# ---- exits (EX-03 to EX-05) --------------------------------------------------------------------


def held(m, broker, qty=13):
    i = enter(m, qty=qty)
    broker.fill(i.client_order_id, qty, 1.44)
    m.poll(at(1))


def test_an_exit_sells_what_is_held_at_the_bid():
    broker, store, m = build()
    held(m, broker)
    x = m.place_exit(at(60), "c1", "vpc", SYM, "TARGET", bid=1.80, ask=1.81)
    assert (x.side, x.qty_intended, x.limit_price, x.reasons) == ("sell", 13, 1.80, ("TARGET",))
    assert x.client_order_id.endswith("-x0")


def test_a_second_trigger_adds_its_reason_and_never_sells_twice():
    broker, store, m = build()
    held(m, broker)
    a = m.place_exit(at(60), "c1", "vpc", SYM, "TARGET", bid=1.80)
    b = m.place_exit(at(60), "c1", "vpc", SYM, "SESSION_CUTOFF", bid=1.80)
    assert a.client_order_id == b.client_order_id
    assert store.get(a.client_order_id).reasons == ("TARGET", "SESSION_CUTOFF")
    assert len([r for r in broker.submits if r.side == "sell"]) == 1


def test_nothing_to_sell_returns_none():
    _, _, m = build()
    assert m.place_exit(at(60), "c1", "vpc", SYM, "TARGET", bid=1.80) is None


def test_unfilled_exits_step_down_then_fall_back_to_market():
    broker, store, m = build()
    held(m, broker)
    m.place_exit(at(60), "c1", "vpc", SYM, "TARGET", bid=1.80)
    for t in (63, 66, 69, 72):
        m.poll(at(t))
    sells = [r for r in broker.submits if r.side == "sell"]
    assert [(r.order_type, r.limit_price) for r in sells] == [
        ("limit", 1.80),
        ("limit", 1.79),
        ("limit", 1.78),
        ("limit", 1.77),
        ("market", None),
    ]
    assert sells[-1].qty == 13


def test_a_partly_filled_exit_replaces_only_the_remainder():
    broker, store, m = build()
    held(m, broker)
    x = m.place_exit(at(60), "c1", "vpc", SYM, "TARGET", bid=1.80)
    broker.fill(x.client_order_id, 8, 1.80)
    m.poll(at(63))
    sells = [r for r in broker.submits if r.side == "sell"]
    assert [r.qty for r in sells] == [13, 5] and m.position(SYM) == 5


def test_exit_without_a_market_fallback_stops_and_says_so(tmp_path):
    cfg = config(tmp_path, exit={"max_replacements": 0, "final_fallback": "none"})
    broker, store, m = build(cfg)
    held(m, broker)
    x = m.place_exit(at(60), "c1", "vpc", SYM, "TARGET", bid=1.80)
    m.poll(at(63))
    assert len([r for r in broker.submits if r.side == "sell"]) == 1
    assert "exit_unfilled" in [k for k, _ in store.events(x.client_order_id)]
    assert m.position(SYM) == 13  # still exposed: the position manager must act on it


# ---- restart and exposure (AC-09, OM-05, OM-06) ----------------------------------------------


def test_recover_sends_an_intent_that_was_saved_but_never_sent():
    broker, store, m = build()
    i = enter(m, cand="c9")
    # simulate a crash between the save and the send: the row exists, the broker saw nothing
    broker.orders.clear()
    broker.submits.clear()
    store.save(i.with_(status=Status.PENDING_SUBMIT))
    (r,) = m.recover(at(30))
    assert r.status is Status.OPEN and len(broker.submits) == 1


def test_recover_adopts_orders_the_broker_already_has():
    broker, store, m = build()
    i = enter(m)
    broker.fill(i.client_order_id, 13, 1.44)
    store.save(i.with_(status=Status.UNCERTAIN))  # we crashed before hearing the fill
    (r,) = m.recover(at(30))
    assert r.status is Status.FILLED and r.qty_filled == 13 and len(broker.submits) == 1


def test_unexpected_broker_exposure_blocks_new_entries_until_cleared():
    broker, store, m = build()
    held(m, broker)
    assert m.reconcile_positions(at(2)) == [] and m.entries_blocked is None
    broker.extra_positions = {"SPY261026P00760000": 2}  # something we did not do
    diffs = m.reconcile_positions(at(3))
    assert diffs == [("SPY261026P00760000", 0, 2)]
    with pytest.raises(EntriesBlocked):
        enter(m, cand="c2")
    m.clear_entries_block(at(4), "owner")
    assert enter(m, cand="c2").status is Status.OPEN


def test_the_tolerance_is_a_config_value(tmp_path):
    broker, store, m = build(config(tmp_path, reconciliation={"quantity_tolerance": 2}))
    broker.extra_positions = {"SPY261026P00760000": 2}
    assert m.reconcile_positions(at(1)) == [] and m.entries_blocked is None
