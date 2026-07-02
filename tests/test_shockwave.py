# tests/test_shockwave.py
import numpy as np
import pandas as pd
import pytest
import build


def test_shockwave_presets_wellformed():
    P = build.SHOCKWAVE_PRESETS
    assert isinstance(P, list) and len(P) >= 6
    labels = [p["label"] for p in P]
    assert len(labels) == len(set(labels))                      # unique labels
    has_pos = any(p["spy"] > 0 for p in P)
    has_neg = any(p["spy"] < 0 for p in P)
    assert has_pos and has_neg                                  # positive AND negative
    for p in P:
        assert set(["label", "spy", "tech", "usd", "likelihood", "recovery"]).issubset(p)
        assert p["likelihood"] in {"common", "occasional", "rare"}
        if p["spy"] < 0:
            assert isinstance(p["recovery"], str) and p["recovery"]   # drawdowns carry recovery
        else:
            assert p["recovery"] is None                              # upside: no recovery


def _price_series(daily_rets, start="2024-01-01"):
    # build a price series whose pct_change equals daily_rets (prepend a base 100)
    lvl = [100.0]
    for r in daily_rets:
        lvl.append(lvl[-1] * (1 + r))
    idx = pd.date_range(start, periods=len(lvl), freq="D")
    return pd.Series(lvl, index=idx)


def _meta_df(rows):  # rows: (ticker, sector, ccy)
    return pd.DataFrame([{"sector": s, "name": t, "industry": "", "currency": c}
                         for (t, s, c) in rows], index=[t for (t, _, _) in rows])


def test_compute_stress_factors_recovers_known_loadings():
    rng = np.random.default_rng(0)
    n = 200
    spy_r = rng.normal(0, 0.01, n)
    tech_r = rng.normal(0, 0.01, n)          # this is the (QQQ - SPY) factor content
    qqq_r = spy_r + tech_r                    # so QQQ = SPY + techfactor
    # name = 1.3*SPY + 0.7*(QQQ-SPY), no noise -> exact fit
    name_r = 1.3 * spy_r + 0.7 * tech_r
    prices = pd.DataFrame({"AAA": _price_series(name_r)})
    spy = _price_series(spy_r)
    qqq = _price_series(qqq_r)
    meta = _meta_df([("AAA", "Tech", "USD")])
    returns = pd.DataFrame([{"status": "open", "total_pct": 12.0, "weight": 1.0}], index=["AAA"])
    out = build.compute_stress_factors(prices, spy, qqq, meta, returns)
    assert len(out) == 1
    f = out[0]
    assert f["ticker"] == "AAA"
    assert f["b_mkt"] == pytest.approx(1.3, abs=1e-3)
    assert f["b_tech"] == pytest.approx(0.7, abs=1e-3)
    assert f["r2"] == pytest.approx(1.0, abs=1e-3)
    assert f["ccy"] == "USD" and f["ret"] == 12.0 and f["weight"] == 1.0
    assert f["low_conf"] is False


def test_compute_stress_factors_short_history_low_conf():
    prices = pd.DataFrame({"AAA": _price_series([0.01, -0.01, 0.02])})
    spy = _price_series([0.01, -0.01, 0.02]); qqq = _price_series([0.01, -0.01, 0.02])
    meta = _meta_df([("AAA", "Tech", "USD")])
    returns = pd.DataFrame([{"status": "open", "total_pct": 1.0, "weight": 1.0}], index=["AAA"])
    out = build.compute_stress_factors(prices, spy, qqq, meta, returns)
    assert out[0]["low_conf"] is True


def test_compute_stress_factors_excludes_closed_and_missing():
    prices = pd.DataFrame({"AAA": _price_series([0.01] * 100)})
    spy = _price_series([0.01] * 100); qqq = _price_series([0.01] * 100)
    meta = _meta_df([("AAA", "Tech", "USD")])
    returns = pd.DataFrame([{"status": "closed", "total_pct": 5.0, "weight": 0.0}], index=["AAA"])
    assert build.compute_stress_factors(prices, spy, qqq, meta, returns) == []


def test_estimate_move_two_factors_plus_fx():
    f_usd = {"b_mkt": 1.5, "b_tech": 0.5, "ccy": "USD"}
    sc = {"spy": -20, "tech": -10, "usd": 5}
    # 1.5*-20 + 0.5*-10 + 5(USD) = -30 -5 +5 = -30
    assert build.estimate_move(f_usd, sc, "GBP") == pytest.approx(-30.0)
    f_gbp = {"b_mkt": 1.5, "b_tech": 0.5, "ccy": "GBP"}     # base ccy -> no FX term
    assert build.estimate_move(f_gbp, sc, "GBP") == pytest.approx(-35.0)
    f_eur = {"b_mkt": 1.0, "b_tech": 0.0, "ccy": "EUR"}     # non-USD foreign -> no USD term
    assert build.estimate_move(f_eur, sc, "GBP") == pytest.approx(-20.0)


