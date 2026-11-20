"""v3.7 AI build-out module: cohort capex/cash-flow/debt + the basket's AI-cycle exposure."""
import re
from pathlib import Path

import pandas as pd
import pytest

import build

ROOT = Path(__file__).resolve().parent.parent


# ---------- helpers ----------

QE = ["2026-09-30", "2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30", "2025-06-30"]


def _hist(spec):
    """spec: {ticker: [(quarter_end, capex, ocf, debt), ...]} -> history frame ($)."""
    rows = []
    for t, qs in spec.items():
        for qe, c, o, d in qs:
            rows.append({"ticker": t, "quarter_end": pd.Timestamp(qe), "capex": c, "ocf": o, "debt": d})
    return pd.DataFrame(rows)


def _uniform(tickers, capex, ocf, debt, n=6, ends=QE):
    return _hist({t: [(ends[i], capex[i], ocf[i], debt[i]) for i in range(n)] for t in tickers})


# ---------- history merge ----------

def test_merge_upserts_fresh_over_history():
    old = _hist({"MSFT": [("2026-06-30", 10e9, 20e9, 5e9), ("2026-03-31", 9e9, 19e9, 5e9)]})
    fresh = _hist({"MSFT": [("2026-09-30", 12e9, 21e9, 6e9), ("2026-06-30", 11e9, 20e9, 5e9)]})
    m = build.merge_ai_capex_history(old, fresh)
    assert len(m) == 3                                    # one new quarter, one replaced
    row = m[(m.ticker == "MSFT") & (m.quarter_end == pd.Timestamp("2026-06-30"))]
    assert float(row.capex.iloc[0]) == 11e9              # restated figure wins
    assert list(m.columns) == build.AI_HISTORY_COLS


def test_merge_handles_empty_sides():
    fresh = _hist({"META": [("2026-09-30", 1e9, 2e9, 3e9)]})
    assert len(build.merge_ai_capex_history(None, fresh)) == 1
    assert len(build.merge_ai_capex_history(fresh, pd.DataFrame())) == 1
    assert build.merge_ai_capex_history(None, None).empty


def test_history_roundtrip(tmp_path):
    p = tmp_path / "h.parquet"
    df = _hist({"ORCL": [("2026-08-31", 1e9, 2e9, 3e9)]})
    build.save_ai_capex_history(df, p)
    back = build.load_ai_capex_history(p)
    assert len(back) == 1 and back.ticker.iloc[0] == "ORCL"
    assert build.load_ai_capex_history(tmp_path / "missing.parquet").empty


# ---------- fetch (no network: yfinance mocked) ----------

class _FakeTicker:
    def __init__(self, t):
        cols = [pd.Timestamp(q) for q in QE[:5]]
        self.quarterly_cashflow = pd.DataFrame(
            [[-10e9] * 5, [20e9] * 5], index=["Capital Expenditure", "Operating Cash Flow"], columns=cols)
        self.quarterly_balance_sheet = pd.DataFrame([[50e9] * 5], index=["Total Debt"], columns=cols)


def test_fetch_flips_capex_sign_and_skips_broken_ticker(monkeypatch, capsys):
    def factory(t):
        if t == "BAD":
            raise RuntimeError("boom")
        return _FakeTicker(t)
    monkeypatch.setattr(build.yf, "Ticker", factory)
    df = build.fetch_ai_capex_statements(["MSFT", "BAD"])
    assert set(df.ticker) == {"MSFT"} and len(df) == 5
    assert (df.capex > 0).all()                          # capex reported negative -> positive spend
    assert (df.debt == 50e9).all()
    err = capsys.readouterr().err
    assert "BAD" in err and err.isascii()                # warn, ASCII only (Windows cp1252)


# ---------- cohort maths ----------

def test_cohort_quarters_ratios_and_totals():
    capex = [12e9, 11e9, 10e9, 9e9, 8e9, 7e9]            # newest first
    ocf = [10e9] * 6
    debt = [30e9, 28e9, 26e9, 24e9, 15e9, 14e9]
    c = build.compute_ai_cohort(_uniform(["MSFT", "GOOGL", "AMZN"], capex, ocf, debt))
    assert c["n_companies"] == 3 and c["missing"] == ["META", "ORCL", "CRWV"]
    q = c["quarters"]
    assert len(q) == 6 and q[-1]["label"] == "Q3 '26" and q[0]["label"] == "Q2 '25"   # oldest -> newest
    assert q[-1]["ratio"] == 120 and q[0]["ratio"] == 70
    assert q[-1]["capex"] == pytest.approx(36.0) and q[-1]["debt"] == pytest.approx(90.0)
    t = c["totals"]
    assert t["capex_ttm"] == pytest.approx(126.0)        # 3 x (12+11+10+9)
    assert t["ratio_ttm"] == 105                          # 42/40
    assert t["debt"] == pytest.approx(90.0) and t["debt_1y"] == pytest.approx(45.0)
    assert c["asof"] == "2026-09-30"


