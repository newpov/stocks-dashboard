# tests/test_doctor.py
import inspect
import numpy as np
import pandas as pd
import pytest
import build


def _series(vals, start="2025-01-01"):
    idx = pd.date_range(start, periods=len(vals), freq="D")
    return pd.Series(vals, index=idx, dtype=float)


def test_basket_beta_perfectly_correlated_unit_slope():
    # basket == bench (same daily returns) -> beta 1.0
    bench = _series([100, 101, 102, 101, 103, 104])
    basket = bench.copy()
    assert build.basket_beta(basket, bench) == pytest.approx(1.0, abs=1e-9)


def test_basket_beta_double_amplitude_slope_two():
    bench = _series([100, 110, 121, 108.9])          # +10%, +10%, -10%
    basket = _series([100, 120, 144, 115.2])          # +20%, +20%, -20%
    assert build.basket_beta(basket, bench) == pytest.approx(2.0, abs=1e-6)


def test_basket_beta_insufficient_or_flat_is_nan():
    assert np.isnan(build.basket_beta(_series([100]), _series([100])))
    flat = _series([100, 100, 100, 100])
    assert np.isnan(build.basket_beta(_series([100, 101, 102, 103]), flat))


def _returns_df(rows):
    # rows: list of (ticker, status, total_pct)
    return pd.DataFrame(
        [{"status": s, "total_pct": p} for (_, s, p) in rows],
        index=[t for (t, _, _) in rows],
    )


def test_pct_open_underwater_counts_only_open_below_zero():
    df = _returns_df([
        ("AAA", "open", -5.0),
        ("BBB", "open", 12.0),
        ("CCC", "open", -0.1),
        ("DDD", "closed", -99.0),   # closed: ignored
    ])
    # 2 of 3 open names underwater -> 66.67%
    assert build.pct_open_underwater(df) == pytest.approx(200.0 / 3.0, abs=1e-6)


def test_pct_open_underwater_no_open_is_nan():
    df = _returns_df([("DDD", "closed", -99.0)])
    assert np.isnan(build.pct_open_underwater(df))


def _meta_df(rows):
    # rows: list of (ticker, sector)
    return pd.DataFrame(
        [{"sector": s, "name": t, "industry": "", "currency": "USD"} for (t, s) in rows],
        index=[t for (t, _) in rows],
    )


def test_sector_effective_n_two_equal_sectors():
    returns = _returns_df([
        ("AAA", "open", 1.0), ("BBB", "open", 1.0),
        ("CCC", "open", 1.0), ("DDD", "open", 1.0),
    ])
    meta = _meta_df([("AAA", "Tech"), ("BBB", "Tech"),
                     ("CCC", "Energy"), ("DDD", "Energy")])
    out = build.sector_effective_n(returns, meta)
    assert out["hhi"] == pytest.approx(0.5, abs=1e-9)
    assert out["effective_n"] == pytest.approx(2.0, abs=1e-9)
    assert out["top_share"] == pytest.approx(0.5, abs=1e-9)
    assert out["n_sectors"] == 2


def test_sector_effective_n_concentrated():
    returns = _returns_df([("AAA", "open", 1.0), ("BBB", "open", 1.0),
                           ("CCC", "open", 1.0), ("DDD", "open", 1.0)])
    meta = _meta_df([("AAA", "Tech"), ("BBB", "Tech"),
                     ("CCC", "Tech"), ("DDD", "Energy")])
    out = build.sector_effective_n(returns, meta)
    assert out["top_sector"] == "Tech"
    assert out["top_share"] == pytest.approx(0.75, abs=1e-9)


def test_sector_effective_n_missing_sector_is_other_and_empty_safe():
    returns = _returns_df([("AAA", "open", 1.0)])
    meta = _meta_df([])  # no sector info
    out = build.sector_effective_n(returns, meta)
    assert out["top_sector"] == "Other"
    empty = build.sector_effective_n(_returns_df([("Z", "closed", 1.0)]), meta)
    assert empty["n_sectors"] == 0 and np.isnan(empty["effective_n"])


def test_basket_vol_trend_rising_when_recent_choppier():
    # 40 calm days (+/-0.1%) then 20 choppy days (+/-3%)
    import numpy as np
    calm = []
    v = 100.0
    for i in range(40):
        v *= (1 + (0.001 if i % 2 == 0 else -0.001))
        calm.append(v)
    choppy = []
    for i in range(20):
        v *= (1 + (0.03 if i % 2 == 0 else -0.03))
        choppy.append(v)
    basket = _series(calm + choppy)
    out = build.basket_vol_trend(basket, recent_days=20)
    assert out["recent_vol"] > out["baseline_vol"]
    assert out["rising"] is True
    assert out["vol"] > 0


