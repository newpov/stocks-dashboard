import pandas as pd
import build


def _real_txns():
    return pd.DataFrame([
        ("AAPL", "2024-10-21", "BUY", 15.0),
        ("NVDA", "2025-01-02", "BUY", 30.0),
        ("NVDA", "2025-04-22", "SELL", 10.0),   # partial -> omitted
        ("LLY",  "2024-10-21", "BUY", 4.0),
        ("LLY",  "2025-03-10", "SELL", 4.0),    # full exit
    ], columns=["ticker", "date", "action", "shares"])


def assert_snapshot_is_clean(snap: pd.DataFrame):
    """Reusable leakage guard — the single source of truth for 'safe to publish'."""
    assert list(snap.columns) == ["ticker", "date", "action", "shares"]
    assert set(snap["action"].unique()) <= {"BUY", "SELL"}
    buys = snap[snap["action"] == "BUY"]["shares"]
    sells = snap[snap["action"] == "SELL"]["shares"]
    assert (buys == 1).all(), "every BUY must be exactly 1 unit (no quantity leak)"
    assert (sells >= 1).all(), "SELL units must be positive integers"
    assert (sells == sells.round()).all(), "SELL units must be whole (no fractional ratios)"
    parsed = pd.to_datetime(snap["date"], format="%Y-%m-%d", errors="raise")
    assert (parsed.dt.normalize() == parsed).all()


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
