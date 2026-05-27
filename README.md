# Stocks dashboard

A personal portfolio dashboard for tracking, analysing, and acting on a basket
of stocks. Built as a **static HTML page** regenerated daily by GitHub Actions
from a transaction log (`log.xlsx`), with optional live news fetched in the
browser via a small Cloudflare Worker.

The author maintains this dashboard for their own portfolio, but the code is
public and reusable — fork it, point it at your own broker export, and you
have your own dashboard for the cost of a free GitHub account and a few
minutes of setup.

---

## Why this exists

The author wanted a single page that answers, every morning:

1. **How is the basket doing** vs the S&P 500 over the same window?
2. **What's pulling it up or down** at the individual-stock and at the
   industry level?
3. **What's worth buying back** that was previously held and exited?
4. **What new ideas** the market is excited about that aren't already in the
   basket?
5. **What's worth trimming or cutting** given current technical and analyst
   signals?
6. **What changed in the market overnight** via curated finance news.

Off-the-shelf tools either don't combine these views in one screen (Trading 212
shows holdings but not analyst targets; Yahoo Finance shows targets but not
your basket TWR; Bloomberg combines them but costs $24k/year) or hide
attribution analysis behind subscriptions. This project sits in the gap: free
to run, public to share, structured by the questions the author actually asks.

A deliberate design constraint throughout: **everything must work as a static
HTML file served by GitHub Pages**. No backend, no auth, no database. The
build is a Python script; the runtime is the user's browser. The Cloudflare
Worker is the one optional piece of infra, and even without it the dashboard
falls back to build-time news cleanly.

---

## Architecture at a glance

```
            ┌──────────────────────────────────────────┐
            │  Daily 08:00 UTC GitHub Actions cron     │
            │  + manual workflow_dispatch              │
            │  + push to source files                  │
            └─────────────────┬────────────────────────┘
                              │
                              ▼
                     ┌──────────────────┐
                     │   python build.py│
                     └────────┬─────────┘
                              │
        ┌─────────────────────┼─────────────────────────┐
        │                     │                         │
        ▼                     ▼                         ▼
   yfinance              Yahoo + MW RSS            Local files
   prices                (build-time news)         (log.xlsx, csv)
        │                     │                         │
        └─────────────┬───────┴─────────────────────────┘
                      ▼
            ┌───────────────────────────┐
            │  data/*.parquet caches    │
            │  data/meta.csv            │
            │  data/analyst_cache       │
            │  data/universe_outlook    │
            └─────────────┬─────────────┘
                          ▼
                ┌───────────────────────┐
                │  docs/index.html      │  ← single self-contained file
                │  (renders 5 sections) │     ~1.2 MB, GitHub Pages serves
                └───────────────────────┘     this as the live dashboard
                          │
        ┌─────────────────┴─────────────────┐
        │     Browser loads the page         │
        │     + JS fetches live news via     │
        │       Cloudflare Worker (optional) │
        └────────────────────────────────────┘
```

Three things happen at three different cadences:

| Cadence | What runs | Where |
|---|---|---|
| **Every page load** | JS reads embedded JSON, draws hero chart + sparklines, fetches latest news from the Worker, applies filters | Browser |
| **Daily 08:00 UTC** (+ push events) | `python build.py` regenerates `docs/index.html` from `log.xlsx` and the data caches, commits + pushes | GitHub Actions |
| **Per cache TTL** (7d portfolio, 30d universe) | yfinance refetches when a cached row is older than its TTL | Inside `build.py` |

---

## What the dashboard shows

The page is organised top-to-bottom as a **decision flow** — research first,
then context, then details, then actions:

```
┌─ Header ──────────────────────────────────────────────────────────────┐
│ Palette toggle · Hero chart (basket vs SPY + GBP/USD bars) · 5 stats │
└───────────────────────────────────────────────────────────────────────┘

┌─ Industry outlook ─────────┬─ Market news ──────────────────────────┐
│ 6 industries from universe │ Live finance headlines (Worker) or    │
│ minus log.xlsx, top 3      │ build-time RSS (fallback). Source     │
│ stocks by analyst upside,  │ filter chips: All / MarketWatch /     │
│ cap-tier badges            │ Yahoo / CNBC / Motley Fool            │
└────────────────────────────┴───────────────────────────────────────┘

┌─ Industry attribution ───────────────────────────────────────────────┐
│ Cost-weighted basket return decomposed by industry. Shows which     │
│ sectors are pulling the basket up or down. Horizontal bars per row. │
└──────────────────────────────────────────────────────────────────────┘

┌─ Main returns table ─────────────────────────────────────────────────┐
│ All 185 positions. Columns: Ticker · Target · Upside · Analyst ·    │
│ Signal · Trend · Last · Cost · Purchased · Since baseline · 1W /    │
│ 1M / 3M / YTD · Post-exit. Sector chip filter + status chips.       │
│ Internal scroll (sticky header). Click any row → modal chart.       │
└──────────────────────────────────────────────────────────────────────┘

┌─ Re-entry ideas (analyst panel) ─────────────────────────────────────┐
│ Top 50 closed positions ranked by analyst price-target upside.      │
│ Each card: ticker · BUY/HOLD/SELL · current price · upside % ·      │
│ technical signal pill. Highlights analyst/technical divergence.     │
└──────────────────────────────────────────────────────────────────────┘

┌─ Exit strategy (top detractors) ─────────────────────────────────────┐
│ Top 8 open positions dragging basket return down. Columns include   │
│ technical signal, analyst recommendation, and a **suggested action**│
│ from a 3×3 heuristic: HOLD / TRIM / MONITOR / REVIEW THESIS / EXIT /│
│ CUT LOSS.                                                           │
└──────────────────────────────────────────────────────────────────────┘

┌─ Biggest regrets ──────────┬─ Lucky escapes ───────────────────────┐
│ Closed positions that      │ Closed positions that fell hardest    │
│ rallied most after exit    │ after exit                           │
└────────────────────────────┴───────────────────────────────────────┘
```

### The 5 header stats

| Stat | Definition | Why it's there |
|---|---|---|
| **Win rate** | % of closed positions with `total_pct > 0` | Personal skill gauge over time. Above 50% = better than random. |
| **Avg analyst upside** | Cost-weighted mean of `(target / current − 1)` across open positions | Forward-looking aggregate of Wall Street consensus on the basket. |
| **Max drawdown** | Worst peak-to-trough decline on the basket NAV series | The single most-cited risk number in portfolio reporting. |
| **Top contributor** | The stock that has added the most to basket return | Biggest single driver of the gains. |
| **Top detractor** | The stock that has subtracted the most from basket return | Biggest single drag. |

Deliberately **excluded** from this strip: total basket return and vs-SPY,
because both are already prominent in the hero chart's right-edge labels.
Duplicating them in stat cards wastes space.

### The hero chart

A composed visualisation with three layers sharing one x-axis:

- **Amber filled area + line:** the basket's time-weighted return %
  (renormalised as positions enter, so opening a new position doesn't reset
  the line).
- **Grey dashed line:** SPY benchmark, rebased to the same starting date.
- **Bottom strip — coloured bars:** weekly GBP/USD exchange rate centred on
  the baseline value (first week). Green bars = pound stronger than baseline,
  red = weaker. Labels show the baseline `ref $1.281` plus the range
  `$min — $max`.

Hovering the chart shows a tooltip with all three values at the hovered week.

### Decision-flow ordering

The vertical order is deliberate — research → context → details → action:

1. **Outlook + News** at top: "what should I read about today" — new
   stocks and the market backdrop.
2. **Attribution**: "which of my sector bets are paying off" — situational
   awareness of the basket as a whole.
3. **Main table**: detailed per-stock view with sorting + filtering.
4. **Re-entry ideas**: "of stocks I've held before, where do analysts see
   most upside" — buy candidates.
