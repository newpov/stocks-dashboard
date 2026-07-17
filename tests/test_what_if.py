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
