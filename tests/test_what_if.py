"""v3.6 What-if: pool + payload + math. Pure + offline."""
import inspect

import pandas as pd

import build


def _hist(rows):
    return pd.DataFrame([{"date": pd.Timestamp(d), "ticker": t, "source": s}
                        for (d, t, s) in rows])


def _wl(tickers, kinds=None):
    kinds = kinds or ["manual"] * len(tickers)
    return pd.DataFrame({"ticker": tickers, "note": [""] * len(tickers),
                         "wl_kind": kinds})


def test_pool_constants():
    assert build.WHATIF_POOL_MAX == 60
    assert build.WHATIF_KEEP_DAYS == 90


def test_pool_union_watchlist_first_then_history_by_recency():
    wl = _wl(["AAA", "BBB"], ["auto", "manual"])
    h = _hist([("2026-07-01", "CCC", "BB"), ("2026-06-01", "DDD", "VS"),
               ("2026-07-01", "AAA", "BB")])
    pool = build.build_what_if_pool(wl, h, owned=set(),
                                    today=pd.Timestamp("2026-07-04"))
    tks = [p["ticker"] for p in pool]
    assert tks[:2] == ["AAA", "BBB"]          # watchlist first, order kept
    assert tks[2:] == ["CCC", "DDD"]          # then history, newest flag first
    aaa = pool[0]
    assert aaa["flagged_now"] is True         # on current watchlist
    assert aaa["sources"] == ["BB"]
    ccc = pool[2]
    assert ccc["flagged_now"] is False
    assert ccc["last_flagged"] == "2026-07-01"


def test_pool_excludes_owned_and_stale_history():
    wl = _wl(["AAA"])
    h = _hist([("2026-01-01", "OLD", "BB"), ("2026-07-01", "OWN", "BB")])
    pool = build.build_what_if_pool(wl, h, owned={"OWN"},
                                    today=pd.Timestamp("2026-07-04"),
                                    keep_days=90)
    tks = [p["ticker"] for p in pool]
    assert "OLD" not in tks and "OWN" not in tks and "AAA" in tks


def test_pool_cap_and_empty_inputs_safe():
    wl = _wl([f"T{i:02d}" for i in range(70)])
    pool = build.build_what_if_pool(wl, None, owned=set(),
                                    today=pd.Timestamp("2026-07-04"), cap=60)
    assert len(pool) == 60
    assert build.build_what_if_pool(None, None, owned=set(),
                                    today=pd.Timestamp("2026-07-04")) == []


def _prices(dates, cols):
    idx = pd.to_datetime(dates)
    return pd.DataFrame({t: v for t, v in cols.items()}, index=idx)


_D4 = ["2026-07-01", "2026-07-02", "2026-07-03", "2026-07-04"]


def test_payload_cum_returns_from_gbp_prices():
    pool = [{"ticker": "AAA", "flagged_now": True, "last_flagged": "", "sources": ["BB"]}]
    pr = _prices(_D4, {"AAA": [100.0, 110.0, 99.0, 121.0]})
    out = build.build_what_if_payload(pool, pr, _D4, n_open=10,
                                      name_lookup={"AAA": "Aco"})
    assert out["dates"] == _D4 and out["n_open"] == 10
    d = out["names"]["AAA"]
    assert d["name"] == "Aco" and d["flagged_now"] is True
    assert d["cum"] == [0.0, 10.0, -1.0, 21.0]     # (p/p0-1)*100


def test_payload_excludes_missing_or_sparse_series():
    pool = [{"ticker": "GONE", "flagged_now": False, "last_flagged": "", "sources": []},
            {"ticker": "SPARSE", "flagged_now": True, "last_flagged": "", "sources": []}]
    pr = _prices(_D4, {"SPARSE": [100.0, None, None, None]})
    out = build.build_what_if_payload(pool, pr, _D4, n_open=5)
    assert out["names"] == {}                       # GONE absent, SPARSE <80% cover