def test_basket_vol_trend_short_series_safe():
    out = build.basket_vol_trend(_series([100, 101]), recent_days=20)
    assert np.isnan(out["vol"]) and out["rising"] is False


def _healthy_metrics():
    return {"drawdown_pct": -2.0, "alpha_30d_pp": 3.0, "beta": 1.0,
            "pct_underwater": 20.0, "effective_n": 6.0, "top_sector": "Tech",
            "top_share": 0.2, "vol_rising": False,
            "detractor_cluster_sector": "", "breadth_down_frac": 0.1}


def test_evaluate_health_zero_flags_healthy():
    state, drivers = build.evaluate_health(_healthy_metrics())
    assert state == "Healthy" and drivers == []


def test_evaluate_health_one_flag_watch():
    m = _healthy_metrics(); m["drawdown_pct"] = -15.0
    state, drivers = build.evaluate_health(m)
    assert state == "Watch" and len(drivers) == 1
    assert drivers[0]["tone"] in {"danger", "warning", "neutral"}


def test_evaluate_health_two_plus_flags_needs_attention():
    m = _healthy_metrics()
    m["drawdown_pct"] = -15.0          # flag 1
    m["pct_underwater"] = 70.0         # flag 2
    m["top_share"] = 0.50              # flag 3
    state, drivers = build.evaluate_health(m)
    assert state == "Needs attention" and len(drivers) >= 2


def test_evaluate_health_beta_only_flags_with_thin_alpha():
    m = _healthy_metrics(); m["beta"] = 1.5; m["alpha_30d_pp"] = 5.0
    state, _ = build.evaluate_health(m)            # high beta but fat alpha -> ok
    assert state == "Healthy"
    m["alpha_30d_pp"] = 1.0                          # high beta + thin alpha -> flag
    state2, drivers2 = build.evaluate_health(m)
    assert state2 == "Watch"
    assert any("leverage" in d["label"].lower() for d in drivers2)


def test_doctor_diagnosis_healthy_is_calm_and_plain():
    txt = build._doctor_diagnosis(_healthy_metrics(), [])
    assert isinstance(txt, str) and len(txt) > 0
    assert "<" not in txt  # plain text, no HTML
    assert "healthy" in txt.lower() or "no red flags" in txt.lower()


def test_doctor_diagnosis_names_the_top_driver():
    m = _healthy_metrics(); m["top_share"] = 0.5; m["top_sector"] = "Energy"
    _, drivers = build.evaluate_health(m)
    txt = build._doctor_diagnosis(m, drivers)
    assert "Energy" in txt


def test_doctor_diagnosis_mentions_beta_when_leverage_flag():
    m = _healthy_metrics(); m["beta"] = 1.4; m["alpha_30d_pp"] = 1.0
    _, drivers = build.evaluate_health(m)
    txt = build._doctor_diagnosis(m, drivers)
    assert "1.4" in txt or "beta" in txt.lower()


def _signals_df(rows):
    # rows: list of (ticker, label)
    return pd.DataFrame([{"signal": lab} for (_, lab) in rows],
                        index=[t for (t, _) in rows])


def test_pick_defend_sector_overtilt():
    returns = _returns_df([("AAA", "open", 5.0), ("BBB", "open", 6.0),
                           ("CCC", "open", 7.0), ("DDD", "open", 8.0)])
    meta = _meta_df([("AAA", "Tech"), ("BBB", "Tech"),
                     ("CCC", "Tech"), ("DDD", "Energy")])  # Tech 75%
    metrics = {"top_sector": "Tech", "top_share": 0.75}
    act = build.pick_defend(returns, meta, _signals_df([]), pd.DataFrame(),
                            None, [], [], metrics)
    assert act["lens"] == "defend" and act["verb"] == "trim"
    assert "Tech" in act["why"] and act["module_key"] == "industry"
    assert set(act["tickers"]).issubset({"AAA", "BBB", "CCC"})


def test_pick_defend_downtrend_exit_beats_mild_tilt():
    returns = _returns_df([("AAA", "open", 5.0), ("BBB", "open", -35.0),
                           ("CCC", "open", 4.0)])
    meta = _meta_df([("AAA", "Tech"), ("BBB", "Energy"), ("CCC", "Health")])
    signals = _signals_df([("AAA", "Trending up"), ("BBB", "Strong downtrend"),
                           ("CCC", "Pullback")])
    metrics = {"top_sector": "Tech", "top_share": 0.34}  # below tilt floor-ish
    act = build.pick_defend(returns, meta, signals, pd.DataFrame(),
                            None, [], [], metrics)
    assert act["verb"] == "exit" and act["tickers"] == ["BBB"]
    assert act["module_key"] == "exit"


