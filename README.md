# Stocks dashboard

A personal portfolio dashboard for tracking, analysing, and acting on a basket
of stocks. Built as a **static HTML page** regenerated daily by GitHub Actions
from a transaction log (`log.xlsx`), with optional live news fetched in the
browser via a small Cloudflare Worker.

The author maintains this dashboard for their own portfolio, but the code is
public and reusable — fork it, point it at your own broker export, and you
have your own dashboard for the cost of a free GitHub account and a few
minutes of setup.

> **Not financial advice.** This dashboard is a personal **benchmarking
> exercise** — a tool the author built to track and reflect on their own
> basket. The figures, technical signals, exit-strategy labels and re-entry
> suggestions shown here are heuristics derived from public market data; they
> are **not investment recommendations**. **Capital at risk:** past
> performance does not predict future returns, and any stock shown here can
> lose money. Do your own research, or speak to a licensed adviser, before
> acting on anything you see on this page.

> **Built with Claude Opus 4.7.** The code, design decisions, prose
> commentary and this README were developed end-to-end in pair programming
> with Anthropic's Claude Opus 4.7 model.

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
   candidates, with concrete 2× ATR suggested stops.
6. **Basket diversification**: portfolio-level lens — pairwise correlations,
   most-correlated pairs (concentration risk), and best diversifiers.
7. **Regrets / Lucky escapes**: retrospective — did I sell too early or
   exit just in time.

This ordering matches how the author actually reviews the portfolio: you
read about the market first, look at how your sectors are doing, dig into
specific positions, then decide what to buy or sell.

