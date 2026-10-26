"""Risk config, contract selection, sizing, daily loss and premium stop: all config-driven."""

from datetime import date

import pandas as pd
import pytest
import yaml

from tournament.live.quotes import OptionFeed, quote_from
from tournament.risk.config import DEFAULT_PATH, ENV_VAR, RiskConfigError, load_risk_config
from tournament.risk.daily import DailyLossGuard, OpenPosition, premium_stop_hit
from tournament.risk.selection import (
    ASK_SIZE_TOO_SMALL,
    NO_EXPIRY,
    NO_STRIKE,
    PREMIUM_TOO_LOW,
    SPREAD_TOO_WIDE_ABS,
    SPREAD_TOO_WIDE_PCT,
    select_contract,
)
from tournament.risk.sizing import (
    BELOW_MIN_QUANTITY,
    PREMIUM_EXCEEDS_BUDGET,
    QUOTE_SIZE_SHORTFALL,
    size_order,
)

CFG = load_risk_config()
TODAY = date(2026, 10, 26)
T0 = pd.Timestamp("2026-10-26T15:00:00Z")


def edited(tmp_path, **sections):
    raw = yaml.safe_load(DEFAULT_PATH.read_text())
    for name, values in sections.items():
        raw[name].update(values)
    p = tmp_path / "risk.yaml"
    p.write_text(yaml.safe_dump(raw))
    return p


# ---- config ------------------------------------------------------------------------------


def test_the_initial_owner_values():
    assert CFG.sizing.premium_budget_pct == 0.20 and CFG.premium_stop.loss_pct == 0.20
    assert CFG.daily_loss.limit_usd == 500 and CFG.daily_loss.on_breach == "halt_entries"
    assert CFG.contract_selection.expiry_days == 0 and CFG.contract_selection.strike_offset == 0
    assert len(CFG.config_hash) == 64


def test_changing_a_value_changes_the_hash_and_needs_no_code(tmp_path):
    other = load_risk_config(edited(tmp_path, sizing={"premium_budget_pct": 0.10}))
    assert other.sizing.premium_budget_pct == 0.10 and other.config_hash != CFG.config_hash
    assert load_risk_config().config_hash == CFG.config_hash  # deterministic


def test_the_env_var_selects_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_VAR, str(edited(tmp_path, daily_loss={"limit_usd": 250})))
    assert load_risk_config().daily_loss.limit_usd == 250


@pytest.mark.parametrize(
    "drop",
    [("sizing", "premium_budget_pct"), ("daily_loss", "limit_usd"), ("premium_stop", "loss_pct")],
)
def test_a_missing_value_blocks_activation(tmp_path, drop):  # RK-07
    raw = yaml.safe_load(DEFAULT_PATH.read_text())
    del raw[drop[0]][drop[1]]
    p = tmp_path / "r.yaml"
    p.write_text(yaml.safe_dump(raw))
    with pytest.raises(RiskConfigError, match=drop[1]):
        load_risk_config(p)


@pytest.mark.parametrize(
    "section, values",
    [
        ("sizing", {"premium_budget_pct": 1.5}),
        ("sizing", {"premium_budget_pct": 0}),
        ("premium_stop", {"loss_pct": 1.0}),
        ("daily_loss", {"limit_usd": -5}),
        ("daily_loss", {"on_breach": "ignore"}),
        ("contract_selection", {"extra_knob": 1}),
    ],
)
def test_out_of_range_or_unknown_values_are_rejected(tmp_path, section, values):
    with pytest.raises(RiskConfigError):
        load_risk_config(edited(tmp_path, **{section: values}))


# ---- contract selection ------------------------------------------------------------------


def occ(k, right="C", exp="261026"):
    return f"SPY{exp}{right}{int(k * 1000):08d}"


def chain():
    rows = []
    for exp, d in (("261026", date(2026, 10, 26)), ("261027", date(2026, 10, 27))):
        for k in range(760, 771):
            for r in "CP":
                rows.append(
                    {"symbol": occ(k, r, exp), "expiration": d, "strike": float(k), "right": r}
                )
    return pd.DataFrame(rows)


