"""v2.6 Value screen — quality+value names near their 52-week low.

Six filters: #1 near-52-week-low is a REQUIRED gate; #2 cheap vs sector (P/E
below the sector median, a pure multiple); #3 positive FCF; #4 ROE > 10%;
#5 positive revenue growth; #6 debt/equity < 1.5. A name needs VALUE_MIN_PASS
(4) of 6 to appear. Universe-only, excludes held names. See value-screen-spec.md.
"""
import numpy as np
import pandas as pd

import build


def _uni(rows: list[dict]) -> pd.DataFrame:
    cols = ["sector", "name", "current_price", "range52w_pct", "pe", "pb",
            "roe", "rev_growth", "fcf", "debt_to_equity"]
    df = pd.DataFrame(rows).set_index("ticker")
    for c in cols:
        if c not in df.columns:
            df[c] = np.nan
    return df


# A Tech sector with four priced names -> median P/E = 25 (median of 10,20,30,40).
# F1/F2 are gate-failing fillers that still count toward the sector median.
def _universe():
    return _uni([
        {"ticker": "CHEAP", "sector": "Tech", "name": "Cheapco", "current_price": 50.0,
         "range52w_pct": 5, "pe": 10, "pb": 1.2, "roe": 0.20, "rev_growth": 0.05,
         "fcf": 1e9, "debt_to_equity": 50},          # 6/6
        {"ticker": "MID", "sector": "Tech", "name": "Midco", "current_price": 80.0,
         "range52w_pct": 10, "pe": 30, "pb": 2.0, "roe": 0.15, "rev_growth": 0.03,
         "fcf": 1e9, "debt_to_equity": 40},          # 5/6 (P/E 30 > median 25)
        {"ticker": "MISS", "sector": "Tech", "name": "Missco", "current_price": 20.0,
         "range52w_pct": 8, "pe": 12, "pb": 1.0, "roe": np.nan, "rev_growth": 0.04,
         "fcf": 1e9, "debt_to_equity": 30},          # 5/6 (ROE missing -> not passed)
        {"ticker": "NOLOW", "sector": "Tech", "name": "Highco", "current_price": 90.0,
         "range52w_pct": 70, "pe": 10, "pb": 1.0, "roe": 0.30, "rev_growth": 0.10,
         "fcf": 1e9, "debt_to_equity": 20},          # excluded: fails the gate
        {"ticker": "WEAK", "sector": "Tech", "name": "Weakco", "current_price": 5.0,
         "range52w_pct": 4, "pe": 10, "pb": 1.0, "roe": 0.02, "rev_growth": -0.01,
         "fcf": -1e9, "debt_to_equity": 200},        # 2/6 -> below floor, excluded
        {"ticker": "HELD", "sector": "Tech", "name": "Heldco", "current_price": 10.0,
         "range52w_pct": 3, "pe": 9, "pb": 1.0, "roe": 0.25, "rev_growth": 0.08,
         "fcf": 1e9, "debt_to_equity": 30},          # would be 6/6 but held -> excluded
        {"ticker": "F1", "sector": "Tech", "name": "Filler1", "current_price": 1.0,
         "range52w_pct": 90, "pe": 20, "fcf": np.nan},
        {"ticker": "F2", "sector": "Tech", "name": "Filler2", "current_price": 1.0,
         "range52w_pct": 90, "pe": 40, "fcf": np.nan},
        {"ticker": "MINE1", "sector": "Mining", "name": "Mineco", "current_price": 12.0,
         "range52w_pct": 6, "pe": 8, "pb": 0.8, "roe": 0.20, "rev_growth": 0.06,
         "fcf": 1e9, "debt_to_equity": 50},          # 5/6: sector has <3 priced -> #2 unavailable
    ])


def _by_ticker(rows):
    return {r["ticker"]: r for r in rows}


def test_value_screen_membership_and_exclusions():
    rows = build.build_value_screen(_universe(), log_tickers={"HELD"},
                                    bb_idea_tickers={"CHEAP"}, min_pass=4)
    by = _by_ticker(rows)
    assert set(by) == {"CHEAP", "MID", "MISS", "MINE1"}   # NOLOW(gate) WEAK(floor) HELD(owned) F1/F2(no gate) all out


def test_value_screen_default_floor_is_strict_all_six():
    # Production default (VALUE_MIN_PASS=6): only the 6/6 names survive.
    rows = build.build_value_screen(_universe(), log_tickers={"HELD"})
    assert [r["ticker"] for r in rows] == ["CHEAP"]       # only the perfect-6 name