def test_pick_defend_none_when_nothing_material():
    returns = _returns_df([("AAA", "open", 5.0), ("BBB", "open", 6.0),
                           ("CCC", "open", 7.0), ("DDD", "open", 8.0)])
    meta = _meta_df([("AAA", "Tech"), ("BBB", "Energy"),
                     ("CCC", "Health"), ("DDD", "Fin")])
    metrics = {"top_sector": "Tech", "top_share": 0.25}
    act = build.pick_defend(returns, meta, _signals_df([]), pd.DataFrame(),
                            None, [], [], metrics)
    assert act is not None and "none_reason" in act and act["lens"] == "defend"


def test_pick_defend_fx_uses_real_ccy_key():
    # compute_currency_exposure emits dicts keyed "ccy" (not "currency").
    # The FX defend candidate must surface the actual currency name, not "None".
    returns = _returns_df([("AAA", "open", 5.0), ("BBB", "open", 6.0),
                           ("CCC", "open", 7.0), ("DDD", "open", 8.0)])
    meta = _meta_df([("AAA", "Tech"), ("BBB", "Energy"),
                     ("CCC", "Health"), ("DDD", "Fin")])  # no sector tilt
    metrics = {"top_sector": "Tech", "top_share": 0.25}
    ccy_rows = [{"ccy": "USD", "share": 0.7, "n": 3}]
    act = build.pick_defend(returns, meta, _signals_df([]), pd.DataFrame(),
                            None, ccy_rows, [], metrics)
    assert act["verb"] == "review" and act["module_key"] == "currency"
    assert "USD" in act["why"] and "None" not in act["why"]


def _quant_df(rows):
    # rows: list of (ticker, rsi)
    return pd.DataFrame([{"rsi": r} for (_, r) in rows],
                        index=[t for (t, _) in rows])


def _returns_df_full(rows):
    # rows: list of dicts with at least ticker(index),status,total_pct,1m_pct,3m_pct
    idx = [r.pop("ticker") for r in rows]
    return pd.DataFrame(rows, index=idx)


def test_pick_tune_take_profit_on_overbought_winner():
    returns = _returns_df_full([
        {"ticker": "AAA", "status": "open", "total_pct": 60.0, "1m_pct": 5.0, "3m_pct": 20.0},
        {"ticker": "BBB", "status": "open", "total_pct": 3.0, "1m_pct": 1.0, "3m_pct": 2.0},
    ])
    quant = _quant_df([("AAA", 82.0), ("BBB", 50.0)])
    act = build.pick_tune(returns, quant, _meta_df([("AAA", "Tech"), ("BBB", "Tech")]), {})
    assert act["verb"] == "take profit" and act["tickers"] == ["AAA"]
    assert act["module_key"] == "signal"


def test_pick_tune_none_when_nothing_to_adjust():
    returns = _returns_df_full([
        {"ticker": "AAA", "status": "open", "total_pct": 8.0, "1m_pct": 2.0, "3m_pct": 4.0},
    ])
    quant = _quant_df([("AAA", 55.0)])
    act = build.pick_tune(returns, quant, _meta_df([("AAA", "Tech")]), {})
    assert act is not None and "none_reason" in act and act["lens"] == "tune"


def test_pick_grow_prefers_higher_conviction():
    value_rows = [
        {"ticker": "VVV", "name": "Value Co", "sector": "Tech", "pass_count": 5, "is_bb_idea": False},
        {"ticker": "WWW", "name": "Weak Co", "sector": "Tech", "pass_count": 3, "is_bb_idea": False},
    ]
    returns = _returns_df([("AAA", "open", 5.0)])
    meta = _meta_df([("AAA", "Tech")])
    act = build.pick_grow(value_rows, [], [], {}, [], returns, meta, {})
    assert act["lens"] == "grow" and act["verb"] == "add"
    assert act["tickers"] == ["VVV"] and act["module_key"] == "value"


def test_pick_grow_fit_bonus_flips_pick_to_fill_gap():
    # Equal base conviction; GAP candidate is in a sector the basket lacks.
    value_rows = [
        {"ticker": "TTT", "name": "Tech Co", "sector": "Tech", "pass_count": 4, "is_bb_idea": False},
        {"ticker": "HHH", "name": "Health Co", "sector": "Health", "pass_count": 4, "is_bb_idea": False},
    ]
    returns = _returns_df([("AAA", "open", 5.0), ("BBB", "open", 6.0)])
    meta = _meta_df([("AAA", "Tech"), ("BBB", "Tech")])   # basket is all Tech
    act = build.pick_grow(value_rows, [], [], {}, [], returns, meta, {})
    assert act["tickers"] == ["HHH"]
    assert "health" in act["why"].lower() or "only" in act["why"].lower()


