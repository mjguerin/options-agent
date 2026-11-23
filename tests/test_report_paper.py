"""The one page shows real Alpaca paper fills where a paper account traded."""

import sqlite3

from tournament.report import data


def _paper_db(path):
    con = sqlite3.connect(path)
    con.execute("create table orders (candidate_id, purpose, qty_filled, avg_fill_price)")
    con.executemany(
        "insert into orders values (?,?,?,?)",
        [("c1", "entry", 5, 3.50), ("c1", "exit", 5, 2.79), ("c2", "entry", 0, None)],
    )
    con.commit()
    con.close()


def test_paper_fills_replace_the_modeled_result_and_keep_it_beside(tmp_path):
    day = tmp_path / "2026-11-23"
    day.mkdir()
    _paper_db(day / "paper-accounts.sqlite")
    t = {"candidate_id": "c1", "entry": 3.39, "exit": 3.94, "qty": 5, "net_usd": 275.0,
         "open_qty": 0}  # fmt: skip
    accounts = {"acct:brt": {"trades": [t], "day_net_usd": 275.0}}
    data._overlay_paper("2026-11-23", tmp_path, accounts)
    x = accounts["acct:brt"]
    assert t["source"] == "paper" and (t["entry"], t["exit"]) == (3.50, 2.79)
    assert -356 < t["net_usd"] < -354 and t["modeled"]["net_usd"] == 275.0
    assert x["day_net_usd"] == t["net_usd"] and x["modeled_day_net_usd"] == 275.0


def test_a_day_without_a_paper_account_stays_modeled(tmp_path):
    accounts = {
        "acct:orb": {"trades": [{"candidate_id": "c1", "net_usd": 9.0}], "day_net_usd": 9.0}
    }
    data._overlay_paper("2026-10-01", tmp_path, accounts)
    assert accounts["acct:orb"]["day_net_usd"] == 9.0 and "source" not in accounts["acct:orb"]