5. **Exit strategy**: "of my current losers, which should I cut" — sell
   candidates.
6. **Regrets / Lucky escapes**: retrospective — did I sell too early or
   exit just in time.

This ordering matches how the author actually reviews the portfolio: you
read about the market first, look at how your sectors are doing, dig into
specific positions, then decide what to buy or sell.

---

## Key design decisions

### Static site, no backend

GitHub Pages is free and hosts static files only. Every dynamic feature on
this page is either pre-computed at build time (most of it) or fetched
client-side from a public Worker (just the live news). There's no database,
no API key, no auth, no server logs. Restoring the site after an outage is
`git push`.

### Recency-based open/closed classification

A position is **closed** when its most recent transaction (in `log.xlsx`) is
a SELL, **open** otherwise. This rule is robust to partial exits and to
multi-cycle tickers (e.g., a stock bought, sold, bought again, sold again
would correctly classify as closed at the end). Net-share math would
misclassify these because Trading 212-style exports don't include per-row
share quantities.

### Two-tier caching with different TTLs

Analyst price-target and recommendation data is fetched from `yf.Ticker().info`:

| Pool | Tickers | TTL | Why |
|---|---|---|---|
| **Portfolio holdings** | ~185 (open + closed in `log.xlsx`) | **7 days** | Trading decisions need fresh signal; weekly refresh is enough since analyst targets don't shift on a daily basis. |
| **Reference universe** | 151 from `universe.csv` | **30 days** | Used only for the industry outlook section. Industry rankings move slowly; monthly is plenty. |

Both caches are committed to the repo (in `data/`) so CI builds reuse them
instead of refetching on every run.

### Time-weighted return (TWR), not money-weighted

The basket line is computed as `sum(w_i * ((p_i,t / p_i,base_i) - 1)) / sum(w_i)`
over tickers whose `baseline_date <= t`. This means adding a new position
doesn't drop the basket line — it just "joins" at its baseline. TWR is the
industry standard for performance reporting because it isolates pure stock-
selection skill from cash-flow timing.

### Exit-strategy heuristic

A 3×3 grid joining technical signal tone × analyst recommendation:

|                    | Analyst BUY | Analyst HOLD | Analyst SELL |
|--------------------|-------------|--------------|--------------|
| **Pos signal**     | HOLD        | TRIM         | EXIT         |
| **Neutral signal** | HOLD        | MONITOR      | EXIT         |
| **Neg signal**     | REVIEW THESIS | TRIM       | CUT LOSS     |

Two divergent signals = high conviction. Two agreeing signals = act.
**This is build-time analytics, not financial advice** — the user makes
the actual call.

### Multi-currency normalisation

All displayed values are in **GBP**. Native-currency prices are downloaded
from yfinance, FX rates are pulled separately, and `convert_to_base()`
applies daily FX to produce a base-currency price series. London-listed
pence-quoted symbols (GBp / GBX) are divided by 100. The hero chart's FX
bars surface this layer of the math directly.

### 4-palette theming

The CSS uses semantic custom properties (`--ink`, `--surface`, `--text`,
`--up`, `--down`, `--accent`). Switching palettes is just swapping the
class on `<body>`, which redefines the variable values. The user can pick
Default (dark amber), Soft Dark (lighter), Light (FT/NYT cream), or
Bloomberg (terminal amber/black). Choice persists via `localStorage`.

---

## File structure

