# tests/test_doctor.py
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