This is only the **default**, though. Anyone can drag the sections into a
different order or hide the ones they don't use — see
[Customizable module layout](#customizable-module-layout).

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

### Quant signals (modal sub-row)

Clicking any row opens a modal whose lower half shows five per-stock technical
metrics, refreshed every build:

| Metric | What it tells you | How to read it in practice |
|---|---|---|
| **vs 200d** | Distance from the 200-day SMA in %. Above zero = price still riding the long-term trend; well below = trend has broken. | `+19%` (recent AAPL): comfortably above its long-term average, trend intact. `-69%` (SQQQ.L): trend decisively broken; only a mean-reversion bet, not a trend trade. |
| **ATR 14d** | 14-day Wilder Average True Range, quoted in **GBP**, with the % of price beside it. Use it to size stops — a stop set ~2× ATR below entry usually sits outside normal noise. | `£4.13 · 1.8%` (recent AAPL): typical daily swing is ~£4, so a stop ~£8 below entry survives a normal-volatility day. A name reading `4.0%` is roughly twice as twitchy — give it more room, or take a smaller position. |
| **RSI 14d** | Classic Wilder RSI. **≥ 70** flags overbought (red), **≤ 30** flags oversold (green). | `79` (recent AAPL): stretched — a pullback wouldn't be a surprise; not the ideal moment to add. `29` (recent WMT): washed out — watch for a bounce. `50` ≈ no momentum bias either way. |
| **52w pos** | Where today's price sits between the 52-week low (0%) and high (100%). Near-high = breakout candidate; near-low = potential value or broken trend. | `100%` (recent AAPL): at the 52-week high — breakout territory, but no headroom left. `5%`: near the lows — either deep value or a broken trend; cross-check with **vs 200d** to tell which. `50%` = mid-range, no strong directional signal. |
| **Volume** | Today's volume divided by the 63-day average. **≥ 1.0** = institutional support behind the move; **< 1.0** = thinly-traded, less trustworthy. | `1.3×` (recent AAPL): above-average participation — the day's price move is credible. `0.4×`: quiet, thin trading — any sharp move can fade quickly and shouldn't be treated as a real signal. |

A common combined read: a stock at **52w pos ≈ 100%**, **RSI > 75**, on
**Volume ≥ 1.5×** is a high-conviction breakout — but probably too late to
chase. The same name with **Volume < 0.7×** is a suspect move likely to
reverse. **RSI < 30** on a name still well **above its 200-day SMA** is the
classic "buy-the-dip" setup; the same RSI on one **40% below 200d** is just
a falling knife.

Source data is the full OHLCV cached in `data/ohlcv_cache.parquet` (one
yfinance batch per build, same lifecycle as the close-only cache). The
close-only `prices_cache.parquet` is left untouched — everything else in the
pipeline still reads its existing shape.

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

Each detractor row also surfaces a **2× ATR suggested stop** sub-line below
the action pill — e.g. `Stop £92.82 ($124.96) −2× ATR`. The GBP value is for
dashboard consistency; the parenthetical native-currency value is what you'd
actually type into the broker. A stop placed two ATRs below the current price
typically sits outside normal daily noise — close enough to limit damage if
the thesis breaks, wide enough not to be shaken out on a routine red day.

**Worked example.** A recent MSTR row read `CUT LOSS` with
`Stop £103.24 ($138.99) −2× ATR`. MSTR was trading around £146.40 with a
14-day ATR of £21.58 — the stop sits ~13% below current price. That's the
trade-off the heuristic makes concrete: if you'd accept a 13% drawdown
before admitting the thesis is wrong, leave the position open and use £103
as the hard line. If 13% feels too painful given your conviction, the
`CUT LOSS` label is telling you to size down today rather than continuing
to negotiate with yourself.

### Re-entry idea cards — RSI context

Each card in the **Re-entry ideas** module shows a small **RSI 14d pill**
beside the technical-signal label, color-coded with the same convention as
the modal: red for ≥70 (overbought — wait for a pullback), green for ≤30
(oversold — potential mean-reversion entry), neutral otherwise. Combined
with the upside %, this answers two questions at once: *how much room does
Wall Street see, and is right now a sensible moment to act?*

**Worked examples.**
- **CRWD with RSI 84** (red pill): analysts may still flag double-digit
  upside, but the stock is stretched right now — buying here often means
  immediately sitting through a pullback. A patient re-entry waits for
  RSI to cool back below 70.
- **PNR with RSI 30** (green pill): washed out near oversold. Combined
  with positive analyst upside, this is the textbook "buy the dip"
  alignment — entering weakness, not chasing strength.
- **ROP with RSI 43** (neutral): middling momentum tells you nothing
  either way; trade the thesis (upside %, analyst rec) without weighting
  the timing.

### Basket diversification

Computed from **6 months of native-currency daily returns** across every
currently-open position. The module renders three summary panels plus a
small histogram of the full pairwise-correlation distribution.

| Panel | What it tells you | How to read it in practice |
|---|---|---|
| **Avg pairwise correlation** | Mean of every pair's daily-return correlation. The headline diversification score in one number, color-coded green below `+0.30`, red above `+0.60`. | `+0.20` (recent basket): well diversified — most pairs only weakly co-move. `+0.50` would mean every pair shares roughly half its move. **Above `+0.60`** = the basket is effectively one big bet wearing many tickers. |
| **Most correlated — concentration risk** | Top 3 pairs ranked by correlation. Concentration warning: paying transaction costs to hold names that move as one. | `GLDW.L ↔ SGLN.L = 1.00` (recent): two physically-backed gold ETFs — literally the same trade. Consolidate into one. `AMAT ↔ LRCX = 0.88`: same semi-equipment sub-industry; high correlation is expected, but ask whether you'd be comfortable doubling the bigger one instead. |
| **Best diversifiers — lowest avg ρ vs rest** | The three positions whose mean correlation with every other open name is closest to zero (or negative). These are doing real diversification work. | `ICSU.L = −0.13` (recent): a short-duration treasury / money-market ETF that moves slightly inverse to the rest. Sizing this up cuts basket drawdown more than sizing up any single equity. |
| **Histogram** | The distribution of every pairwise correlation across eight 0.25-wide buckets between `−1.00` and `+1.00`. Hover a bar for the exact range and count. | A **right-skewed mound centred near `+0.10` to `+0.30`** (current shape) is the healthy pattern — most pairs only weakly relate. A tall bar in the `+0.75` to `+1.00` bucket = duplicated bets; a near-empty middle with a tall right-side cluster = a concentrated theme dressed in many tickers. |

**Combined read.** The current basket shows avg `+0.20` (well diversified)
*and* a histogram peaking in the `+0.00` to `+0.25` bucket — those agree.
The dashboard still flags `GLDW.L ↔ SGLN.L = 1.00` as a concentration
risk: a healthy basket overall doesn't mean every individual decision is
optimal, and the panel surfaces the specific pair worth acting on.

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

### Customizable module layout

Every section below the hero is a self-contained **module**. An "Edit layout"
button (top-right, beside the palette toggle) flips the page into edit mode,
revealing a drag handle and a show/hide toggle on each module. Drag to reorder
(powered by [SortableJS](https://sortablejs.github.io/Sortable/), vendored into
`docs/vendor/` so there's no CDN dependency), untick to hide, "Reset" restores
the default.

Order and hidden state persist in `localStorage` (`stocks-dashboard-layout-v1`),
so a customised layout survives rebuilds — `build.py` only ships the *default*
order, and each visitor's overrides live in their own browser. Same
client-side-state pattern as the palette toggle. If the author later adds a new
section, it slots into its default position for everyone — including visitors
who already customised — so nobody silently loses it. This is why people who
clone the repo each get a layout they can tailor without touching `build.py`.

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
│   ├── prices_cache.parquet           ← native-currency closes, all tickers
│   ├── ohlcv_cache.parquet            ← full OHLCV (for ATR / Volume metrics)
│   ├── benchmark_cache.parquet        ← SPY prices
│   ├── fx_cache.parquet               ← FX pairs (USDGBP, EURGBP, etc.)
│   ├── meta.csv                       ← sector / industry / name / currency
│   ├── analyst_cache.parquet          ← yf.info per ticker, 7-day TTL
│   └── universe_outlook_cache.parquet ← universe.csv precomputed, 30-day TTL
│
├── docs/
│   ├── index.html                     ← generated dashboard (~1.2 MB)
│   └── vendor/
│       └── Sortable.min.js            ← drag-reorder lib (vendored, no CDN)
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
