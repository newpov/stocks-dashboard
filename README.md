# Stocks dashboard

A personal portfolio dashboard that reads a transaction log (`log.xlsx`),
fetches prices from yfinance, and writes a self-contained HTML dashboard to
`docs/index.html`. Multi-currency (GBP base, with FX attribution), per-ticker
charts, buy/sell markers, technical signals.

**Live dashboard:** see the GitHub Pages URL under repo Settings → Pages.

## Updating your trades

1. Edit `log.xlsx` (Sheet2): add a row with `Action`, `Time`, `ISIN`,
   `Ticker`, `Name`. Each row is treated as one unit.
2. Commit and push.
3. The dashboard rebuilds automatically every Saturday 08:00 UTC, or trigger
   it on demand via **Actions → Weekly rebuild → Run workflow**.

## Running locally

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
python build.py
# open docs/index.html
```

## Ticker resolution

Trading 212-style exports give you the local-exchange ticker. The build
script maps these to yfinance tickers via the ISIN country prefix:

| ISIN | Suffix | Example |
|------|--------|---------|
| `US`, `CA`, `IL`, `KY`, `BM`, `CH`, `AN`, `SG` | _(none)_ | `AAPL`, `BNS`, `CYBR` |
| `GB`, `JE`, `XS` | `.L` | `RR.L`, `BA.L`, `SGLN.L` |
| `IE` (UCITS ETFs) | `.L` | `VWRP.L`, `IUHC.L` |
| `IE` (US-listed ops: ETN, PNR…) | _(none)_ | `ETN` |
| `DE` | `.DE` | `RHM.DE` |
| `FR` | `.PA` | `SAF.PA` |
| `NL` | `.AS` | `ASM.AS` |

Manual fund entries with no ticker/ISIN can be mapped by name via
`FUND_NAME_TICKERS` in `build.py`. Edge cases go in `TICKER_OVERRIDES`.

## Files

| File | Purpose |
|------|---------|
| `log.xlsx` | Transaction log (source of truth) |
| `build.py` | Builds `docs/index.html` |
| `requirements.txt` | Python deps |
| `.github/workflows/build.yml` | Weekly rebuild + commit |
| `data/` | Price + FX + metadata caches (auto-managed) |
| `docs/index.html` | Self-contained dashboard (deployed by GitHub Pages) |
