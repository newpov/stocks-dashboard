"""v3.12 Shockwave hedge lens (short-side phase B): how much of a -1x S&P /
-1x Nasdaq fund would offset the basket's fitted sensitivities, what that does
in the active scenario and its mirror, and how -3x funds decay. No network."""
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import build

ROOT = Path(build.__file__).parent
JS = (ROOT / "assets" / "dashboard.js").read_text(encoding="utf-8")
SRC = Path(build.__file__).read_text(encoding="utf-8")


def _f(t, bm, bt, ccy="USD", w=1.0):
    return {"ticker": t, "b_mkt": bm, "b_tech": bt, "ccy": ccy, "weight": w}


def test_mix_offsets_both_factors():
    # basket exposures: market 1.2, tech 0.5 -> Nasdaq fund covers tech (and
    # 0.5 of market), S&P fund covers the remaining 0.7 of market
    mix = build.hedge_mix([_f("A", 1.4, 0.8), _f("B", 1.0, 0.2)])
    assert mix["b_mkt"] == pytest.approx(1.2) and mix["b_tech"] == pytest.approx(0.5)
    assert mix["h_ndx"] == pytest.approx(0.5) and mix["h_spx"] == pytest.approx(0.7)


def test_negative_tech_tilt_uses_sp_fund_only():
    mix = build.hedge_mix([_f("A", 0.9, -0.3)])
    assert mix["h_ndx"] == 0 and mix["h_spx"] == pytest.approx(0.9)


def test_weights_nan_betas_and_fx_share():
    mix = build.hedge_mix([_f("A", 1.0, 0.0, "USD", 3.0), _f("B", float("nan"), None, "GBP", 1.0)])
    assert mix["b_mkt"] == pytest.approx(0.75)          # NaN beta counts as 0, weight 1
    assert mix["fx_share"] == pytest.approx(0.75)        # 3 of 4 weight units in USD names


def test_hedged_move_neutralises_the_factors():
    mix = build.hedge_mix([_f("A", 1.2, 0.5, "GBP")])
    r = build.hedged_move(mix, {"spy": -20, "tech": -10, "usd": 0})
    assert r["unhedged"] == pytest.approx(1.2 * -20 + 0.5 * -10)   # -29
    assert abs(r["hedged"]) < 1e-9                                  # fully offset
    mirror = build.hedged_move(mix, {"spy": 20, "tech": 10, "usd": 0})
    assert mirror["unhedged"] == pytest.approx(29) and abs(mirror["hedged"]) < 1e-9


def test_hedge_has_no_currency_bet():
    # Funds assumed priced in the investor's currency (currency-hedged), so a
    # dollar move doesn't change the hedge; the basket's own USD exposure stays.
    mix = build.hedge_mix([_f("A", 1.0, 0.0, "USD")])
    r = build.hedged_move(mix, {"spy": 0, "tech": 0, "usd": 5})
    assert r["hedge"] == 0 and r["unhedged"] == pytest.approx(5.0)


def test_tech_heavier_than_market_never_overhedges():
    # Real basket 21 Nov: b_tech 1.11 > b_mkt 0.85. Covering all the tech with
    # the Nasdaq fund shorted the market 1.11x -> "hedged" basket +31% in a
    # crash, i.e. a net short bet. The funds together offset the market once.
    mix = build.hedge_mix([_f("A", 0.85, 1.11, "GBP")])
    assert mix["h_ndx"] == pytest.approx(0.85) and mix["h_spx"] == 0
    assert mix["tech_left"] == pytest.approx(1.11 - 0.85)
    r = build.hedged_move(mix, {"spy": -45, "tech": -15, "usd": 0})
    assert r["hedged"] == pytest.approx(-(1.11 - 0.85) * 15)    # only leftover tech


def test_empty_basket_has_nothing_to_hedge():
    mix = build.hedge_mix([])
    assert mix["h_spx"] == 0 and mix["h_ndx"] == 0


def test_inverse_decay_on_a_choppy_flat_index():
    # index alternates -10% / +11.11%: ends flat, but daily-reset funds lose
    lv = [100.0]
    for i in range(40):
        lv.append(lv[-1] * (0.9 if i % 2 == 0 else 1 / 0.9))
    s = pd.Series(lv, index=pd.bdate_range("2026-01-01", periods=len(lv)))
    d = build.inverse_fund_decay(s, days=40)
    assert abs(d["index"]) < 1e-6
    assert d["inv1"] < 0 and d["inv3"] < d["inv1"]       # leverage decays faster
    assert d["naive3"] == pytest.approx(0, abs=1e-6)
    assert d["decay3"] == pytest.approx(d["inv3"] - d["naive3"])


def test_inverse_decay_needs_enough_history():
    assert build.inverse_fund_decay(pd.Series([1.0, 2.0, 3.0])) is None
    assert build.inverse_fund_decay(None) is None


def test_payload_and_shell_carry_the_hedge_lens():
    pay = build.build_shockwave_payload([_f("A", 1.0, 0.3)], build.SHOCKWAVE_PRESETS,
                                        "2026-11-21", "GBP",
                                        hedge_decay={"spx": {"days": 63}, "ndx": None})
    assert pay["hedge"]["mix"]["h_spx"] == pytest.approx(0.7)
    assert pay["hedge"]["decay"]["spx"]["days"] == 63
    html = build.render_shockwave(pay)
    assert 'id="sw-hedge"' in html


def test_js_mirrors_the_hedge_maths_and_states_the_risks():
    assert "function hedgePnl(" in JS and "SHOCKWAVE.hedge" in JS
    low = JS.lower()
    assert "reset daily" in low and "not advice" in low
    assert "hedge_decay=hedge_decay" in SRC or "hedge_decay=_hedge_decay" in SRC