def test_estimate_move_nan_beta_contributes_zero():
    f = {"b_mkt": float("nan"), "b_tech": float("nan"), "ccy": "GBP"}
    assert build.estimate_move(f, {"spy": -20, "tech": -10, "usd": 0}, "GBP") == 0.0


def test_basket_weighted_move_uniform_vs_weighted():
    fs = [{"b_mkt": 2.0, "b_tech": 0.0, "ccy": "GBP", "weight": 1.0},
          {"b_mkt": 0.0, "b_tech": 0.0, "ccy": "GBP", "weight": 1.0}]
    sc = {"spy": -10, "tech": 0, "usd": 0}
    assert build.basket_weighted_move(fs, sc, "GBP") == pytest.approx(-10.0)   # mean(-20,0)
    fs[0]["weight"] = 3.0                                                       # weight the mover
    assert build.basket_weighted_move(fs, sc, "GBP") == pytest.approx(-15.0)   # (3*-20+1*0)/4


def test_recovery_estimate_bands():
    assert build.recovery_estimate(-3) is None
    assert build.recovery_estimate(-10) == "~1 year"
    assert build.recovery_estimate(-20) == "~2 years"
    assert build.recovery_estimate(-35) == "~4 years"
    assert build.recovery_estimate(-55) == "~5-6 years"
    assert build.recovery_estimate(12) is None


def _factor(t, ccy="USD", w=1.0):
    return {"ticker": t, "name": t, "sector": "Tech", "b_mkt": 1.0, "b_tech": 0.2,
            "r2": 0.6, "ccy": ccy, "ret": 5.0, "weight": w, "low_conf": False}


def test_build_shockwave_payload_shape_and_privacy():
    factors = [_factor("AAA"), _factor("BBB", ccy="GBP")]
    pl = build.build_shockwave_payload(
        factors, build.SHOCKWAVE_PRESETS, "30 Jun 2026", "GBP",
        mcap_by_ticker={"AAA": 2.5e12})
    assert pl["base_ccy"] == "GBP" and pl["as_of"] == "30 Jun 2026"
    assert pl["size_default"] in {"eq", "w"}
    assert pl["fx_ccy"] == "USD"
    assert len(pl["factors"]) == 2
    assert pl["factors"][0]["mcap"] == 2.5e12
    assert pl["factors"][1]["mcap"] is None            # missing -> graceful None
    blob = str(pl)
    assert "shares" not in blob and "£" not in blob   # no shares, no GBP symbol
    assert pl["presets"][0]["label"] == build.SHOCKWAVE_PRESETS[0]["label"]


def test_render_shockwave_shell():
    pl = {"factors": [], "presets": [], "base_ccy": "GBP", "as_of": "30 Jun 2026",
          "size_default": "eq"}
    html = build.render_shockwave(pl)
    assert 'id="shockwave-wrap"' in html
    assert "Shockwave" in html
    assert "as of 30 Jun 2026" in html
    for cid in ("sw-chips", "sw-sliders", "sw-hero", "sw-action", "sw-field",
                "sw-impact", "sw-size"):
        assert 'id="' + cid + '"' in html


def test_render_shockwave_escapes_as_of():
    pl = {"factors": [], "presets": [], "base_ccy": "GBP",
          "as_of": '<img src=x onerror=alert(1)>', "size_default": "eq"}
    html = build.render_shockwave(pl)
    assert "<img src=x" not in html and "&lt;img" in html


def test_render_html_accepts_stress_factors_param():
    import inspect
    assert "stress_factors" in inspect.signature(build.render_html).parameters


def test_render_html_source_has_shockwave_hooks():
    import inspect
    src = inspect.getsource(build.render_html)
    assert "render_shockwave(" in src
    assert "build_shockwave_payload(" in src
    assert "{shockwave_html}" in src
    assert "SHOCKWAVE" in src


def test_main_source_calls_compute_stress_factors():
    import inspect
    src = inspect.getsource(build.main)
    assert "compute_stress_factors(" in src
    assert "stress_factors=" in src


def test_shockwave_button_and_js_in_source():
    import inspect
    src = inspect.getsource(build.render_html)
    assert 'id="shockwave-btn"' in src
    assert "setupShockwave" in src
    assert ".shockwave-wrap" in src


def test_estimate_move_floors_at_total_loss():
    # A position can't lose more than 100%; the linear model must not overshoot.
    f = {"b_mkt": 2.5, "b_tech": 0.5, "ccy": "GBP"}
    assert build.estimate_move(f, {"spy": -45, "tech": -15, "usd": 0}, "GBP") == -100.0