def test_cohort_needs_three_companies_with_four_quarters():
    capex, ocf, debt = [1e9] * 6, [2e9] * 6, [3e9] * 6
    two = _uniform(["MSFT", "GOOGL"], capex, ocf, debt)
    assert build.compute_ai_cohort(two) is None
    short = pd.concat([two, _uniform(["META"], capex, ocf, debt, n=3)])
    assert build.compute_ai_cohort(short) is None        # META has < 4 quarters -> not counted
    assert build.compute_ai_cohort(pd.DataFrame()) is None
    assert build.compute_ai_cohort(None) is None


def test_cohort_aligns_by_rank_not_calendar():
    # ORCL's fiscal quarters end a month earlier; they still line up by rank.
    ends_orcl = ["2026-08-31", "2026-05-31", "2026-02-28", "2025-11-30", "2025-08-31"]
    h = pd.concat([
        _uniform(["MSFT", "GOOGL"], [1e9] * 5, [2e9] * 5, [3e9] * 5, n=5),
        _uniform(["ORCL"], [1e9] * 5, [2e9] * 5, [3e9] * 5, n=5, ends=ends_orcl),
    ])
    c = build.compute_ai_cohort(h)
    assert len(c["quarters"]) == 5
    assert c["quarters"][-1]["label"] == "Q3 '26"        # majority label at that rank
    assert c["quarters"][-1]["capex"] == pytest.approx(3.0)


def test_cohort_debt_total_none_when_a_company_lacks_it():
    h = _uniform(["MSFT", "GOOGL", "AMZN"], [1e9] * 6, [2e9] * 6, [3e9] * 6)
    h.loc[(h.ticker == "AMZN") & (h.quarter_end == pd.Timestamp(QE[4])), "debt"] = float("nan")
    c = build.compute_ai_cohort(h)
    assert c["totals"]["debt_1y"] is None                 # never a partial sum passed off as total
    assert c["totals"]["debt"] == pytest.approx(9.0)


def test_cohort_caps_quarters_shown():
    h = _uniform(["MSFT", "GOOGL", "AMZN"], [1e9] * 6, [2e9] * 6, [3e9] * 6)
    assert len(build.compute_ai_cohort(h, n_quarters=4)["quarters"]) == 4


# ---------- exposure classification ----------

def _d(status="open", industry="", total=10.0, contribution=1.0, m1=0.0, name=None):
    return {"status": status, "industry": industry, "total": total, "contribution": contribution, "m1": m1, "name": name}


def test_classification_groups_precedence_and_exclusions():
    data = {
        "AMZN": _d(industry="Internet Retail"),                     # spender by cohort, not industry
        "NVDA": _d(industry="Semiconductors"),
        "ASML": _d(industry="Semiconductor Equipment & Materials"),
        "DELL": _d(industry="Computer Hardware"),
        "VST": _d(industry="Utilities - Independent Power Producers"),
        "QBTS": _d(industry="Computer Hardware"),                   # excluded despite industry
        "LMT": _d(industry="Aerospace & Defense"),                  # not AI
        "OLD": _d(status="closed", industry="Semiconductors"),      # closed -> ignored
        "WAT": _d(status="watch", industry="Semiconductors"),       # watchlist -> ignored
    }
    e = build.classify_ai_exposure(data)
    g = {x["key"]: x["tickers"] for x in e["groups"]}
    assert g["spend"] == ["AMZN"] and sorted(g["chips"]) == ["ASML", "NVDA"]
    assert g["hw"] == ["DELL"] and g["power"] == ["VST"]
    assert e["n_ai"] == 5 and e["n_open"] == 7
    assert e["excluded"] == [("QBTS", "quantum computing")]


def test_classification_shares_and_month_comparison():
    data = {
        "NVDA": _d(industry="Semiconductors", contribution=6.0, m1=-10.0, total=100.0),
        "MU": _d(industry="Semiconductors", contribution=2.0, m1=-20.0, total=300.0),
        "LMT": _d(industry="Aerospace & Defense", contribution=2.0, m1=4.0),
        "XOM": _d(industry="Oil & Gas Integrated", contribution=0.0, m1=None),
    }
    e = build.classify_ai_exposure(data)
    assert e["contrib_share"] == pytest.approx(80.0)    # 8 of 10 pts
    assert e["share_positions"] == pytest.approx(50.0)  # 2 of 4 names
    assert e["m1_ai"] == pytest.approx(-15.0) and e["m1_rest"] == pytest.approx(4.0)   # None skipped
    chips = next(x for x in e["groups"] if x["key"] == "chips")
    assert chips["tickers"] == ["MU", "NVDA"]            # sorted by return, best first
    assert chips["contrib_share"] == pytest.approx(80.0)


