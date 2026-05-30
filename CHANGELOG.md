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
