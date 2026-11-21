"""v3.9: the outlook module groups the universe by SECTOR and weights each
company by its market cap at the START of the 12-month window.

Why: weighting by today's cap on 3-7-name GICS sub-industries let one moonshot
(whose cap is now huge *because* it ran) dominate its group -- the page showed
"Computer Hardware +1606%". Start-of-period weighting is what an index fund
bought a year ago actually experienced; sector groups are broad enough that no
single name decides the number. No hard % cap (a booming sector should read high).
"""
from pathlib import Path

import numpy as np
import pandas as pd

import build

ROOT = Path(build.__file__).parent
JS = (ROOT / "assets" / "dashboard.js").read_text(encoding="utf-8")


def _uni(rows):
    df = pd.DataFrame(rows).set_index("ticker")
    for col, default in (("upside", np.nan), ("recommendation", ""), ("num_analysts", 0),
                         ("cap_tier", ""), ("current_price", np.nan), ("sector", "Tech"),
                         ("industry", "Hardware")):
        if col not in df.columns:
            df[col] = default
    return df


def test_moonshot_does_not_dominate_with_start_of_period_weights():
    # MOON went +900% (cap 10bn -> 100bn); BIG went +10% (100bn -> 110bn).
    # Today's caps would weight them ~1:1.1 -> ~434%. Start caps are 10:100.
    groups = build.build_industry_outlook(_uni([
        {"ticker": "MOON", "ret_12m": 900.0, "market_cap": 100e9},
        {"ticker": "BIG", "ret_12m": 10.0, "market_cap": 110e9},
        {"ticker": "MID", "ret_12m": 10.0, "market_cap": 110e9},
    ]), min_holdings=3)
    expected = (10e9 * 900 + 100e9 * 10 + 100e9 * 10) / (10e9 + 100e9 + 100e9)
    assert abs(groups[0]["avg_ret_12m"] - expected) < 0.01      # ~52.4%
    assert groups[0]["avg_ret_12m"] < 60


def test_total_loss_names_drop_out_of_the_weighting():
    # ret <= -100% has no meaningful start cap (division by ~0): exclude it
    # from the weights rather than blowing up the group number.
    groups = build.build_industry_outlook(_uni([
        {"ticker": "DEAD", "ret_12m": -100.0, "market_cap": 1e6},
        {"ticker": "A", "ret_12m": 20.0, "market_cap": 120e9},
        {"ticker": "B", "ret_12m": 20.0, "market_cap": 120e9},
    ]), min_holdings=3)
    assert abs(groups[0]["avg_ret_12m"] - 20.0) < 0.01


def test_groups_by_sector_when_asked():
    rows = [{"ticker": f"T{i}", "ret_12m": 10.0 + i, "market_cap": 1e9,
             "sector": "Energy", "industry": f"Ind{i}"} for i in range(4)]
    groups = build.build_industry_outlook(_uni(rows), min_holdings=3, group_by="sector")
    assert [g["industry"] for g in groups] == ["Energy"]   # label kept under "industry"
    assert groups[0]["group_by"] == "sector"
    assert groups[0]["n_holdings"] == 4


def test_render_names_it_sector_outlook_and_marks_cards():
    rows = [{"ticker": f"T{i}", "ret_12m": 10.0, "market_cap": 1e9,
             "sector": "Energy"} for i in range(3)]
    groups = build.build_industry_outlook(_uni(rows), min_holdings=3, group_by="sector")
    html = build.render_industry_outlook(groups, universe_size=3)
    assert "Sector outlook" in html and "Industry outlook" not in html
    assert 'data-group="sector"' in html
    assert "start-of-period" in html.lower() or "start of the period" in html.lower()


def test_aux_payload_lists_universe_names_by_sector_excluding_held():
    uni = _uni([
        {"ticker": "HELD", "ret_12m": 5.0, "market_cap": 1e9, "sector": "Energy"},
        {"ticker": "NEW1", "ret_12m": 9.0, "market_cap": 1e9, "sector": "Energy"},
        {"ticker": "NEW2", "ret_12m": 3.0, "market_cap": 1e9, "sector": "Energy"},
    ])
    meta = pd.DataFrame({"name": ["New One", "New Two", "Held Co"]},
                        index=["NEW1", "NEW2", "HELD"])
    aux = build.build_aux_payload(pd.DataFrame(), pd.DataFrame(), meta, uni,
                                  diversification_data=None, log_tickers={"HELD"})
    names = [e["ticker"] for e in aux["universe_sectors"]["Energy"]]
    assert names == ["NEW1", "NEW2"]                     # held excluded, best first


def test_page_wiring_uses_sector_label_and_lookup():
    src = Path(build.__file__).read_text(encoding="utf-8")
    assert '("outlook", "Sector outlook", industry_html)' in src   # module key unchanged
    assert 'group_by="sector"' in src
    assert "universe_sectors" in JS and "dataset.group" in JS
    assert '"module_label": "Sector outlook"' in src