def test_contribution_share_none_when_sign_makes_percent_meaningless():
    data = {"NVDA": _d(industry="Semiconductors", contribution=5.0),
            "LMT": _d(industry="Aerospace & Defense", contribution=-8.0)}
    e = build.classify_ai_exposure(data)                 # open total negative -> no "% of return"
    assert e["contrib_share"] is None
    assert e["contrib_ai"] == pytest.approx(5.0) and e["contrib_rest"] == pytest.approx(-8.0)


def test_classification_empty_basket():
    e = build.classify_ai_exposure({})
    assert e["n_ai"] == 0 and e["n_open"] == 0 and e["contrib_share"] is None and e["m1_ai"] is None


# ---------- rendering ----------

def _cohort():
    return build.compute_ai_cohort(_uniform(["MSFT", "GOOGL", "AMZN", "META"],
                                            [12e9, 11e9, 10e9, 9e9, 8e9, 7e9], [10e9] * 6,
                                            [30e9, 28e9, 26e9, 24e9, 15e9, 14e9]))


def _exposure():
    return build.classify_ai_exposure({
        "AMZN": _d(industry="Internet Retail", contribution=1.0, name="Amazon"),
        "NVDA": _d(industry="Semiconductors", contribution=6.0, m1=-12.0, total=33.7, name="NVIDIA"),
        "LMT": _d(industry="Aerospace & Defense", contribution=3.0, m1=2.0),
    })


def test_render_full_module():
    html = build.render_ai_buildout(_cohort(), _exposure())
    assert 'class="aib-section"' in html and "AI build-out" in html
    assert "120%" in html and "debt-funded" in html      # latest ratio > 100 -> pill
    assert "more cash than their businesses make" in html
    assert 'data-ticker="NVDA"' in html and "ticker-clickable" in html   # chips open the stock modal
    assert 'id="aib-detail"' in html and "hidden" in html                 # detail collapsed by default
    assert "past month" in html.lower()
    assert "£" not in html and "&pound;" not in html                 # never the author's money


def test_render_held_cohort_rows_are_clickable_and_tagged():
    html = build.render_ai_buildout(_cohort(), _exposure())
    tbody = re.search(r'<tbody id="aib-cohort">(.*?)</tbody>', html, re.S).group(1)
    rows = {re.search(r'<td class="aib-tk">([A-Z]+)', r).group(1): r
            for r in re.findall(r'<tr[^>]*>.*?</tr>', tbody, re.S) if 'class="aib-tk"' in r}
    assert "ticker-clickable" in rows["AMZN"] and 'data-ticker="AMZN"' in rows["AMZN"]
    assert "you hold" in rows["AMZN"].lower()
    assert "data-ticker" not in rows["MSFT"]             # not held: plain row, no modal hook


def test_render_escapes_hostile_text():
    e = build.classify_ai_exposure({'<b>X': _d(industry="Semiconductors", name='<img src=x onerror=alert(1)>')})
    html = build.render_ai_buildout(_cohort(), e)
    assert "<img src=x" not in html and "<b>X" not in html
    assert "&lt;b&gt;X" in html


def test_render_points_wording_when_share_undefined():
    e = build.classify_ai_exposure({"NVDA": _d(industry="Semiconductors", contribution=5.0),
                                    "LMT": _d(industry="Aerospace & Defense", contribution=-8.0)})
    html = build.render_ai_buildout(_cohort(), e)
    assert "pts" in html and "combined return" not in html


def test_render_empty_state_without_cohort():
    html = build.render_ai_buildout(None, _exposure())
    assert 'class="aib-section"' in html
    assert "next live build" in html                     # honest, not a crash or a blank module
    assert "2 of your 3 open names" in html              # exposure still useful on its own


def test_render_neutral_headline_when_self_funded():
    c = build.compute_ai_cohort(_uniform(["MSFT", "GOOGL", "AMZN"], [5e9] * 6, [10e9] * 6, [1e9] * 6))
    html = build.render_ai_buildout(c, _exposure())
    assert "more cash than their businesses make" not in html and "debt-funded" not in html


# ---------- page integration ----------

def test_module_sits_between_news_and_market_expectations():
    src = (ROOT / "build.py").read_text(encoding="utf-8")
    block = src[src.index("_module_defs = ["):src.index("_modules = [(mid")]
    keys = re.findall(r'\("([a-z_]+)",', block)
    i = keys.index("ai_buildout")
    assert keys[i - 1] == "news" and keys[i + 1] == "predictions"
    assert '("ai_buildout", "AI build-out", ai_buildout_html)' in block


def test_render_html_accepts_history_kwarg():
    import inspect
    assert "ai_capex_history" in inspect.signature(build.render_html).parameters


def test_assets_wire_the_module():
    js = (ROOT / "assets" / "dashboard.js").read_text(encoding="utf-8")
    css = (ROOT / "assets" / "dashboard.css").read_text(encoding="utf-8")
    assert "setupAiBuildout" in js and "aib-detail" in js
    assert ".aib-section" in css and ".aib-tile" in css
