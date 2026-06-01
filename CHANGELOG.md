# Changelog

A high-level history of features shipped to the dashboard. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); version
numbers are author-tagged milestones, not strict semver.

Dates are reconstructed from the public [Actions history](https://github.com/newpov/stocks-dashboard/actions),
with each version representing one substantive feature push (daily bot
rebuilds are excluded).

The dashboard had ~8 internal iterations privately before the first
public push; **v1.0 reflects that already-mature initial state** rather
than a barely-working prototype.

---

## v1.7 — Customizable stats + drill-down modals · 31 May 2026

The hero stats become user-pickable, and most numbers on the page now
drill into a focused info modal showing the breakdown.

### Added
- **Configurable hero stats**: pick 5 of 10 stats via edit-mode (drag to
  reorder, checkbox to toggle, persisted to `localStorage`). Default
  selection = annualized return, Sharpe, win rate, win/loss ratio, avg
  analyst upside; the rest (total return, max drawdown, top contributor,
  top detractor, open positions count) are opt-in.
- **Rating-moves panel**: diff of this build's analyst cache vs last
  build's surfaces target-price changes &geq; 5% and recommendation
  shifts since the last refresh. Stored as `data/prior_analyst_cache.parquet`,
  refreshed automatically.
- **Unusual-volume chips**: pinned amber pills near the hero subtitle
  for open positions trading on >2&times; their 63-day average volume
  with a &gt;1% day move. Up to 3 chips; click any to drill into the
  ticker modal.
- **Click-to-expand drill-down modals** (T11&ndash;T15) via a new shared
  info-modal element layered under the existing ticker modal:
  - Industry-outlook cards &rarr; modal listing every ticker in that
    industry from the universe, sorted by 12-mo return.
  - Industry-attribution rows &rarr; modal listing every open position
    in that industry with weight, return, contribution.
  - Diversification "Best diversifiers" + "Most correlated pairs" ticker
    symbols &rarr; ticker modal.
  - Histogram-bar columns &rarr; modal listing every pair whose
    correlation falls in the clicked bucket.
  - Hero-chart weekly clicks &rarr; modal with that week's top 5 up &amp;
    down movers across held tickers.
- **Stacked modal semantics**: clicking a ticker inside an info modal
  opens the ticker modal *on top*; Escape closes the topmost first,
  then the underlying info modal on the next press.
- **30-day alpha sparkline hover**: shows date + value at the nearest
  point on mouse-move; crosshair + dot mark the position.
- **Mobile detractor cards**: below 700px, the 7-column "Exit strategy"
  table collapses to a 3-card stack with just the actionable tags
  (signal &middot; analyst &middot; suggested action); tap opens the
  ticker modal for full numbers.
- **Desktop-view override toggle**: a "Desktop view" button (visible on
  narrow viewports) forces the full desktop layout via `min-width:1100px`
  on the body -- the page becomes horizontally scrollable on phones for
  users who want full information density. Wikipedia / Reddit pattern.
  Persisted in `localStorage`.

### Changed
- Universe-only tickers (in `universe.csv` but not in your basket /
  watchlist) now open a "universe only" fallback info-modal instead of
  silently failing, showing the limited fields we have (name, industry,
  12-mo return, cap tier).
- `compute_basket_correlation` now returns the full pair list alongside
  the histogram and summary stats so T14's bucket-drill-down can filter
  in JS without a round-trip.
- Edit-layout discovery tooltip now positions itself dynamically against
  the Edit button's actual viewport rect (was hardcoded to `right:24px`,
  which misaligned with the palette toggle).

### Internal
- New `build_aux_payload()` helper assembles per-modal lookup tables
  (`industries`, `sectors`, `pairs`, `weekly_movers`) into a single JSON
  payload (`AUX_DATA`) consumed by the modal click handlers.
- New `_safeOpenTicker()` JS helper routes ticker-clickable elements
  through the universe-fallback path automatically.
- Stack-aware Escape handler consolidates ticker + info modal close
  routing into a single keydown listener.

### Deferred to v1.8
- **T16 precomputed modal chart polylines**: a perf optimization (&sim;200ms
  saved on first modal open) that requires migrating the modal chart
  to a fixed-viewBox coordinate system. Reverted from v1.7 scope as the
  visual-regression risk outweighed the marginal benefit.
- **Hero chart SPY-shading polygon alignment**: the green/red area
  between basket and SPY can mis-align with the SPY polyline by ~1 px
  near crossover points. Fix likely involves z-ordering the SPY line
  on top of the shading polygon.