def feed(bid=1.43, ask=1.44, bid_size=20, ask_size=30, now=T0):
    f = OptionFeed(pd.Timedelta(seconds=5), pd.Timedelta(seconds=2), pd.Timedelta(seconds=10), 5000)
    f.on_connected(now - pd.Timedelta(minutes=5))

    def put(symbol):
        m = {"T": "q", "S": symbol, "t": now.isoformat(), "bp": bid, "bs": bid_size,
             "ap": ask, "as": ask_size, "bx": "C", "ax": "C", "c": "A"}  # fmt: skip
        f.on_messages([m], now + pd.Timedelta(milliseconds=5))

    for s in chain()["symbol"]:
        put(s)
    return f


def pick(direction="bullish", spot=765.3, cfg=CFG.contract_selection, f=None):
    f = f or feed()
    return select_contract(
        direction, spot, TODAY, chain(), lambda s: f.view(s, T0 + pd.Timedelta(seconds=1)), cfg
    )


def test_nearest_atm_call_for_bullish_and_put_for_bearish():
    c, p = pick("bullish"), pick("bearish")
    assert c.selected and c.symbol == occ(765, "C") and c.right == "C"
    assert p.selected and p.symbol == occ(765, "P") and p.right == "P"


def test_a_tie_between_strikes_goes_to_the_lower_strike():
    assert pick(spot=765.5).symbol == occ(765, "C")


def test_strike_offset_moves_toward_the_money_side_for_each_right(tmp_path):
    cfg = load_risk_config(edited(tmp_path, contract_selection={"strike_offset": 1}))
    cs = cfg.contract_selection
    assert pick("bullish", cfg=cs).symbol == occ(764, "C")  # call in the money = lower strike
    assert pick("bearish", cfg=cs).symbol == occ(766, "P")  # put in the money = higher strike
    otm = load_risk_config(edited(tmp_path, contract_selection={"strike_offset": -2}))
    assert pick("bullish", cfg=otm.contract_selection).symbol == occ(767, "C")


def test_expiry_days_is_configurable_and_a_missing_expiry_rejects(tmp_path):
    nxt = load_risk_config(edited(tmp_path, contract_selection={"expiry_days": 1}))
    assert pick(cfg=nxt.contract_selection).symbol == occ(765, "C", "261027")
    far = load_risk_config(edited(tmp_path, contract_selection={"expiry_days": 5}))
    assert pick(cfg=far.contract_selection).reasons == (NO_EXPIRY,)
    edge = load_risk_config(edited(tmp_path, contract_selection={"strike_offset": 20}))
    assert pick(cfg=edge.contract_selection).reasons == (NO_STRIKE,)


@pytest.mark.parametrize(
    "kw, reason",
    [
        ({"bid": 1.30, "ask": 1.44}, SPREAD_TOO_WIDE_ABS),
        ({"bid": 0.20, "ask": 0.24}, SPREAD_TOO_WIDE_PCT),
        ({"bid": 0.02, "ask": 0.03}, PREMIUM_TOO_LOW),
        ({"ask_size": 0}, ASK_SIZE_TOO_SMALL),
    ],
)
def test_the_selected_contract_is_rejected_not_swapped_for_another(kw, reason):
    s = pick(f=feed(**kw))
    assert not s.selected and reason in s.reasons and s.symbol == occ(765, "C")


def test_quote_gate_reasons_pass_through():
    stale = feed(now=T0 - pd.Timedelta(seconds=30))
    assert "STALE_QUOTE" in pick(f=stale).reasons


# ---- sizing ------------------------------------------------------------------------------


def test_quantity_is_the_most_contracts_the_premium_budget_allows():
    r = size_order(1.44, 30, 10_000, 10_000, CFG)
    assert (r.quantity, r.budget, r.premium_per_contract) == (13, 2000.0, 144.0)
    assert r.premium_at_risk == pytest.approx(1872.0)
    assert r.planned_stop_risk == pytest.approx(374.4)  # 20% of the premium, not a guaranteed cap
    assert size_order(6.41, 30, 10_000, 10_000, CFG).quantity == 3


def test_exact_division_is_not_lost_to_float_error():
    assert size_order(0.20, 500, 10_000, 10_000, CFG).quantity == 100