def test_pick_grow_empty_pool_is_honest_nopick():
    returns = _returns_df([("AAA", "open", 5.0)])
    meta = _meta_df([("AAA", "Tech")])
    act = build.pick_grow([], [], [], {}, [], returns, meta, {})
    assert act is not None and "none_reason" in act and act["lens"] == "grow"


def test_compute_doctor_report_shape_and_three_actions():
    basket = _series([100, 102, 101, 105, 108, 110] + [110] * 40)
    bench = _series([100, 101, 101, 103, 104, 105] + [105] * 40)
    returns = _returns_df_full([
        {"ticker": "AAA", "status": "open", "total_pct": 60.0, "1m_pct": 4.0, "3m_pct": 20.0},
        {"ticker": "BBB", "status": "open", "total_pct": -8.0, "1m_pct": -2.0, "3m_pct": -3.0},
        {"ticker": "CCC", "status": "open", "total_pct": 12.0, "1m_pct": 1.0, "3m_pct": 5.0},
    ])
    meta = _meta_df([("AAA", "Tech"), ("BBB", "Tech"), ("CCC", "Tech")])
    quant = _quant_df([("AAA", 82.0), ("BBB", 40.0), ("CCC", 55.0)])
    value_rows = [{"ticker": "ZZZ", "name": "Z", "sector": "Health",
                   "pass_count": 5, "is_bb_idea": False}]
    rep = build.compute_doctor_report(
        returns=returns, meta=meta, basket=basket, bench=bench,
        contrib=pd.DataFrame(), signals=_signals_df([]), analyst=pd.DataFrame(),
        quant_metrics=quant, diversification_data=None, ccy_exposure_rows=[],
        industry_groups=[], value_rows=value_rows, auto_tickers=[],
        bb_universe_obs=[], watchlist_payload={}, analyst_rows=[], as_of="30 Jun 2026")
    assert rep["state"] in {"Healthy", "Watch", "Needs attention"}
    assert set(rep["actions"]) == {"defend", "tune", "grow"}
    for lens in ("defend", "tune", "grow"):
        a = rep["actions"][lens]
        assert ("verb" in a) or ("none_reason" in a)
    assert rep["as_of"] == "30 Jun 2026"
    assert isinstance(rep["metrics"]["beta"], float)


def test_compute_doctor_report_handles_percent_return_series():
    # Real pipeline passes CUMULATIVE-RETURN-PERCENT series (start at 0, cross
    # zero) -- not price levels. Beta/vol/drawdown must be finite, not NaN from
    # pct_change() dividing by a 0.0 starting value.
    lvl_b = _series([100, 102, 101, 105, 108, 110] + list(range(110, 150)))
    # identical shape for basket and bench -> beta ~ 1.0
    pct_b = (lvl_b / float(lvl_b.iloc[0]) - 1.0) * 100.0   # starts at 0.0
    pct_bench = pct_b.copy()
    returns = _returns_df([("AAA", "open", 5.0), ("BBB", "open", -3.0)])
    meta = _meta_df([("AAA", "Tech"), ("BBB", "Energy")])
    rep = build.compute_doctor_report(
        returns=returns, meta=meta, basket=pct_b, bench=pct_bench,
        contrib=pd.DataFrame(), signals=_signals_df([]), analyst=pd.DataFrame(),
        quant_metrics=pd.DataFrame(), diversification_data=None, ccy_exposure_rows=[],
        industry_groups=[], value_rows=[], auto_tickers=[], bb_universe_obs=[],
        watchlist_payload={}, analyst_rows=[], as_of="x")
    beta = rep["metrics"]["beta"]
    assert beta == beta and abs(beta - 1.0) < 1e-6      # finite AND ~1.0
    assert rep["metrics"]["vol"] == rep["metrics"]["vol"]        # not NaN
    assert rep["metrics"]["drawdown_pct"] == rep["metrics"]["drawdown_pct"]
    assert "n/a beta" not in rep["diagnosis"]


