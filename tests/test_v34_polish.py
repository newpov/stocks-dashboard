"""v3.4 fixes/polish. JS-behaviour items are guarded at the page-source level
(the repo's convention for client-side code) + browser-verified separately."""
import inspect

import pandas as pd

import build


def _page_src():
    return inspect.getsource(build.render_html)


# --- #6: Losers / Bottom 10 sorted most-negative first -----------------------

def test_holdings_sort_uses_shared_helper():
    # Both the header-click sort and the per-mode default must go through one
    # deterministic helper so direction can be set explicitly.
    assert "function sortTable(" in _page_src()


def test_losers_and_bottom10_sort_ascending():
    # Ascending on Since-baseline (col 9) => biggest loss on top.
    src = _page_src()
    assert "sortTable(9, true)" in src
    # and the worst-first modes are the ones triggering it
    assert "'losers'" in src and "'bottom10'" in src


# --- #5: lightweight analyst modal for non-owned Rating-moves names ----------

def _analyst_frame():
    return pd.DataFrame({
        "current_price": {"FANG": 180.0, "AAA": 50.0},
        "target_mean":   {"FANG": 210.0, "AAA": 55.0},
        "target_high":   {"FANG": 250.0, "AAA": 60.0},
        "target_low":    {"FANG": 190.0, "AAA": 48.0},
        "num_analysts":  {"FANG": 28,    "AAA": 10},
        "recommendation":{"FANG": "buy", "AAA": "hold"},
        "name":          {"FANG": "Diamondback Energy", "AAA": "Owned Co"},
    })


def _mv(ticker, kind="target", before=198.0, after=210.0, pct=6.1):
    return {"ticker": ticker, "kind": kind, "before": before, "after": after,
            "pct_change": pct, "abs_pct": abs(pct), "cur_rec": "buy", "cur_target": after}


def test_rmm_payload_excludes_owned_and_includes_nonowned():
    analyst = _analyst_frame()
    payload = build.build_rating_moves_modal_payload(
        [_mv("FANG"), _mv("AAA")], analyst, owned_tickers={"AAA"})
    assert "FANG" in payload and "AAA" not in payload
    d = payload["FANG"]
    assert "Diamondback Energy" in d["sub"]
    # implied upside (210/180-1)*100 = 16.7%
    assert "16.7%" in d["html"]
    # target range present
    assert "190" in d["html"] and "250" in d["html"]
    # coverage depth + the flagging move
    assert "28 analyst" in d["html"] and "Flagged" in d["html"]


def test_rmm_payload_skips_ticker_without_analyst_row():
    payload = build.build_rating_moves_modal_payload(
        [_mv("ZZZ")], _analyst_frame(), owned_tickers=set())
    assert payload == {}


def test_rmm_payload_empty_inputs_safe():
    assert build.build_rating_moves_modal_payload(None, _analyst_frame(), set()) == {}
    assert build.build_rating_moves_modal_payload([_mv("FANG")], None, set()) == {}
    assert build.build_rating_moves_modal_payload([_mv("FANG")], pd.DataFrame(), set()) == {}


def test_rmm_payload_escapes_adversarial_name():
    analyst = _analyst_frame()
    analyst.loc["FANG", "name"] = '<img src=x onerror=alert(1)>'
    payload = build.build_rating_moves_modal_payload(
        [_mv("FANG")], analyst, owned_tickers=set())
    assert "<img" not in payload["FANG"]["sub"]
    assert "&lt;" in payload["FANG"]["sub"]


def _card_fields(**over):
    base = {"ticker": "FANG", "name": "Diamondback", "sym": "$", "price": 180.0,
            "target": 210.0, "target_high": 250.0, "target_low": 190.0,
            "upside": 16.7, "rec": "buy", "n_analysts": 28,
            "moves": [{"kind": "target", "before": 198.0, "after": 210.0, "pct_change": 6.1}]}
    base.update(over)
    return base


def test_render_analyst_card_shows_price_target_range_upside_rec():
    html = build.render_analyst_card(_card_fields())
    assert "$180.00" in html and "$210.00" in html
    assert "16.7%" in html
    assert "190" in html and "250" in html         # range band
    assert "28 analyst" in html
    assert "Flagged" in html
    assert "discovery only" in html.lower() or "no position" in html.lower()


def test_render_analyst_card_omits_range_when_bounds_missing():
    html = build.render_analyst_card(_card_fields(target_high=None, target_low=None))
    assert "Target range" not in html


def test_render_analyst_card_handles_missing_numbers():
    html = build.render_analyst_card(
        _card_fields(price=None, target=None, upside=None, n_analysts=None))
    assert "&mdash;" in html                         # graceful blanks, no crash


def test_rm_analyst_modal_wired_in_page_source():
    src = _page_src()
    assert "const RM_ANALYST" in src
    assert "openAnalystInfo" in src