def test_value_screen_scoring_and_sector_pe():
    rows = build.build_value_screen(_universe(), log_tickers={"HELD"}, min_pass=4)
    by = _by_ticker(rows)
    assert by["CHEAP"]["pass_count"] == 6 and by["CHEAP"]["passed"]["cheap"] is True
    assert by["MID"]["pass_count"] == 5 and by["MID"]["passed"]["cheap"] is False   # P/E 30 > median 25
    assert by["MISS"]["passed"]["roe"] is False                                     # missing ROE not passed
    assert by["MINE1"]["passed"]["cheap"] is False                                  # sector <3 peers -> no median


def test_value_screen_debt_to_equity_is_percent():
    # yfinance debtToEquity is a percentage (120 -> 1.2x). 50 -> 0.5x passes < 1.5;
    # WEAK's 200 -> 2.0x fails.
    rows = build.build_value_screen(_universe(), log_tickers={"HELD"}, min_pass=4)
    by = _by_ticker(rows)
    assert by["CHEAP"]["passed"]["de"] is True
    assert abs(by["CHEAP"]["debt_to_equity"] - 0.5) < 1e-9


def test_value_screen_sorted_by_pass_then_discount():
    rows = build.build_value_screen(_universe(), log_tickers={"HELD"}, min_pass=4)
    tickers = [r["ticker"] for r in rows]
    assert tickers[0] == "CHEAP"                       # 6/6 first
    # among the 5/6 names, deepest P/E discount to sector first; MINE1 (no median) last
    assert tickers.index("MID") < tickers.index("MINE1")


def test_value_screen_bb_idea_tag():
    rows = build.build_value_screen(_universe(), log_tickers={"HELD"},
                                    bb_idea_tickers={"CHEAP"}, min_pass=4)
    by = _by_ticker(rows)
    assert by["CHEAP"]["is_bb_idea"] is True
    assert by["MID"]["is_bb_idea"] is False


def test_value_screen_empty_universe():
    assert build.build_value_screen(pd.DataFrame(), log_tickers=set()) == []


# --- render -----------------------------------------------------------------

def _vrow(t, pc=6, bb=False):
    return {"ticker": t, "name": t + " Inc", "sector": "Tech", "price": 100.0,
            "pe": 10.0, "sector_median_pe": 20.0, "pe_discount": 0.5, "pb": 1.2,
            "roe": 0.18, "rev_growth": 0.05, "fcf_positive": True,
            "debt_to_equity": 0.4, "range52w_pct": 5.0, "pass_count": pc,
            "passed": {"near_low": True, "cheap": True, "fcf": True, "roe": True,
                       "rev": True, "de": pc == 6}, "is_bb_idea": bb}


def test_render_value_screen_basic():
    html = build.render_value_screen([_vrow("MRK")], "12 Jun 2026")
    assert "value-screen-section" in html
    assert "MRK" in html
    assert "$100" in html                      # native price next to ticker
    assert "6/6" in html                       # pass count
    assert "as of 12 Jun 2026" in html         # honest refresh date
    assert "passed" in html                    # subtitle count


def test_render_value_screen_bb_tag():
    html = build.render_value_screen([_vrow("NVDA", bb=True)], "12 Jun 2026")
    assert "BB" in html                         # cross-tool tag
    assert "vs-bb" in html                      # row highlight / tag class


def test_render_value_screen_pagination():
    many = [_vrow(f"T{i}") for i in range(12)]
    html = build.render_value_screen(many, "12 Jun 2026")
    assert "data-vs-page" in html and "vs-arrow" in html
    few = [_vrow(f"T{i}") for i in range(5)]
    assert "vs-arrow" not in build.render_value_screen(few, "12 Jun 2026")


def test_render_value_screen_caps_at_max_rows():
    rows = [_vrow(f"T{i}") for i in range(25)]
    html = build.render_value_screen(rows, "12 Jun 2026")
    assert html.count('class="vs-row') == build.VALUE_MAX_ROWS   # only top 20 rendered
    assert f"25 names passed" in html                            # subtitle shows true total
    assert "showing top" in html


def test_render_value_screen_empty():
    assert "vs-empty" in build.render_value_screen([], "12 Jun 2026")


def test_render_value_screen_header_tooltips():
    html = build.render_value_screen([_vrow("MRK")], "12 Jun 2026")
    assert "Return on equity" in html        # ROE column hover explanation
    assert "sector median" in html           # P/E column hover explanation
    assert html.count("title=") >= 6         # a tooltip per criterion


def test_render_value_screen_shades_by_strength():
    import re
    a, b = _vrow("LOWROE"), _vrow("HIROE")
    a["roe"], b["roe"] = 0.11, 0.35
    html = build.render_value_screen([a, b], "12 Jun 2026")
    alphas = re.findall(r'vs-c-roe"[^>]*rgba\([^)]*,\s*([0-9.]+)\)', html)
    assert len(alphas) == 2
    assert float(alphas[1]) > float(alphas[0])   # higher ROE -> deeper shade
