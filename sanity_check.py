"""Post-build gate run in CI before committing docs/index.html. Any failure
exits non-zero so the publish step is skipped and the last-good page stays live.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
SNAPSHOT = ROOT / "basket.snapshot.csv"
HTML = ROOT / "docs" / "index.html"
PAYLOAD = ROOT / "docs" / "data" / "payload.json"
LAST_PUBLISH_META = ROOT / "data" / "last_publish_meta.json"
POSITION_FLOOR = 10
BAND_FRAC = 0.7   # refuse to publish if positions drop below 70% of last published


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
    if not meta_path.exists():
        return None
    try:
        return int(json.loads(meta_path.read_text(encoding="utf-8"))["position_count"])
    except Exception:
        return None


def write_last_count(n: int, meta_path: Path = LAST_PUBLISH_META) -> None:
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(
        {"position_count": int(n),
         "date": datetime.now(timezone.utc).strftime("%Y-%m-%d")}), encoding="utf-8")


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


def main():
    if not SNAPSHOT.exists():
        _fail("basket.snapshot.csv missing — nothing to publish")
    df = pd.read_csv(SNAPSHOT)
    last = read_last_count()
    n = check_position_count(df, last_count=last)
    check_output_files(HTML, PAYLOAD)
    write_last_count(n)   # advance the baseline only on full success
    print("SANITY OK")


if __name__ == "__main__":
    main()
