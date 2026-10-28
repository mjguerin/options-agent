"""The dashboard (BRD 14). Run with `tournament dashboard run`.

Five views: overview, strategies, execution, operations, and controls. Everything here is read
from the engine's published state, the API and the session folders; controls go through the
API, which asks the engine. A page that can't reach a source says so instead of failing.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from tournament.appconfig import app_config
from tournament.dashboard import data
from tournament.dashboard.backend import get_backend
from tournament.risk.config import load_risk_config

COLORS = {"green": "🟢", "amber": "🟠", "red": "🔴"}
STATUS_ICON = {"OK": "🟢", "PAUSED": "🟠", "HALTED": "🟠", "FLATTENED": "🟠", "BLOCKED": "🔴"}


def local(ts) -> str:
    if ts is None:
        return ""
    return pd.Timestamp(ts).tz_convert(app_config().exchange.tz).strftime("%H:%M:%S")


def api_call(fn, *args, default=None):
    """Call the API; on failure show why and carry on with `default`."""
    try:
        return fn(*args)
    except data.ApiError as exc:
        st.warning(f"API: {exc.detail}")
        return default


# ---- pages -------------------------------------------------------------------------------------


def header(status: dict) -> None:
    colour, words = data.engine_light(status)
    state = status.get("state") or {}
    c = st.columns(5)
    c[0].metric("Engine", f"{COLORS[colour]} {words}")
    c[1].metric("Session", state.get("session", "-"))
    c[2].metric("Mode", state.get("mode", "-"))
    c[3].metric("Slot", state.get("slot", "-"))
    c[4].metric("Open incidents", len(status.get("open_incidents", [])))


def page_overview(b, status: dict) -> None:
    state = status.get("state") or {}
    feeds = state.get("feeds", {})
    f = st.columns(3)
    f[0].metric("SPY feed", "connected" if feeds.get("stock", {}).get("connected") else "DOWN")
    opt = feeds.get("option")
    if opt:
        lag = (opt.get("lag_s") or {}).get("p99")
        f[1].metric("Option feed", "connected" if opt.get("connected") else "DOWN")
        amber = lag is not None and lag > b.cfg.health.lag_warn_s
        f[2].metric(
            "Quote delivery lag p99",
            "-" if lag is None else f"{lag * 1000:.0f} ms",
            "slow" if amber else None,
        )
    st.subheader("Accounts")
    table = data.accounts_table(state)
    if table.empty:
        st.info(
            "No accounts reported yet: the engine starts at 03:35 ET on session days and is "
            "ready by about 03:45."
        )
    else:
        table.insert(1, "", table["status"].map(STATUS_ICON))
        st.dataframe(table, hide_index=True, use_container_width=True)
    st.subheader("Profit and loss through the session")
    sessions = data.session_dirs(b.live_root, state.get("session"))
    hist_dir = b.root / "data" / "run" / "state_history"
    hist = data.load_history(hist_dir / f"{state.get('session', '')}.jsonl")
    if hist.empty:
        st.caption("The curves start a minute or so after the engine does.")
    else:
        st.line_chart(hist, height=240)
        st.caption(
            "Realized plus open profit and loss per account. "
            "The no-trade reference is the zero line."
        )
        st.line_chart(data.drawdown(hist, 10_000.0), height=160)
        st.caption("Drop from each account's peak, in dollars (from a $10,000 start).")
    del sessions
    inc = status.get("open_incidents", [])
    if inc:
        st.subheader("Open incidents")
        st.dataframe(pd.DataFrame(inc), hide_index=True, use_container_width=True)


def page_strategies(b, status: dict) -> None:
    state = status.get("state") or {}
    strategies = state.get("strategies", {})
    if not strategies:
        st.info("No strategy state yet.")
        return
    sid = st.selectbox("Strategy", sorted(strategies))
    s = strategies[sid]
    c = st.columns(4)
    c[0].metric("Setup state", s["setup_state"] or "-")
    c[1].metric("Last outcome", s["outcome"] or "-")
    c[2].metric("Readiness", s["readiness"] or "-")
    c[3].metric("Version", f"{s['version']} / {s['config_hash']}")
    if s["blocks"] or s["reasons"]:
        st.write("**Why it can't act now:**", ", ".join(s["blocks"] + s["reasons"]))
    acct = (state.get("accounts") or {}).get(f"acct:{sid}", {})
    st.subheader("Positions")
    pos = acct.get("positions_detail", [])
    if pos:
        st.dataframe(pd.DataFrame(pos), hide_index=True, use_container_width=True)
    else:
        st.caption("Flat.")
    risk = load_risk_config()
    st.caption(
        f"Active exits: premium stop at {risk.premium_stop.loss_pct:.0%} below entry, "
        "session cutoff, "
        f"and the strategy's reference exits. Daily loss limit ${risk.daily_loss.limit_usd:,.0f}."
    )
    st.subheader("Recent candidate decisions")
    dec = pd.DataFrame(acct.get("recent_decisions", []))
    if dec.empty:
        st.caption("No candidates yet today.")
    else:
        dec["at"] = dec["at"].map(local)
        dec["reasons"] = dec["reasons"].map(lambda r: ", ".join(r) or "accepted")
        st.dataframe(dec.iloc[::-1], hide_index=True, use_container_width=True)


def page_execution(b, status: dict) -> None:
    state = status.get("state") or {}
    dirs = data.session_dirs(b.live_root, state.get("session"))
    if not dirs:
        st.info("No session folder yet.")
        return
    run = st.selectbox("Run", [d.name for d in dirs])
    folder = next(d for d in dirs if d.name == run)
    orders, events = data.read_orders(folder / "accounts.sqlite")
    if orders.empty:
        st.caption("No orders in this run.")
        return
    cols = [
        "created_at", "account_binding", "purpose", "side", "symbol", "qty_intended",
        "qty_submitted", "qty_filled", "order_type", "limit_price", "avg_fill_price",
        "status", "reasons",
    ]  # fmt: skip
    st.dataframe(
        orders[cols].iloc[::-1].head(b.cfg.tables.max_rows),
        hide_index=True,
        use_container_width=True,
    )
    lat = data.fill_latencies(orders, events)
    c = st.columns(4)
    c[0].metric("Orders", len(orders))
    c[1].metric("Filled", int((orders["status"] == "FILLED").sum()))
    c[2].metric(
        "Cancelled or rejected",
        int(orders["status"].isin(["CANCELED", "REJECTED", "EXPIRED"]).sum()),
    )
    c[3].metric("Median fill time", "-" if lat.empty else f"{lat['latency_s'].median():.1f} s")
    pick = st.selectbox("Order history", [""] + orders["client_order_id"].tolist())
    if pick:
        st.dataframe(
            events[events["client_order_id"] == pick][["at", "kind", "detail"]],
            hide_index=True,
            use_container_width=True,
        )


def page_operations(b, status: dict) -> None:
    state = status.get("state") or {}
    st.subheader("Feeds and engine")
    st.json({"published": state.get("at"), "feeds": state.get("feeds", {})})
    pending = status.get("pending_commands", [])
    st.write(f"**Commands waiting for the engine:** {len(pending)}")
    if pending:
        st.dataframe(pd.DataFrame(pending), hide_index=True, use_container_width=True)
    st.subheader("Incidents")
    inc = api_call(b.api.incidents, 100, default=[]) if b.api else []
    if inc:
        st.dataframe(pd.DataFrame(inc), hide_index=True, use_container_width=True)
    else:
        st.caption("None recorded.")
    st.subheader("Audit log")
    aud = api_call(b.api.audit, b.cfg.tables.max_rows, default=[]) if b.api else []
    if aud:
        st.dataframe(pd.DataFrame(aud), hide_index=True, use_container_width=True)
    else:
        st.caption("Nothing yet.")
    st.subheader("Nightly job reports")
    reps = data.ops_reports(b.ops_root)
    if reps:
        st.dataframe(pd.DataFrame(reps), hide_index=True, use_container_width=True)
    else:
        st.caption("No nightly report yet.")


def page_research(b, status: dict) -> None:
    ledgers = data.load_ledgers(b.live_root)
    if not ledgers:
        st.info("The ledgers are written after each session's close. None yet.")
        return
    table = data.cumulative(ledgers)
    st.subheader("All sessions so far")
    st.dataframe(table, hide_index=True, use_container_width=True)
    label = ledgers[-1]["summary"].get("result_label", "gross of unmodeled costs")
    st.caption(
        f"Results are {label}. Net P&L is the evaluation ledger (every account priced against "
        "its own decision quotes the same way) plus open positions at a fresh bid. Outages and "
        "incidents are shown, never excluded. Shadow results are development observations."
    )
    last = max(b_["summary"]["session_date"] for b_ in ledgers)
    ranking = data.read_json(
        next(iter(sorted(b.live_root.glob(f"{last}/*/ledger/ranking.json"))), b.live_root)
    )
    if ranking:
        st.subheader(f"Ranking, {last}")
        rows = [
            {
                "rank": r["rank"],
                "account": r["account"],
                "net P&L est $": r["net_pnl_est_usd"],
                "promotion qualified": r["promotion_qualified"],
                "not evaluable": ", ".join(r.get("not_evaluable", [])),
                "note": "leads, but does not qualify" if r["leader_not_qualified"] else "",
            }
            for r in ranking
        ]
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
        st.caption(
            "Every gate is mandatory. A gate with no threshold, or one whose inputs are not yet "
            "trustworthy, is not evaluable and blocks qualification."
        )
    acct = st.selectbox("Account", sorted({b_["summary"]["account"] for b_ in ledgers}))
    mine = [b_ for b_ in ledgers if b_["summary"]["account"] == acct][-1]
    st.subheader(f"{acct}, {mine['summary']['session_date']}")
    c = st.columns(4)
    m = mine["metrics"]
    c[0].metric("Closed trades", m["closed_trades"])
    c[1].metric("Win rate", "-" if m["win_rate"] is None else f"{m['win_rate']:.0%}")
    c[2].metric(
        "Max drawdown", "-" if m["max_drawdown_usd"] is None else f"${m['max_drawdown_usd']:,.0f}"
    )
    c[3].metric("Books reconcile", "yes" if mine["reconciliation"]["ok"] else "NO")
    trades = pd.DataFrame(mine["trades"])
    if trades.empty:
        st.caption("No trades.")
    else:
        cols = ["symbol", "qty", "closed_qty", "broker_entry", "broker_exit", "eval_entry",
                "eval_exit", "broker_gross_usd", "eval_gross_usd", "fees_usd", "net_eval_usd",
                "exit_reasons", "flags"]  # fmt: skip
        st.dataframe(trades[cols], hide_index=True, use_container_width=True)
        name = mine.get("execution_ledger_name", "execution ledger")
        st.caption(
            f"The 'broker_*' columns come from the {name} (in shadow mode: modeled fills, not a "
            "broker). Evaluation columns price the same executions against the quote held at "
            "each decision, the same way for every account."
        )
    paths = data.read_json(
        next(
            iter(
                sorted(b.live_root.glob(f"{mine['summary']['session_date']}/*/ledger/paths.json"))
            ),
            b.live_root,
        )
    )
    rows = (paths or {}).get(acct) or []
    if rows:
        st.write("**Where each trade went**")
        view = []
        for r in rows:
            o, ref = r.get("option") or {}, r.get("reference") or {}
            hold = o.get("hold") or {}
            view.append(
                {
                    "symbol": r["symbol"],
                    "entry": r["entry"],
                    "exit": r["exit"],
                    "best bid in hold": hold.get("peak_bid"),
                    "worst bid in hold": hold.get("low_bid"),
                    "best bid after exit": (o.get("after_exit") or {}).get("peak_bid"),
                    "last bid of day": (o.get("session_end") or {}).get("last_bid"),
                    "exit vs holding to the end $": o.get("exit_vs_held_to_end_usd"),
                    "signal result R": ref.get("result_r"),
                    "signal best R": ref.get("best_excursion_r"),
                    "signal exit": ref.get("exit_reason"),
                }
            )
        st.dataframe(pd.DataFrame(view), hide_index=True, use_container_width=True)
        st.caption(
            "A signal that never moved in favour (best R near zero) failed on its own terms. "
            "A signal that ran for the trade and was then given back points at the exit. One "
            "trade is an anecdote."
        )
    st.write(
        "**Reconciliation**", {k: v["ok"] for k, v in mine["reconciliation"]["checks"].items()}
    )
    st.write("**Operating conditions**", mine["summary"]["operational"])


def page_controls(b, status: dict) -> None:
    from tournament.api.config import load_api_config

    if b.api is None:
        st.error("Controls need the API tokens in .env.")
        return
    controls = load_api_config().controls
    state = status.get("state") or {}
    st.warning("Controls act on the engine's accounts. Pausing and cancelling reduce exposure; "
               "the others need a typed confirmation.")  # fmt: skip
    cmd = st.selectbox(
        "Control", list(controls), format_func=lambda c: f"{c}: {controls[c].summary}"
    )
    spec = controls[cmd]
    scope = st.radio("Scope", spec.scopes, horizontal=True)
    body: dict = {"command": cmd, "scope": scope}
    target = "ALL"
    if scope == "account":
        accounts = sorted(state.get("accounts") or {})
        body["account"] = target = (
            st.selectbox("Account", accounts) if accounts else st.text_input("Account", "acct:")
        )
    if spec.needs_symbol:
        held = ((state.get("accounts") or {}).get(body.get("account", ""), {}) or {}).get(
            "positions", {}
        )
        body["symbol"] = target = (
            st.selectbox("Contract", sorted(held)) if held else st.text_input("Contract")
        )
    result = None
    if not spec.confirm:
        if st.button(f"Send {cmd}"):
            result = api_call(b.api.control, body)
    else:
        prep = st.session_state.get("prepared")
        if prep is not None and prep.body != body:
            prep = st.session_state["prepared"] = None  # the selection changed: start over
        if prep is None:
            if st.button(f"Prepare {cmd}"):
                r = api_call(b.api.control, body)
                if r and r.get("needs_confirmation"):
                    phrase = b.cfg.controls.confirm_phrase.format(command=cmd, target=target)
                    st.session_state["prepared"] = data.Prepared(
                        body,
                        r["confirm_id"],
                        r["expires_at"],
                        r["will_do"],
                        r.get("positions", {}),
                        phrase,
                    )
                    st.rerun()
        else:
            st.error(f"**{prep.will_do}**  Valid until {local(prep.expires_at)} ET.")
            st.write("Positions at stake:", prep.positions or "none reported")
            typed = st.text_input(f"Type `{prep.phrase}` to confirm")
            if st.button("Confirm and send", disabled=typed != prep.phrase):
                result = api_call(b.api.control, {**body, "confirm_id": prep.confirm_id})
                st.session_state["prepared"] = None
            if st.button("Cancel"):
                st.session_state["prepared"] = None
                st.rerun()
    if result and result.get("command_id"):
        st.session_state["last_command"] = result["command_id"]
        st.success(f"Queued as {result['command_id'][:8]}. {result.get('note') or ''}")
    last = st.session_state.get("last_command")
    if last:
        c = api_call(b.api.command, last)
        if c:
            st.write(f"Last command **{c['command']}**: {c['status']}")
            if c.get("result"):
                st.json(c["result"])


# ---- main ----------------------------------------------------------------------------------------

st.set_page_config(page_title="Options tournament", layout="wide")
backend = get_backend()
st.title("Options strategy tournament")
if backend.setup_error:
    st.error(f"Dashboard setup: {backend.setup_error}")
    st.code(
        "printf 'API_OWNER_TOKEN=%s\\nAPI_VIEWER_TOKEN=%s\\n' "
        "$(openssl rand -hex 32) $(openssl rand -hex 32) >> ~/options-agent/.env"
    )
    st.stop()
current = api_call(backend.api.status, default={"engine_reporting": False, "state": None})
header(current)
page = st.sidebar.radio(
    "View", ["Overview", "Strategies", "Execution", "Research", "Operations", "Controls"]
)
{
    "Overview": page_overview,
    "Strategies": page_strategies,
    "Execution": page_execution,
    "Research": page_research,
    "Operations": page_operations,
    "Controls": page_controls,
}[page](backend, current)
st.sidebar.caption(f"{backend.cfg.version} · refresh {backend.cfg.refresh_s:g}s")
if st.sidebar.button("Refresh now"):
    st.rerun()
