# Quick start

A zero-backend stock dashboard: a single Python script turns a CSV of trades (or
just a watchlist) into a static, analyst-style HTML page — technical signals,
analyst targets, news, prediction-market odds, and a daily "what to look at
first" read. No server, no database, no build tooling.

**Live demo:** https://newpov.github.io/stocks-dashboard/ ·
Full docs: [README.md](README.md) · Changes: [CHANGELOG.md](CHANGELOG.md)

## Try it in 3 steps

```bash
git clone https://github.com/newpov/stocks-dashboard
cd stocks-dashboard
pip install -r requirements.txt

python build.py --demo        # builds demo.html from the sample transactions.csv
```

Open `demo.html` in any browser. That's the whole thing — one self-contained file.

## Use your own data

Pick whichever fits; both are plain CSV:

- **You have trades** → put them in `transactions.csv` (broker-agnostic columns:
  `symbol`, `quantity`, `side`/action, `date`, `price`). Then `python build.py`
  writes `docs/index.html`.
- **You just want to track ideas** → list tickers in `watchlist.csv`
  (`ticker` required, `note` optional) and run `python build.py --watchlist-only`
  — each is tracked equal-weight from its start.

Prices, FX, analyst data and news are fetched from yfinance at build time and
cached in `data/` (committed, so rebuilds are fast and offline-friendly).

## Handy commands

```bash
python build.py                  # real dashboard -> docs/index.html
python build.py --demo           # sample dashboard -> demo.html
python build.py --watchlist-only # track watchlist.csv only (no positions)
python -m pytest -q              # run the test suite
python -m http.server 8765 --directory docs   # preview docs/ locally
```

## Notes

- **Privacy:** the dashboard treats each position as one equal-weight unit — no
  share counts or money amounts surface. Your real `log.xlsx` (if you use one)
  stays local and gitignored.
- **Hosting:** push `docs/` to GitHub Pages (or open the file directly). The
  optional Cloudflare Worker in `worker/` only powers the live-news refresh and
  degrades gracefully when absent.
- **Not financial advice** — it's a personal tracking/learning exercise.