---

## v1.6 — Risk-adjusted metrics + UX polish · 31 May 2026

Hero numbers get smarter (vol-adjusted, magnitude-aware, alpha-trended) and
the edit-mode UX stops looking glitched. Backing fetch layer also gets a
small observability boost.

### Added
- **Annualized return** in the hero subtitle alongside total return
  (geometric, gated below 3 months elapsed to avoid extrapolation noise).
- **Sharpe (1y)** stat card replacing Max Drawdown -- weekly log returns
  annualized via &times; &radic;52, risk-free = 0 (project convention).
  Color-banded green &geq; 1, red &lt; 0.
- **Win / loss ratio** stat card replacing Top Contributor -- realized
  &pound; magnitude of average win vs average loss. Distinguishes "70% win
  rate with tiny wins and huge losses" from genuinely good strategies.
- **30-day rolling &alpha; sparkline** beneath the hero chart -- excess
  basket return over SPY in trailing pp, with current value color-coded.
- **Build-health footer line**: surfaces silent yfinance failures
  (e.g. delisted tickers) that previously hid in stderr.
- **One-time edit-layout discovery hint**: pulse + tooltip on first ever
  page load, gated by `localStorage["edit-layout-discovered"]`.

### Changed
- **Hidden modules in edit mode** now collapse to a slim strikethrough
  placeholder bar instead of full-width dimmed content -- removes the
  "page looks broken" impression first-time visitors had.
- Hero subtitle wording: lead with the performance numbers, push the
  methodology note ("TWR, renormalized") to a dimmer secondary tier.
- Max Drawdown + Top Contributor stats stay computed in scope (no
  observable card) -- forthcoming v1.7 stats registry makes them
  opt-in via the edit-mode UI.

### Internal
- Every yfinance fetcher (`download_ohlcv`, `fetch_meta`,
  `fetch_analyst_data`, `fetch_ticker_news`) now returns its failed-ticker
  set as part of a tuple so `main()` can aggregate for the build-health
  footer. Two callers inside `fetch_universe_outlook` updated to unpack.

---

## v1.5 — Per-ticker context + GitHub-ready · 30 May 2026

Two threads landed together: extending the modal with stock-specific
news, and getting the repo polished for public consumption.

### Added
- **Per-ticker recent news** at the bottom of every modal — 5 headlines
  from yfinance, 7-day TTL parquet cache, publisher + relative-time
  subtext.
- **Hero chart redesign**: subtle red wash below the 0% baseline, green /
  red area shading between basket and SPY lines, brighter dashed zero
  line, `Δ +X.Xpp` delta badge.
- **MIT License** (`LICENSE`).
- **Standalone `demo.html`** at repo root — fully self-contained (~430 KB,
  SortableJS inlined), built daily by CI from `transactions.csv`. Downloads
  and runs without any setup. Replaces the previous CI behaviour of
  overwriting the live dashboard with sample data.
- **README screenshots** in `docs/assets/` (hero, modal, outlook+news,
  edit-layout) + status badges + acknowledgements section crediting
  yfinance, SortableJS, Cloudflare Workers, feedparser, Claude Opus 4.7.
- **`dashboard-audit.md`** + this **`CHANGELOG.md`**.

### Changed
- Outlook + News restored to **side-by-side layout** when both visible
  and adjacent (auto-paired via CSS Grid + JS).
- SPY benchmark line re-aligned to basket's date grid for precise visual
  overlay at any week.
- CI workflow now writes to `demo.html` only — never touches
  `docs/index.html`, so the author's locally-built dashboard stops
  getting silently overwritten by the daily bot.

---

## v1.4 — Customizable layout + quant deep-dive · 29 May 2026

Commit: `3b004b3`. The biggest feature push so far, bundling the
personalisation layer and a full quant-metric breakdown into one shipment.

### Added
- **Customizable module layout** — every section below the hero is a
  draggable, hideable "module". Edit-layout mode reveals drag handles
  and Shown/Hidden toggles per module; **localStorage persistence** for
  order + hidden state. SortableJS vendored into `docs/vendor/`.
- **Forward-compatible reconciliation** — when a new section ships later,
  it slots into its default position for users who already customised.
- **Quant-signals sub-row in the modal** — 200-day SMA distance, ATR
  (14d, GBP), RSI (14d), 52-week range position, volume ratio. Each
  color-coded with subtext context.
- **2× ATR suggested stops** on every Exit-strategy row (`£X.XX ($Y.YY)`
  dual currency) — turns categorical CUT LOSS / EXIT / TRIM labels into
  concrete broker-ready price levels.
