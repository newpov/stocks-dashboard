"""Post-build gate run in CI before committing docs/index.html. Any failure
exits non-zero so the publish step is skipped and the last-good page stays live.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
SNAPSHOT = ROOT / "basket.snapshot.csv"
HTML = ROOT / "docs" / "index.html"
PAYLOAD = ROOT / "docs" / "data" / "payload.json"
DEMO_HTML = ROOT / "demo.html"
LAST_PUBLISH_META = ROOT / "data" / "last_publish_meta.json"
POSITION_FLOOR = 10
BAND_FRAC = 0.7   # refuse to publish if positions drop below 70% of last published
DEMO_MIN_KB = 400  # demo.html is ~850 KB; below this it's a degraded build


def _fail(msg: str):
    print(f"SANITY FAIL: {msg}")
    raise SystemExit(1)


def check_position_count(snapshot_df: pd.DataFrame, floor: int = POSITION_FLOOR,
                         last_count: "int | None" = None, band_frac: float = BAND_FRAC):
    n = snapshot_df["ticker"].nunique()
    if n < floor:
        _fail(f"only {n} positions in snapshot (floor {floor})")
    if last_count is not None and last_count > 0 and n < band_frac * last_count:
        _fail(f"positions {n} dropped below {band_frac:.0%} of last published "
              f"{last_count} — refusing to publish a likely-truncated basket")
    print(f"  positions: {n} (floor {floor}, last published {last_count}, band {band_frac:.0%})")
    return n


def read_last_count(meta_path: Path = LAST_PUBLISH_META) -> "int | None":
    # Missing file is the expected first-run case (band check simply disabled).
    # A PRESENT-but-corrupt file is different: surface it loudly so a poisoned
    # baseline doesn't silently downgrade the gate to floor-only.
    if not meta_path.exists():
        return None
    try:
        return int(json.loads(meta_path.read_text(encoding="utf-8"))["position_count"])
    except Exception as e:
        print(f"  WARN: last-publish meta {meta_path.name} unreadable ({e}); "
              f"band check disabled this run", file=sys.stderr)
        return None


def write_last_count(n: int, meta_path: Path = LAST_PUBLISH_META) -> None:
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(
        {"position_count": int(n),
         "date": datetime.now(timezone.utc).strftime("%Y-%m-%d")}), encoding="utf-8")


def assert_snapshot_is_clean(snap: pd.DataFrame):
    """Privacy guard — the single source of truth for 'safe to publish'. Raises
    AssertionError on any leak. Imported by test_snapshot_privacy.py and run in
    CI via main() below (so the gate needs no pytest)."""
    assert list(snap.columns) == ["ticker", "date", "action", "shares"]
    assert snap.notna().all().all(), "snapshot has NaN cells (incomplete export)"
    assert set(snap["action"].unique()) <= {"BUY", "SELL"}
    buys = snap[snap["action"] == "BUY"]["shares"]
    sells = snap[snap["action"] == "SELL"]["shares"]
    assert (buys == 1).all(), "every BUY must be exactly 1 unit (no quantity leak)"
    assert (sells >= 1).all(), "SELL units must be positive integers"
    assert (sells == sells.round()).all(), "SELL units must be whole (no fractional ratios)"
    # Single source of truth: don't assume the writer's int cast — every share
    # value must be a whole number (a fractional value would be a quantity leak).
    assert snap["shares"].apply(lambda x: float(x).is_integer()).all(), \
        "all shares must be whole numbers (no fractional quantity leak)"
    parsed = pd.to_datetime(snap["date"], format="%Y-%m-%d", errors="raise")
    assert (parsed.dt.normalize() == parsed).all()


def check_output_files(html: Path, payload: Path, min_kb: int = 500):
    if not html.exists():
        _fail(f"missing {html}")
    kb = html.stat().st_size / 1024
    if kb < min_kb:
        _fail(f"{html.name} is {kb:.0f} KB (< {min_kb} KB) — build likely degraded")
    if not payload.exists():
        _fail(f"missing {payload}")
    try:
        json.loads(payload.read_text(encoding="utf-8"))
    except Exception as e:
        _fail(f"{payload.name} is not valid JSON: {e}")
    print(f"  outputs: {html.name} {kb:.0f} KB, {payload.name} valid JSON")


def check_demo_output(demo: Path, min_kb: int = DEMO_MIN_KB):
    """The CI workflow also rebuilds the self-contained demo.html and publishes it
    unconditionally. Gate it on a minimal size so a degraded demo build can't ship
    silently (it has no separate payload to JSON-validate; it's self-contained)."""
    if not demo.exists():
        _fail(f"missing {demo}")
    kb = demo.stat().st_size / 1024
    if kb < min_kb:
        _fail(f"{demo.name} is {kb:.0f} KB (< {min_kb} KB) — demo build likely degraded")
    print(f"  demo: {demo.name} {kb:.0f} KB")


def main():
    if not SNAPSHOT.exists():
        _fail("basket.snapshot.csv missing — nothing to publish")
    df = pd.read_csv(SNAPSHOT)
    try:
        assert_snapshot_is_clean(df)
    except AssertionError as e:
        _fail(f"snapshot privacy guard: {e}")
    print("  privacy guard: snapshot is clean")
    last = read_last_count()
    # Escape hatch: when the author intentionally makes a large real reduction,
    # set SANITY_SKIP_BAND=1 for one run so the position-count band doesn't block
    # a legitimate publish. The floor still applies.
    band = 0.0 if os.environ.get("SANITY_SKIP_BAND") else BAND_FRAC
    if band == 0.0 and last is not None:
        print("  band check skipped (SANITY_SKIP_BAND set)")
    n = check_position_count(df, last_count=last, band_frac=band)
    check_output_files(HTML, PAYLOAD)
    check_demo_output(DEMO_HTML)
    write_last_count(n)   # advance the baseline only on full success
    print("SANITY OK")


if __name__ == "__main__":
    main()
