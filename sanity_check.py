"""Post-build gate run in CI before committing docs/index.html. Any failure
exits non-zero so the publish step is skipped and the last-good page stays live.
"""
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
SNAPSHOT = ROOT / "basket.snapshot.csv"
HTML = ROOT / "docs" / "index.html"
PAYLOAD = ROOT / "docs" / "data" / "payload.json"
POSITION_FLOOR = 10


def _fail(msg: str):
    print(f"SANITY FAIL: {msg}")
    raise SystemExit(1)


def check_position_count(snapshot_df: pd.DataFrame, floor: int = POSITION_FLOOR):
    n = snapshot_df["ticker"].nunique()
    if n < floor:
        _fail(f"only {n} positions in snapshot (floor {floor})")
    print(f"  positions: {n} (>= {floor})")


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
    check_position_count(pd.read_csv(SNAPSHOT))
    check_output_files(HTML, PAYLOAD)
    print("SANITY OK")


if __name__ == "__main__":
    main()