```
stocks-dashboard/
├── README.md                          ← this file
├── build.py                           ← the build pipeline
├── log.xlsx                           ← author's transaction log (private)
├── transactions.csv                   ← demo log used if log.xlsx absent
├── tickers.csv                        ← legacy ticker config (unused)
├── watchlist.csv                      ← optional watchlist tickers
├── universe.csv                       ← 150 large/mid/small US caps for industry outlook
├── requirements.txt                   ← Python deps
├── .gitignore
│
├── .github/
│   └── workflows/
│       └── build.yml                  ← daily 08:00 UTC cron + push triggers
│
├── data/                              ← all caches (committed, reused by CI)
│   ├── prices_cache.parquet           ← native-currency prices, all tickers
│   ├── benchmark_cache.parquet        ← SPY prices
│   ├── fx_cache.parquet               ← FX pairs (USDGBP, EURGBP, etc.)
│   ├── meta.csv                       ← sector / industry / name / currency
│   ├── analyst_cache.parquet          ← yf.info per ticker, 7-day TTL
│   └── universe_outlook_cache.parquet ← universe.csv precomputed, 30-day TTL
│
├── docs/
│   └── index.html                     ← generated dashboard (~1.2 MB)
│
└── worker/                            ← optional Cloudflare Worker
    ├── index.js                       ← RSS proxy + finance-keyword filter
    ├── wrangler.toml                  ← deploy config
    ├── .gitignore
    └── README.md                      ← deploy instructions
```

---

## Data flow inside `build.py`

```python
main()
├── load_transactions_from_log()       # parse log.xlsx
├── load_watchlist()                   # parse watchlist.csv (optional)
├── download_prices(tickers)           # yfinance bulk fetch, daily history
├── download_benchmark()               # SPY
├── load_meta_cache() + fetch_meta()   # sector/industry/currency per ticker
├── download_fx(needed_pairs)          # USDGBP, EURGBP, etc.
├── convert_to_base(prices, meta, fx)  # native → GBP
├── build_positions(transactions, prices)  # returns table
├── compute_basket_mtm_series()        # TWR series
├── compute_benchmark_series()         # SPY rebased
├── compute_contributors()             # per-ticker contribution_pp
├── compute_signals()                  # technical signal per ticker
├── fetch_analyst_data(all_tickers, cache, ttl=7d)
├── load_universe() + fetch_universe_outlook(universe, ttl=30d)
├── fetch_news()                       # build-time RSS fallback
└── render_html(...)                   # write docs/index.html
```

`render_html()` is a single big f-string that templates the entire page. All
JS lives inside one `<script>` block at the bottom; all CSS is in one
`<style>` block at the top. Sections are produced by small helper renderers
(`render_table`, `render_detractors_strategy`, `render_industry_outlook`, etc.)
and stitched together in render order matching the decision-flow layout.

---

## The Cloudflare Worker (optional)

The Worker at `worker/index.js` is a small **CORS-headed RSS proxy** that
serves finance headlines as JSON. It exists because Yahoo Finance and
MarketWatch RSS feeds don't include CORS headers, so the browser can't fetch
them directly from `newpov.github.io`. The Worker fetches them server-side
(no CORS rules in Node), normalises the XML to JSON, and returns them with
`Access-Control-Allow-Origin: *`.

Three things the Worker does for free:

1. **Source-allowlists** to a hand-picked set of finance feeds (MarketWatch
   market-pulse + real-time, Motley Fool, CNBC Business, Yahoo Finance top
   stories). Keeps it from being an open proxy.
2. **Filters by finance keywords** so non-finance items leaking into top-story
   feeds (politics, lifestyle) get dropped.
3. **Edge-caches for 10 minutes**, so even a busy day's worth of visitors
   triggers maybe 30-40 upstream fetches.

If you don't deploy the Worker, the dashboard still works — the news section
falls back to build-time fetched content (refreshed once per CI cycle, so up
to 24 hours stale). Deploying the Worker upgrades news to live-per-page-load.

Full deploy instructions: [worker/README.md](worker/README.md).

---

## Setup if you're forking this

1. **Fork the repo** on GitHub. You get `<your-username>/stocks-dashboard`.
2. **Replace `log.xlsx`** with your own Trading 212-style export, or delete
   it and use `transactions.csv` (simpler CSV format — see the demo file).