def test_a_contract_dearer_than_the_budget_is_rejected_never_sized_up():
    r = size_order(25.00, 30, 10_000, 10_000, CFG)
    assert r.quantity == 0 and r.reasons == (PREMIUM_EXCEEDS_BUDGET,)


def test_cash_and_quoted_size_cap_the_quantity_or_reject(tmp_path):
    assert size_order(1.44, 30, 10_000, 600, CFG).capped_by == "cash"
    assert size_order(1.44, 30, 10_000, 600, CFG).quantity == 4
    r = size_order(1.44, 5, 10_000, 10_000, CFG)
    assert (r.quantity, r.capped_by) == (5, "ask_size")
    strict = load_risk_config(edited(tmp_path, sizing={"on_quote_size_shortfall": "reject"}))
    assert size_order(1.44, 5, 10_000, 10_000, strict).reasons == (QUOTE_SIZE_SHORTFALL,)
    assert size_order(1.44, 30, 10_000, 100, CFG).reasons == (BELOW_MIN_QUANTITY,)


def test_the_budget_follows_the_config_and_the_equity_basis(tmp_path):
    small = load_risk_config(edited(tmp_path, sizing={"premium_budget_pct": 0.10}))
    assert size_order(1.44, 30, 10_000, 10_000, small).quantity == 6
    assert size_order(1.44, 30, 8_000, 10_000, CFG).quantity == 11  # budget from today's equity


# ---- daily loss and premium stop ---------------------------------------------------------


def pos(entry=1.44, bid=1.0, qty=10):
    return OpenPosition(occ(765), qty, entry, bid)


def guard(cfg=CFG):
    return DailyLossGuard(cfg.daily_loss, cfg.sizing.contract_multiplier)


def test_open_losses_at_the_bid_count_and_the_halt_is_sticky():
    g = guard()
    ok = g.update(T0, -100, [pos(bid=1.30)])  # -100 realized, -140 open
    assert ok.loss == pytest.approx(240) and ok.entries_allowed and not ok.breached
    hit = g.update(T0, -100, [pos(bid=1.0)])  # open -440: total 540 >= 500
    assert hit.breached and not hit.entries_allowed and not hit.flatten
    back = g.update(T0, 0, [pos(bid=1.44)])  # marks recover; the halt stays
    assert back.breached and not back.entries_allowed and g.breached_at == T0
    g.reset_session()
    assert g.update(T0, 0, []).entries_allowed


def test_realized_only_ignores_open_positions(tmp_path):
    cfg = load_risk_config(edited(tmp_path, daily_loss={"measure": "realized_only"}))
    assert guard(cfg).update(T0, -100, [pos(bid=0.1)]).loss == pytest.approx(100)


def test_an_unmarked_position_follows_the_configured_assumption(tmp_path):
    p = OpenPosition(occ(765), 10, 1.44, None)
    s = guard().update(T0, 0, [p])
    assert s.unmarked == 1 and s.loss == pytest.approx(1440) and s.breached
    soft = load_risk_config(edited(tmp_path, daily_loss={"unmarked_position": "entry_cost"}))
    assert guard(soft).update(T0, 0, [p]).loss == 0


def test_flatten_is_only_requested_when_configured(tmp_path):
    cfg = load_risk_config(edited(tmp_path, daily_loss={"on_breach": "halt_and_flatten"}))
    s = guard(cfg).update(T0, -600, [])
    assert s.breached and s.flatten


def test_premium_stop_fires_at_the_configured_loss():
    assert premium_stop_hit(1.00, 0.80, CFG.premium_stop.loss_pct)  # exactly -20%
    assert not premium_stop_hit(1.00, 0.81, CFG.premium_stop.loss_pct)
    assert not premium_stop_hit(1.00, None, CFG.premium_stop.loss_pct)
    assert premium_stop_hit(1.44, 1.15, 0.20) and not premium_stop_hit(1.44, 1.16, 0.20)


def test_the_quote_message_helper_used_by_the_fixtures_matches_the_feed():
    m = {"T": "q", "S": occ(765), "t": T0.isoformat(), "bp": 1.0, "ap": 1.1, "bs": 1, "as": 1}
    assert quote_from(m, T0).mid == pytest.approx(1.05)