- **RSI pill** on every Re-entry idea card, color-coded for oversold /
  overbought / neutral timing.
- **Basket diversification module** — avg pairwise correlation, top 3
  most-correlated pairs (concentration risk), top 3 best diversifiers,
  6-month correlation distribution histogram.
- **Full OHLCV cache** (`data/ohlcv_cache.parquet`) feeding ATR + volume
  metrics — single yfinance batch per build, no extra API calls.

---

## v1.3 — 5-stat strip + comprehensive README · ~27-28 May 2026

Commit: `825d034`. Hero stats expansion plus the first proper docs.

### Added
- **5-stat hero grid** replacing the original 3 cards: Win Rate · Avg
  Analyst Upside · Max Drawdown · Top Contributor · Top Detractor.
- **Win rate** computation (% of closed positions that ended profitable).
- **Avg analyst upside** (cost-weighted across covered open positions).
- **Max drawdown** with peak-to-trough date tracking.
- **Comprehensive README** covering architecture, design decisions, file
  structure, data flow, setup, refresh cadence — the first proper guide
  for anyone landing on the repo.

---

## v1.2 — Wire live news to Worker · ~26 May 2026

Late polish on the news pipeline shipped in v1.1.

### Added
- **Browser-side fetch** from the Cloudflare Worker on page load so the
  news feed refreshes every visit rather than only at build time.
- **Graceful degradation** — static build-time fallback shows if the
  Worker is unreachable; no visible breakage.
- **Source filter chips** on the news panel (All / Yahoo Finance /
  Motley Fool / CNBC).

### Fixed
- HTML-entity decoding in news titles (numeric `&#x2018;`, named `&apos;`
  etc.) so headlines render cleanly.

---

## v1.1 — Watchlist + Re-entry analyst + Worker plumbing · 25 May 2026

The first big content expansion beyond the basket itself.

### Added
- **Watchlist support** (`watchlist.csv`) — tickers tracked but not held,
  fetched alongside the portfolio so the modal can open them too.
- **Re-entry ideas** module — Wall Street consensus targets and upside
  for closed positions, ranked by upside. Answers "of stocks I've held
  before, where do analysts now see most room?".
- **Exit-strategy detractors module** — 3×3 grid joining technical
  signal tone × analyst recommendation → suggested action (`HOLD` /
  `TRIM` / `EXIT` / `REVIEW THESIS` / `CUT LOSS`).
- **Analyst data layer** — yfinance target prices + recommendations,
  7-day TTL parquet cache.
- **Cloudflare Worker** (`worker/`) — CORS-headed RSS proxy with
  hand-picked finance feeds. Replaced the build-time static news with a
  live feed.
- **Industry outlook module** — 12-month return leaders from a 150-name
  reference universe (`universe.csv`), with mega/large/mid/small cap
  badges. Refreshed monthly.
- **Industry attribution module** — cost-weighted contribution per
  sector for the current open positions.
- **Signal column + Target/Upside/Analyst columns** in the holdings
  table.
- **Sector chip filter** above the holdings table.
- **GBP/USD bar chart** beneath the basket line in the hero.
- **4-palette toggle** (Default · Soft Dark · Light · Bloomberg).

### Changed
- Open/closed classification switched from share-net-math to
  **transactional recency** (last action = SELL → closed). Robust to
  partial exits and multi-cycle tickers.

---

## v1.0 — Initial v9 dashboard · 24 May 2026

First public push. The dashboard had been privately iterated to "v9"
before this, so the genesis already shipped a mature feature set rather
than a barebones prototype.

### Added
- **Time-weighted return (TWR)** for the basket vs SPY, in GBP.
- **Multi-currency normalisation** (USD, EUR, GBp pence handling) via
  daily FX rates from yfinance.
- **Hero line chart** — basket vs SPY benchmark since the first
  purchase (24 Oct 2024).
- **Holdings table** with weights, returns, cost basis, % moves over
  1W / 1M / 3M / YTD, with sort + search.
- **Per-stock modal** with detail chart and headline stats.
- **Regret tracker** — post-exit price moves for closed positions
  (regret = sold too early, escape = sold before a drop).
- **Contributor/detractor stats** in the hero header.
- **Static-site architecture**: GitHub Actions daily cron + GitHub
  Pages, zero backend, zero auth, zero API keys, **$0/month** to run.

---

*Maintained as a hand-curated narrative rather than auto-generated from
git log — the commit history is dominated by daily bot rebuilds, which
don't change features.*
