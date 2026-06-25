import pandas as pd
import build


def _txns(rows):
    return pd.DataFrame(rows, columns=["ticker", "date", "action", "shares"])


def test_single_buy_becomes_one_unit():
    df = _txns([("AAA", "2025-01-02", "BUY", 15.0)])
    snap = build.export_basket_snapshot(df)
    assert list(snap.columns) == ["ticker", "date", "action", "shares"]
    assert snap.iloc[0].tolist() == ["AAA", "2025-01-02", "BUY", 1]


def test_multiple_buys_each_one_unit():
    df = _txns([
        ("AMD", "2025-01-15", "BUY", 12.0),
        ("AMD", "2025-07-08", "BUY", 8.0),
    ])
    snap = build.export_basket_snapshot(df)
    assert (snap["action"] == "BUY").all()
    assert snap["shares"].tolist() == [1, 1]
    assert snap["date"].tolist() == ["2025-01-15", "2025-07-08"]


def test_full_exit_sell_closes_cycle_with_unit_count():
    df = _txns([
        ("LLY", "2024-10-21", "BUY", 4.0),
        ("LLY", "2025-03-10", "SELL", 4.0),
    ])
    snap = build.export_basket_snapshot(df)
    assert snap["action"].tolist() == ["BUY", "SELL"]
    assert snap["shares"].tolist() == [1, 1]


def test_full_exit_after_two_buys_sell_unit_count_is_two():
    df = _txns([
        ("X", "2025-01-02", "BUY", 5.0),
        ("X", "2025-02-02", "BUY", 7.0),
        ("X", "2025-03-02", "SELL", 12.0),
    ])
    snap = build.export_basket_snapshot(df)
    assert snap["shares"].tolist() == [1, 1, 2]


def test_partial_trim_is_omitted():
    df = _txns([
        ("NVDA", "2025-01-02", "BUY", 30.0),
        ("NVDA", "2025-04-22", "SELL", 10.0),
    ])
    snap = build.export_basket_snapshot(df)
    assert snap["action"].tolist() == ["BUY"]
    assert snap["shares"].tolist() == [1]


def test_rebuy_after_full_exit_two_cycles():
    df = _txns([
        ("CSCO", "2024-01-02", "BUY", 10.0),
        ("CSCO", "2024-06-02", "SELL", 10.0),
        ("CSCO", "2026-05-13", "BUY", 10.0),
    ])
    snap = build.export_basket_snapshot(df)
    assert snap["action"].tolist() == ["BUY", "SELL", "BUY"]
    assert snap["shares"].tolist() == [1, 1, 1]


def test_orphan_sell_emits_nothing():
    # a SELL with no preceding BUY must not produce a bogus SELL 0 row
    df = _txns([("ZZZ", "2025-01-02", "SELL", 5.0)])
    snap = build.export_basket_snapshot(df)
    assert snap.empty
    assert snap["shares"].dtype.kind == "i"   # int dtype even when empty


def test_no_zero_share_sell_rows_ever():
    df = _txns([
        ("NVDA", "2025-01-02", "BUY", 30.0),
        ("NVDA", "2025-04-22", "SELL", 10.0),   # partial -> omitted
        ("LLY",  "2024-10-21", "BUY", 4.0),
        ("LLY",  "2025-03-10", "SELL", 4.0),    # full exit
    ])
    snap = build.export_basket_snapshot(df)
    sells = snap[snap["action"] == "SELL"]["shares"]
    assert (sells >= 1).all()


def test_snapshot_loads_back_into_positions(tmp_path, monkeypatch):
    import numpy as np
    monkeypatch.setattr(build, "BASKET_SNAPSHOT_CSV", tmp_path / "basket.snapshot.csv")
    real = _txns([
        ("X", "2025-01-02", "BUY", 5.0),
        ("X", "2025-02-03", "BUY", 7.0),    # scaled in
    ])
    build.write_basket_snapshot(real)
    loaded = build.load_transactions_from_snapshot()
    assert set(loaded["action"].unique()) == {"BUY"}
    assert (loaded["shares"] == 1.0).all()       # equal-weight units survive load
    assert str(loaded["date"].dtype).startswith("datetime")

    dates = pd.bdate_range("2025-01-02", periods=40)
    px = pd.DataFrame({"X": np.linspace(100.0, 140.0, 40)}, index=dates)
    pos = build.build_positions(loaded, px)
    # simple-average baseline of the two buy-date closes (NOT qty-weighted)
    b0 = float(px["X"].asof(pd.Timestamp("2025-01-02")))
    b1 = float(px["X"].asof(pd.Timestamp("2025-02-03")))
    assert abs(pos.loc["X"]["baseline"] - (b0 + b1) / 2.0) < 1e-6
