"""One-off gate: how far do per-name baselines move under strict-privacy
normalization (qty-weighted from log.xlsx vs simple-average from the snapshot)?
Author-only (reads the private log.xlsx). Prints a table; flags large moves.
Apples-to-apples: both sides price off the same downloaded `prices` frame, so
the only variable is weighting (qty-weighted vs equal-unit simple-average).

Apples-to-apples case: load_transactions_from_log() returns columns
['ticker', 'date', 'action', 'shares'] — no 'price' column present.
The `price` column is added internally by build_positions() from the prices
frame, so both sides already derive prices from the same source. No drop needed.
"""
import os
import tempfile
from pathlib import Path

import pandas as pd

import build

THRESHOLD_PCT = 1.0   # flag any baseline that moves more than this, in %


def main():
    real_txns, _ = build.load_transactions_from_log()

    # Apples-to-apples guard: if a future schema change adds a 'price' column,
    # drop it so both sides price off the same downloaded `prices` frame.
    if "price" in real_txns.columns:
        real_txns = real_txns.drop(columns=["price"])
        print("NOTE: dropped 'price' column from log transactions (apples-to-apples guard)")
    else:
        print("NOTE: no 'price' column in log transactions — both sides price from OHLCV (expected)")

    tickers = sorted(real_txns["ticker"].unique().tolist())
    print(f"Downloading OHLCV for {len(tickers)} tickers...")
    ohlcv, failed, _ = build.download_ohlcv(tickers)
    if failed:
        print(f"WARNING: {len(failed)} tickers failed to download: {sorted(failed)}")
    prices = ohlcv.xs("Close", axis=1, level=1)

    # Side A: qty-weighted from real log (actual share quantities)
    pos_qty = build.build_positions(real_txns, prices)

    # Side B: equal-unit simple-average from snapshot (1 unit per BUY)
    # Use the real loader path via a temp file — exercises load_transactions_from_snapshot()
    # without touching the committed basket.snapshot.csv.
    snap = build.export_basket_snapshot(real_txns)
    tmp = Path(tempfile.gettempdir()) / "_baseline_diff_snapshot.csv"
    snap.to_csv(tmp, index=False)
    _orig = build.BASKET_SNAPSHOT_CSV
    build.BASKET_SNAPSHOT_CSV = tmp
    try:
        pos_snap = build.build_positions(build.load_transactions_from_snapshot(), prices)
    finally:
        build.BASKET_SNAPSHOT_CSV = _orig
        os.remove(tmp)

    # Build comparison table
    rows = []
    for t in pos_qty.index:
        if t not in pos_snap.index:
            continue
        a = float(pos_qty.loc[t]["baseline"])
        b = float(pos_snap.loc[t]["baseline"])
        if a and a == a:   # finite, non-zero
            d = (b - a) / a * 100.0
            rows.append((t, a, b, d))

    diff = pd.DataFrame(rows, columns=["ticker", "baseline_qty", "baseline_snap", "delta_pct"])
    diff = diff.reindex(diff["delta_pct"].abs().sort_values(ascending=False).index)

    pd.set_option("display.float_format", lambda v: f"{v:,.4f}")
    pd.set_option("display.max_rows", None)
    pd.set_option("display.width", 120)
    print()
    print(diff.to_string(index=False))

    flagged = diff[diff["delta_pct"].abs() > THRESHOLD_PCT]
    print(f"\n{len(flagged)} name(s) move more than {THRESHOLD_PCT}%:")
    print(flagged.to_string(index=False) if len(flagged) else "  (none)")

    print(f"\nTotal names: {len(diff)} | non-zero moves: {int((diff['delta_pct'].abs() > 1e-9).sum())}")


if __name__ == "__main__":
    main()
