import pandas as pd
import build
# The canonical privacy guard lives in sanity_check.py so CI can run it without
# pytest (CI's requirements.txt has no pytest). Tests reuse the same function.
from sanity_check import assert_snapshot_is_clean


def _real_txns():
    return pd.DataFrame([
        ("AAPL", "2024-10-21", "BUY", 15.0),
        ("NVDA", "2025-01-02", "BUY", 30.0),
        ("NVDA", "2025-04-22", "SELL", 10.0),   # partial -> omitted
        ("LLY",  "2024-10-21", "BUY", 4.0),
        ("LLY",  "2025-03-10", "SELL", 4.0),    # full exit
    ], columns=["ticker", "date", "action", "shares"])


def test_exported_snapshot_passes_privacy_guard():
    snap = build.export_basket_snapshot(_real_txns())
    assert_snapshot_is_clean(snap)


def test_writer_roundtrips_and_stays_clean(tmp_path, monkeypatch):
    target = tmp_path / "basket.snapshot.csv"
    monkeypatch.setattr(build, "BASKET_SNAPSHOT_CSV", target)
    build.write_basket_snapshot(_real_txns())
    assert target.exists()
    assert_snapshot_is_clean(pd.read_csv(target))


def test_committed_snapshot_is_clean_if_present():
    """If a real basket.snapshot.csv has been committed, it must be leak-free.
    Skips cleanly in a fresh checkout that has none yet."""
    if not build.BASKET_SNAPSHOT_CSV.exists():
        import pytest
        pytest.skip("no committed snapshot yet")
    assert_snapshot_is_clean(pd.read_csv(build.BASKET_SNAPSHOT_CSV))
