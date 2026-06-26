"""v2.9.2 cache-freshness (H3) + snapshot-staleness guard.

The universe cache must decide freshness from an EMBEDDED date, never file mtime —
CI's git checkout resets every file's mtime to "now", so an mtime TTL never expires
and the cache (value-screen + industry-outlook data) freezes forever under auto-publish.
"""
from datetime import datetime, timezone

import pandas as pd

import build


def _today():
    return pd.Timestamp(datetime.now(timezone.utc).date()).normalize()


# --- snapshot-staleness guard (Task A) ---

def test_snapshot_is_behind_log():
    assert build.snapshot_is_behind_log(pd.Timestamp("2026-05-13"), pd.Timestamp("2026-06-01")) is True
    assert build.snapshot_is_behind_log(pd.Timestamp("2026-05-13"), pd.Timestamp("2026-05-13")) is False
    assert build.snapshot_is_behind_log(pd.Timestamp("2026-06-01"), pd.Timestamp("2026-05-13")) is False
    assert build.snapshot_is_behind_log(None, pd.Timestamp("2026-06-01")) is False
    assert build.snapshot_is_behind_log(pd.Timestamp("2026-05-13"), None) is False


def test_snapshot_latest_date(tmp_path):
    p = tmp_path / "snap.csv"
    pd.DataFrame({"ticker": ["A", "B"], "date": ["2024-01-02", "2026-05-13"],
                  "action": ["BUY", "SELL"], "shares": [1, 1]}).to_csv(p, index=False)
    assert build.snapshot_latest_date(p) == pd.Timestamp("2026-05-13")
    assert build.snapshot_latest_date(tmp_path / "absent.csv") is None


# --- universe cache freshness from embedded date (Task B / H3) ---

def _write_cache(path, days_old):
    d = (_today() - pd.Timedelta(days=days_old)).strftime("%Y-%m-%d")
    pd.DataFrame({"ret_12m": [1.0], "cache_date": [d]}).to_parquet(path)


def test_universe_cache_date_roundtrip(tmp_path):
    p = tmp_path / "uni.parquet"
    _write_cache(p, days_old=0)
    assert build._universe_cache_date(p) == _today()
    assert build._universe_cache_age_days(p) == 0


def test_universe_cache_fresh_via_embedded_date(tmp_path, monkeypatch):
    p = tmp_path / "uni.parquet"
    _write_cache(p, days_old=5)
    monkeypatch.setattr(build, "UNIVERSE_CACHE", p)
    assert build._universe_cache_is_fresh(ttl_days=30) is True


def test_universe_cache_stale_via_embedded_date(tmp_path, monkeypatch):
    p = tmp_path / "uni.parquet"
    _write_cache(p, days_old=40)
    monkeypatch.setattr(build, "UNIVERSE_CACHE", p)
    assert build._universe_cache_is_fresh(ttl_days=30) is False


def test_freshness_ignores_mtime_regression(tmp_path, monkeypatch):
    # THE CI BUG: a 40-day-old cache whose mtime was just reset (git checkout)
    # must STILL be stale. Pre-fix, p.touch() would have made it "fresh forever".
    p = tmp_path / "uni.parquet"
    _write_cache(p, days_old=40)
    monkeypatch.setattr(build, "UNIVERSE_CACHE", p)
    p.touch()   # mtime = now
    assert build._universe_cache_is_fresh(ttl_days=30) is False


def test_universe_cache_no_date_column_is_stale(tmp_path, monkeypatch):
    # Old pre-cache_date parquet → treated as stale so the next build refetches
    # and rewrites it in the dated format (one-time migration).
    p = tmp_path / "uni.parquet"
    pd.DataFrame({"ret_12m": [1.0]}).to_parquet(p)
    monkeypatch.setattr(build, "UNIVERSE_CACHE", p)
    assert build._universe_cache_date(p) is None
    assert build._universe_cache_is_fresh(ttl_days=30) is False