3. **Enable Pages**: Settings → Pages → Source: "Deploy from a branch" →
   branch `main`, folder `/docs`. The URL becomes
   `<your-username>.github.io/stocks-dashboard`.
4. **(Optional) Deploy the Worker** for live news per the
   [worker/README.md](worker/README.md).
5. **Edit `universe.csv`** to taste — it's just a list of tickers to compare
   industry performance against. The default 150 are US large/mid/small caps.
6. **First build**: trigger Actions → Scheduled rebuild → Run workflow.
   The first run takes ~2 minutes because all caches are cold.

To run locally without pushing:

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows (use 'source .venv/bin/activate' on macOS/Linux)
pip install -r requirements.txt
python build.py
# open docs/index.html in any browser
```

---

## Known limitations

1. **Share-quantity approximation.** Trading 212-style exports don't include
   per-row share counts. The build treats each trade row as "1 unit," which
   inflates `total_invested` for multi-cycle tickers (e.g., COFF: 3 buys + 4
   sells over 4 cycles overstates the actual cost basis). The author has
   accepted this — the *transactional-recency* rule for open/closed
   classification is independent of the share math and is robust.
2. **Industry outlook is bounded by `universe.csv`.** Truly broad coverage
   (Russell 2000, global indices) would require either a paid screener feed
   or hardcoding thousands of tickers. The current 150-name universe is a
   pragmatic compromise.
3. **Worker is unmanaged for forks.** Forks inherit the source code but get
   their own `workers.dev` URL after running `wrangler deploy`. The
   `NEWS_WORKER_URL` constant in `build.py` must be updated by each fork
   independently.
4. **Yahoo Finance data quality** for small caps and foreign listings is
   sometimes flaky (missing analyst targets, occasional NaN price points).
   The build skips gracefully when this happens; the rest of the dashboard
   still ships.
5. **No real-time prices.** yfinance gives end-of-day closes only.
   Intraday is a separate problem with no free solution at scale.

---

## Refresh cadence summary

| Layer | How often | What triggers it |
|---|---|---|
| **News headlines** (live, via Worker) | Every page load | Browser fetch on load |
| **News headlines** (build-time fallback) | Per CI build | Daily 08:00 UTC or push |
| **yfinance daily prices** | Per CI build | Daily 08:00 UTC or push |
| **Analyst targets (portfolio)** | 7-day TTL per ticker | Daily CI checks fetched_at |
| **Universe outlook** | 30-day TTL on whole cache | Daily CI checks file mtime |
| **Industry classification (`meta.csv`)** | Once per new ticker | Cached forever; only fetched on first encounter |

In practice, opening the live URL gives you news fresh to the minute (if the
Worker is deployed) and prices/analyst data fresh to within the last day.

---

## Tech stack

- **Python 3.11** — build pipeline (`pandas`, `yfinance`, `feedparser`,
  `pyarrow`, `openpyxl`).
- **Vanilla HTML / CSS / JS** in the rendered page. No frameworks, no
  bundler, no transpilation. The whole page is a single ~1.2 MB file.
- **Cloudflare Workers** (free tier) for the news proxy. Zero npm
  dependencies in the Worker — RSS parsing via regex.
- **GitHub Actions** for the daily build + commit + push.
- **GitHub Pages** for hosting.

Total monthly cost: **$0**. The author has run this in production for
months at this cost.

---

## Roadmap / open ideas

- **Broader universe** for industry outlook (S&P 500 constituents) — adds
  ~5 minutes to the monthly refresh fetch.
- **Per-ticker news** in the modal — Yahoo Finance has per-symbol RSS feeds
  that could surface news specifically about a clicked stock.
- **Drawdown chart** — small inset visualisation of the drawdown series
  over time, not just the single max number.
- **Currency exposure breakdown** — pie chart of cost basis by FX currency
  to make currency risk explicit.
- **Dividend tracking** — Trading 212 exports include dividend rows; could
  surface as annual income and yield estimate.

Contributions and forks welcome.