def test_payload_ffills_small_gaps():
    pool = [{"ticker": "AAA", "flagged_now": True, "last_flagged": "", "sources": []}]
    pr = _prices(_D4, {"AAA": [100.0, None, 105.0, 110.0]})
    out = build.build_what_if_payload(pool, pr, _D4, n_open=5)
    assert out["names"]["AAA"]["cum"] == [0.0, 0.0, 5.0, 10.0]


def test_payload_empty_inputs_safe():
    assert build.build_what_if_payload([], None, [], n_open=0) == \
        {"dates": [], "n_open": 0, "names": {}}


def test_what_if_wired_in_render_html():
    src = inspect.getsource(build.render_html)
    assert "const WHATIF" in src
    assert "whatif_json" in src
    assert "build_what_if_pool(" in src
    assert "build_what_if_payload(" in src


def test_hero_legend_has_what_if_toggles_default_off():
    html = build._hero_legend_html(has_nasdaq=False, what_if=True)
    assert 'data-series="whatif"' in html and 'data-series="blended"' in html
    assert html.count('aria-pressed="false"') >= 2
    assert "leg-wi" in html


def test_hero_legend_omits_what_if_when_no_pool():
    html = build._hero_legend_html(has_nasdaq=False, what_if=False)
    assert "whatif" not in html


def test_chip_row_and_subtitle_in_page():
    src = inspect.getsource(build.render_html)
    assert 'id="whatif-chips"' in src
    assert "Hindsight view" in src and "Not a forecast" in src


def test_css_gates_what_if_legend_to_short_mode():
    css = build._read_asset("dashboard.css")
    assert ".leg-wi{display:none" in css.replace(" ", "")
    assert ".range-short" in css


def test_watchlist_cards_carry_what_if_checkbox():
    payload = {"AAA": {"name": "Aco", "currency": "USD", "ccy_symbol": "$",
                       "latest": 100.0, "native_latest": 100.0, "total": -5.0,
                       "prices": [100, 98, 95], "note": "", "wl_kind": "manual"}}
    html = build.render_watchlist(payload, pd.DataFrame())
    assert 'class="wl-wi-pick"' in html
    assert 'data-wi-ticker="AAA"' in html
    assert 'type="checkbox"' in html


# --- Task 6: client JS (source guards; behaviour browser-verified separately) --

def test_js_setup_what_if_present():
    js = build._read_asset("dashboard.js")
    assert "function setupWhatIf" in js
    assert "whatIfSelection" in js and "whatIfOn" in js and "whatIfBlendedOn" in js
    assert "computeWhatIfSeries" in js and "computeBlendedSeries" in js
    assert "renderWhatIfChips" in js


def test_js_blend_uses_n_open_weighting():
    js = build._read_asset("dashboard.js")
    assert "n_open" in js  # blended weighting reads WHATIF.n_open


def test_js_what_if_lines_gated_to_short_mode():
    js = build._read_asset("dashboard.js")
    # the lines are only computed inside the short-mode branch (heroRange != all)
    assert "wiSeries" in js and "blSeries" in js
    assert "data-wi-remove" in js and "data-wi-add" in js


def test_js_checkbox_click_does_not_open_card():
    # the tick must stopPropagation so the enclosing .wl-card click (openModal)
    # doesn't also fire.
    js = build._read_asset("dashboard.js")
    assert "wl-wi-pick" in js and "stopPropagation" in js


def test_blend_math_reference():
    # 3-day fixture: basket cum [0,1,2]%, custom cum [0,10,20]%, N=9, k=1
    def daily(cum):
        g = [1 + c / 100 for c in cum]
        return [0.0] + [g[i] / g[i - 1] - 1 for i in range(1, len(g))]
    N, k = 9, 1
    rb, rc = daily([0, 1, 2]), daily([0, 10, 20])
    comb = [(N * a + k * b) / (N + k) for a, b in zip(rb, rc)]
    g, cum = 1.0, []
    for r in comb:
        g *= 1 + r
        cum.append(round((g - 1) * 100, 4))
    assert cum[0] == 0.0
    assert 1.85 < cum[1] < 1.95          # ~ (9*1% + 1*10%)/10 = 1.9%
    assert 3.7 < cum[2] < 3.9
