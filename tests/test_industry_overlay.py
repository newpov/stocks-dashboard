"""v3.4 #4: industry overlay on the hero chart. Pure + offline."""
import inspect

import pandas as pd

import build


def _prices(tkrs, days=40, start="2025-01-01"):
    idx = pd.date_range(start, periods=days, freq="D")
    data = {t: [100 * (1 + (0.004 * (j + 1)) * i / days) for i in range(days)]
            for j, t in enumerate(tkrs)}
    return pd.DataFrame(data, index=idx)


def _txns(tkrs, start="2025-01-01"):
    d = pd.Timestamp(start)
    return pd.DataFrame([{"ticker": t, "date": d, "action": "BUY", "shares": 1.0}
                         for t in tkrs])


def _meta(mapping):
    return pd.DataFrame({"industry": mapping,
                         "name": {t: t for t in mapping},
                         "sector": {t: "" for t in mapping},
                         "currency": {t: "USD" for t in mapping}})


def _returns(open_tkrs):
    return pd.DataFrame({"status": {t: "open" for t in open_tkrs}})


def test_owned_industries_ranked_by_window_return_top3():
    tkrs = ["A", "B", "C", "D"]
    prices = _prices(tkrs)
    meta = _meta({"A": "Tech", "B": "Health", "C": "Energy", "D": "Fin"})
    out = build.build_industry_overlay_series(
        _txns(tkrs), prices, meta, _returns(tkrs), industry_groups=[], n_owned=3)
    owned = [e for e in out if e["kind"] == "owned"]
    assert len(owned) == 3
    for e in owned:
        assert e["series"]["dates"] and e["series"]["values"]
        assert e["color"].startswith("#")


def test_non_owned_from_groups_excludes_owned_industries():
    tkrs = ["A"]
    prices = _prices(tkrs)
    meta = _meta({"A": "Tech"})
    groups = [{"industry": "Tech", "avg_ret_12m": 50.0},
              {"industry": "Semis", "avg_ret_12m": 30.0},
              {"industry": "Utilities", "avg_ret_12m": 12.0},
              {"industry": "Mining", "avg_ret_12m": 8.0}]
    out = build.build_industry_overlay_series(
        _txns(tkrs), prices, meta, _returns(tkrs), industry_groups=groups,
        n_owned=3, n_nonowned=2)
    non = [e for e in out if e["kind"] == "outlook"]
    assert [e["name"] for e in non] == ["Semis", "Utilities"]
    assert non[0]["endpoint"] == 30.0
    assert "series" not in non[0] and "endpoint" in non[0]


def test_non_owned_skips_glitch_outliers():
    # A cap-weighted 12m return of +1600% is a data glitch (bad constituent
    # price); it must not be surfaced as a "top performer".
    out = build.build_industry_overlay_series(
        _txns(["A"]), _prices(["A"]), _meta({"A": "Tech"}), _returns(["A"]),
        industry_groups=[{"industry": "Glitch", "avg_ret_12m": 1605.0},
                         {"industry": "Semis", "avg_ret_12m": 40.0}],
        n_nonowned=2)
    non = [e["name"] for e in out if e["kind"] == "outlook"]
    assert "Glitch" not in non
    assert "Semis" in non


def test_empty_inputs_safe():
    assert build.build_industry_overlay_series(
        pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame(),
        industry_groups=[]) == []


def test_single_name_industry_still_produces_line():
    out = build.build_industry_overlay_series(
        _txns(["A"]), _prices(["A"]), _meta({"A": "Tech"}), _returns(["A"]),
        industry_groups=[], n_owned=3)
    assert [e["name"] for e in out] == ["Tech"]
    assert out[0]["kind"] == "owned"


def test_public_surface_importable():
    assert callable(build.build_industry_overlay_series)


# --- IO-T2: legend toggle + payload merge ------------------------------------

def test_hero_legend_has_industries_toggle():
    html = build._hero_legend_html(
        True, industries=[{"name": "Tech", "kind": "owned", "color": "#5dcaa5"},
                          {"name": "Semis", "kind": "outlook", "color": "#d4537e"}])
    assert 'data-series="industries"' in html
    assert "Tech" in html and "Semis" in html
    assert "#d4537e" in html          # non-owned swatch colour inline


def test_hero_legend_no_industries_when_empty():
    html = build._hero_legend_html(True, industries=[])
    assert 'data-series="industries"' not in html


def test_industry_overlay_wired_in_render_html():
    src = inspect.getsource(build.render_html)
    assert "build_industry_overlay_series(" in src
    assert '"industries"' in src


# --- IO-T3: hero JS + toggle -------------------------------------------------

def test_industry_overlay_js_and_toggle_present():
    src = inspect.getsource(build.render_html)
    assert "showIndustries" in src
    assert "heroIndustries" in src
    assert 'data-series="industries"' in src
    assert "PORTFOLIO.industries" in src