def test_compute_doctor_report_never_raises_on_degenerate():
    basket = _series([100, 101])
    rep = build.compute_doctor_report(
        returns=pd.DataFrame(), meta=pd.DataFrame(), basket=basket, bench=basket,
        contrib=pd.DataFrame(), signals=pd.DataFrame(), analyst=pd.DataFrame(),
        quant_metrics=pd.DataFrame(), diversification_data=None, ccy_exposure_rows=[],
        industry_groups=[], value_rows=[], auto_tickers=[], bb_universe_obs=[],
        watchlist_payload={}, analyst_rows=[], as_of="x")
    assert "none_reason" in rep["actions"]["grow"]


def _sample_report(**over):
    base = {
        "state": "Watch",
        "drivers": [{"label": "Tech concentration (75%)", "tone": "warning"}],
        "diagnosis": "The main thing to watch is Tech concentration (75%).",
        "metrics": {"beta": 1.3},
        "as_of": "30 Jun 2026",
        "actions": {
            "defend": {"lens": "defend", "tickers": ["AAA", "BBB"], "verb": "trim",
                       "why": "Tech is 75% of your open names.",
                       "module_key": "industry", "module_label": "Industry outlook"},
            "tune": {"lens": "tune", "tickers": ["CCC"], "verb": "take profit",
                     "why": "CCC overbought.", "module_key": "signal",
                     "module_label": "Signal map"},
            "grow": {"lens": "grow", "none_reason": "Nothing compelling to add."},
        },
    }
    base.update(over)
    return base


def test_render_doctor_has_panel_state_and_three_cards():
    html = build.render_doctor(_sample_report())
    assert 'id="doctor-wrap"' in html
    assert "Basket check-up" in html
    assert "Watch" in html
    assert "Defend" in html and "Tune" in html and "Grow" in html
    assert "as of 30 Jun 2026" in html
    assert "Nothing compelling to add." in html   # honest no-pick rendered


def test_render_doctor_escapes_adversarial_text():
    rep = _sample_report()
    rep["actions"]["defend"]["why"] = '<img src=x onerror=alert(1)>'
    rep["actions"]["defend"]["tickers"] = ['<script>']
    html = build.render_doctor(rep)
    assert "<img src=x" not in html and "<script>" not in html
    assert "&lt;" in html


def test_doctor_public_surface_importable():
    for fn in ("basket_beta", "pct_open_underwater", "sector_effective_n",
               "basket_vol_trend", "evaluate_health", "compute_doctor_report",
               "render_doctor"):
        assert callable(getattr(build, fn))


def test_doctor_button_and_panel_in_page_source():
    # The page f-string (returned by render_html) must contain the Doctor button
    # id, the panel placeholder, and the toggle IIFE. Assert against the source
    # so we don't need a full network build here.
    src = inspect.getsource(build.render_html)
    assert 'id="doctor-btn"' in src
    assert "{doctor_html}" in src
    assert "setupDoctor" in src


# --- v3.4 #1: Doctor "see <module>" is a working link ------------------------

def test_render_doctor_link_carries_mapped_module_dom_target():
    # The action's module_key ("industry", "signal") must be translated to the
    # actual DOM module id ("outlook", "quadrant") so the click can scroll there.
    html = build.render_doctor(_sample_report())
    assert 'data-module-target="outlook"' in html      # industry -> outlook module
    assert 'data-module-target="quadrant"' in html      # signal   -> quadrant module
    # The human label is still shown next to the arrow.
    assert "Industry outlook" in html and "Signal map" in html


def test_render_doctor_link_is_actionable_element():
    # Not a bare <div>: it must be clickable (a real anchor/button the JS wires).
    html = build.render_doctor(_sample_report())
    # locate the defend card's link fragment
    assert 'class="doc-link"' in html
    # an actionable element: <a ...> or role="button" or an href anchor target
    assert ('<a ' in html and 'doc-link' in html) or 'role="button"' in html


def test_render_doctor_no_link_when_module_key_absent():
    rep = _sample_report()
    # a none-reason card has no module_key; a card can also lack the key
    rep["actions"]["tune"].pop("module_key", None)
    rep["actions"]["tune"].pop("module_label", None)
    html = build.render_doctor(rep)
    # only the defend card (industry) should emit a target now
    assert html.count("data-module-target") == 1


def test_module_key_to_dom_id_map_covers_all_doctor_keys():
    # Every module_key the Doctor pickers emit must have a DOM-id mapping.
    emitted = {"industry", "exit", "currency", "signal", "holdings",
               "value", "watchlist"}
    for key in emitted:
        assert build._DOCTOR_MODULE_DOM.get(key), f"unmapped module_key {key}"


def test_doctor_links_wired_in_page_source():
    src = inspect.getsource(build.render_html)
    # modules carry a stable scroll id, and a handler targets the doctor links
    assert 'id="module-' in src
    assert "doc-link[data-module-target]" in src
