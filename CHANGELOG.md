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

## v2.4 — Equal-weight basket, value-weight mode, Signal map, mobile fix, +50 quiz · 13 June 2026

A methodology pass and a readability pass. The basket is now **equal-weighted**
(each position = one unit), removing a rebase bias where a stock split or heavy
scale-in could make a name look like a 10&times; position and drag the headline
return down. An opt-in **value-weight mode** restores capital-weighting for
anyone who supplies real share quantities. The conviction quadrant is replaced
by a far more legible **Signal map**, and one CSS root-cause fix ends the mobile
horizontal-overflow that was clipping every section.

### Added
- **Value-weight mode** (`WEIGHT_MODE` env var or `--weight value`) &mdash;
  opt-in capital weighting by real `shares &times; price`. Reads real quantities
  from `transactions.csv` (`shares`) or a quantity column in `log.xlsx` if one is
  present; otherwise falls back to one unit per position. Default stays **equal**
  (privacy-driven, no monetary scale). `build_positions` and the basket MTM both
  branch on the mode; everything downstream reads `weight` and follows.
- **Signal map** &mdash; replaces the conviction-vs-signal quadrant with a
  **beeswarm**: every open position placed along a bearish&harr;bullish technical
  axis, jittered vertically so none overlap, coloured by return (green up / red
  down), with hover tooltips and only the extremes labelled. (Equal weight
  removed the old size axis and a holding-tenure axis clustered badly &mdash; the
  beeswarm is collision-free by construction.)
- **Market expectations &mdash; question subtitles** &mdash; each row now shows
  the underlying market question beneath the theme ("Fed rate decision" &rarr;
  "Will the upper bound&hellip; be above 5.25%? &mdash; 0%"), so the % has
  meaning. Themes expanded toward **political / shock** markets (Fed emergency
  meeting, government shutdown, debt ceiling, tariffs, China&ndash;Taiwan).
- **+50 quiz questions** (50 &rarr; 100) &mdash; 5 entry-level + 5 hard per
  category (25 + 25), same cloze/direct mix, plus an optional `difficulty` field.
- **Watchlist-only mode** (`--watchlist-only`) &mdash; build the whole dashboard
  from `watchlist.csv` with **no trade history**: each ticker is tracked
  equal-weight from its window start, so a newcomer gets the full per-ticker
  briefing (signals, analyst, news, Big Brain, Signal map) without modelling a
  single trade. Banner + hero relabel to "Your watchlist". Lowers the
  barrier-to-try for forkers.
- **Broker-agnostic CSV import** &mdash; `transactions.csv` now accepts common
  header variants (`symbol`/`quantity`/`side`/&hellip;) and free-text actions
  ("Market buy", "Sold", "B"); only ticker/date/action are required (no quantity
  column &rarr; one unit per row), so most brokers' exports work with little
  editing.

### Changed
- **Equal-weight basket** &mdash; `build_positions` collapses every position to
  one unit; the basket series is the equal-weight mean of per-position returns
  (closed positions frozen at their realized return so closing a winner doesn't
  drop the line). Currency exposure, contribution, industry attribution and
  analyst-upside all follow. The **win/loss ratio is now %-based** (no &pound; in
  the hero stat). The badge tooltip still shows the raw buy/sell counts.
- **Hero stats** &mdash; `Annualized` dropped from the default set (it
  duplicated the chart's return); still selectable in the picker. The
  **since-you-last-looked** banner is now a compact pill directly under the stat
  strip &mdash; and a latent bug where `display:flex` overrode the `hidden`
  attribute (an empty orange bar on first visits) is fixed.

### Fixed
- **Mobile horizontal overflow** &mdash; a wide table forced the single grid
  track to its min-content, blowing the whole page past the viewport and
  clipping every section (Big Brain included). `min-width:0` on the module grid
  items lets the inner scrollers scroll instead; the 2-column regret +
  diversification grids collapse to one column; the news feed caps to half the
  screen height.
- **Market expectations** &mdash; theme links point to the Kalshi **series**
  page (the raw event-ticker path 404'd); a **0pp** delta now renders neutral
  instead of misleading green/red.

## v2.3 — Market expectations, Big Brain memory, since-you-last-looked, conviction quadrant · 13 June 2026

Extends the v2.2 decision surface with a forward-looking **sentiment** layer,
a sense of **memory** (how past calls played out), a **welcome-back** diff, and
a portfolio-wide **conviction-vs-signal** lens. Plus three fixes surfaced in
day-to-day use.

### Added
- **Market expectations module** — prediction-market sentiment from **Kalshi +
  Polymarket**. A row per curated theme (Fed decision, recession, S&amp;P range,
  market-crash, &hellip;): implied-probability % + a bar that mirrors it +
  **since-last-build &Delta;** + a source badge, sorted by |&Delta;| so the
  biggest movers lead. Curation is a single committed `predictions.csv`
  (theme + an explicit `source` column, which dissolves multi-source dedup);
  the build auto-resolves the current open market per theme. Two fetchers
  behind one normalized record via stdlib `urllib` (no new runtime dep), each
  graceful-skip on failure. **Build-time only** &mdash; the daily demo/CI
  rebuild renders from the committed cache and makes **no** network calls to
  the prediction APIs. A one-line legend explains what the %, bar and &Delta;
  mean. Frame: market-implied odds, shapes-not-amounts, not advice.
- **Big Brain macro callout** &mdash; a pinned full-width strip above the
  Big Brain 2&times;2 when a tracked prediction market moves &geq; 8pp
  build-over-build, related to the basket's equity exposure
  ("recession odds +8pp &rarr; you're ~95% equities").
- **Big Brain memory** &mdash; when a name Big Brain flags now was flagged
  before, the card carries a small "flagged 3 weeks ago &mdash; +12% since"
  line, so the board becomes accountable for its past calls. Backed by a
  committed `data/bigbrain_log.csv` (date / ticker / price / archetype &mdash;
  no &pound;, no shares); the real build appends, the demo skips.
- **"Since you last looked" banner** &mdash; a dismissible strip below the hero
  that diffs the current page against a `localStorage` snapshot from your last
  visit: basket &Delta;pp, new Big Brain idea names, and any tracked prediction
  market that shifted. Shows once per new build; nothing on a first visit.
  Pure client-side.
- **Conviction-vs-signal quadrant module** &mdash; a full-width SVG scatter of
  every open position: **x** = a 0&ndash;100 technical signal score
  (RSI distance-from-50 + trend + momentum), **y** = position weight, dot
  **colour** = bullish/bearish. Surfaces the mismatches a table hides &mdash;
  a big position with a quiet signal (top-left), a small position screaming
  (bottom-right). Only the ~12 standouts are labelled so a 100+-name basket
  stays readable; every dot clicks through to its modal.
- **`test_predictions.py`** (15 `pytest` cases) for the prediction layer;
  `test_bigbrain.py` expanded to 50 (memory, quadrant score/data/render,
  analyst-snapshot age-gate). 65 tests total.

### Changed
- **Rating-moves baseline is now stable (~weekly).** The "prior" analyst
  snapshot only refreshes when it's missing or &gt; ~6 days old, instead of
  being overwritten every build. Previously the baseline reset to *current* on
  every run, so target/recommendation moves only ever flashed for a single
  build and then vanished; they now measure against a meaningful reference and
  accumulate over time.

### Fixed
- **News panel could render empty.** A saved source-filter
  (`localStorage`) that no longer matched the live worker's feed set (which has
  narrowed over time) hid every headline until you clicked a chip.
  `applyNewsFilter` now falls back to "All" when the chosen source matches no
  rendered rows, and re-persists `*` so it self-heals.
- **Lazy modal could stay permanently blank.** A transient failure of the v2.0
  sidecar fetch (e.g. during a GitHub Pages redeploy window) marked the ticker
  "hydrated" with no data, so reopening never retried. The ticker is now only
  marked hydrated when the fetch actually succeeds.
- **Quiz repeated the same question.** The picker now excludes the
  last-shown question, so you never get the same one twice in a row across
  opens or "Next".

---

## v2.2 — "Big Brain says" discovery board · 5 June 2026

Turns the dashboard from a data display into a **decision surface**. A new
top-of-stack module reads every metric the build already computes and surfaces
the four names where the most is happening — leaning toward **discovery**
(stocks the author doesn't own yet) rather than restating the current basket.
Ships with the project's first automated test suite.

### Added
- **"Big Brain says" discovery board** — a full-width 2&times;2 of four
  colour-coded cards, pinned at the top of the module stack. Two cards come
  from a **universe-discovery lane** and two from the author's **live basket**;
  a sold position can steal a basket slot when it out-signals an open one.
  Each card carries a punchy one-line verdict, evidence "pills" (e.g.
  `RSI 71`, `#2 weight`, `2.3x vol`, `target -9%`), and a best-effort **real
  news headline** (title + publisher, linked out) when a recent one exists.
  - **Signal-stacking engine.** Per ticker, ~18 flag detectors across five
    domains (position, trend, flow, street, news) fire from the existing
    `quant_metrics` / `signals` / `contrib` / `rating_moves` data. A stack
    score sums flag weights and applies a **domain-diversity multiplier**
    (`1 + 0.25 &times; (distinct_domains - 1)`, capped at 2.0), so a name
    flagged across four different panels outranks one with four flags from a
    single panel — surfacing cross-panel correlations a daily scan misses.
  - **Universe shortlist + deepen.** The ~500-name reference universe is
    pre-ranked on already-cached outlook fields (analyst upside &times;
    coverage + 12-month momentum + recommendation); the top ~40 are then
    "deepened" — OHLCV + news fetched, quant/signals computed — so unowned
    names stack signals on equal footing. Cached to
    `data/bb_universe_ohlcv_cache.parquet` (TTL-style reuse) to keep builds
    fast. A `beats_your_sector` relational flag ("outpacing every semi you
    own") keeps idea cards distinct from the Industry-outlook leaderboard.
  - **Colour-coded card types**: red `Bleeding` / amber `Running hot` /
    green for constructive held names; blue `Setup you're missing` (or the
    neutral `On the radar` when no portfolio relationship is computed) for
    unowned ideas; green `Ran without you` for sold names that kept climbing.
    An ownership badge (`held` / `not owned` / `sold`) sits on every card.
    The 2&times;2 collapses to a single column under 760px.
  - **Privacy preserved.** Cards use shapes not amounts throughout — RSI, %,
    pp, weight *rank*, volume ratio, holding-period days — never £ figures or
    share counts, consistent with the rest of the dashboard.
- **First automated tests** — `test_bigbrain.py` (38 `pytest` cases) locks the
  pure Big Brain logic: flag detectors, the diversity-weighted scorer,
  severity mapping, archetype matching, the two-lane 2+2 selection with
  backfill, the universe shortlist ranking, the relational flag, and the
  render markup. New `requirements-dev.txt` pins `pytest`.

### Changed
- **Module order**: `Rating moves` now sits directly under `Re-entry ideas`
  (both are analyst-signal modules), and `Industry attribution` now sits
  directly under `Basket diversification` (both are portfolio-level lenses).
  Saved per-visitor layouts are unaffected; only the shipped default changed.

---

## v2.1 — Quiz feature, modal axis polish, closed-list signal triage · 3 June 2026

A UX-polish release that also extends the v1.9 educational layer with a
50-question finance-knowledge quiz (5 categories, medium difficulty,
practical tone). Four other friction points noticed during day-to-day use
get closed, including the visible text-stretch in the modal chart axis
labels (the visual cost of v1.8 T1's `preserveAspectRatio="none"`
decision — now isolated rather than reverted).

### Added
- **Finance-knowledge quiz** (`Quiz` button in the top-right cluster).
  50 medium-difficulty questions across 5 categories complementary to
  the Pocket Lesson topics: **Market mechanics** (settlement, order
  types, dark pools, IPO mechanics, exchange structure), **Corporate
  actions** (splits, spinoffs, M&amp;A, rights, buybacks, DRIP, tender,
  ex-div), **Beyond equities** (yield curve, credit spreads, ratings,
  ETF vs MF, commodities, TIPS, callable bonds, money market),
  **Derivatives** (covered call, Greeks, IV, futures vs forwards,
  straddle, cash-secured put, put-call parity), **History &amp; regs**
  (1929, 1987, 2008, Glass-Steagall, circuit breakers, Dodd-Frank,
  MiFID II, SEC, FCA, Sarbanes-Oxley). Each question is 3-option
  multiple choice with a 1-sentence explanation revealed on answer.
  Correct answers flash green via a `@keyframes flashCorrect` pulse;
  the monthly score (e.g. `7/10`) bumps with a `@keyframes scorePop`
  scale + color punch. State (seen set, monthly counters) persists in
  `localStorage` as `quizSeen` + `quizMonthly`, with auto-reset of
  monthly counters on each calendar-month flip and seen-set recycle
  when 90%+ of the pool has been answered. Authoring cost: 10 questions
  per category, balanced topic coverage within each.
- **HTML overlay for modal chart axis labels.** Y-axis percentages,
  x-axis dates, and the `B` / `S` characters inside transaction markers
  now render via an `.modal-chart-labels` div positioned absolutely on
  top of the SVG, with label positions computed in CSS percentages
  mapped from the same viewBox coordinates the SVG uses. The SVG keeps
  geometry (polyline, grid lines, baseline, area gradient, hover
  crosshair, transaction circles); HTML carries everything text-bearing.
  Result: axis labels render at native browser DPI instead of inheriting
  the SVG's ~1.93× non-uniform horizontal stretch. v1.8 T1's
  precomputed-geometry win (resize-free chart, ~150-300ms saved on first
  modal-open) is preserved — only the visible cost (distorted text) was
  isolated and fixed.
- **Closed-positions default sort by Signal** (col 4). Clicking the
  Closed filter chip now re-sorts the table by Signal instead of the
  Open-mode default (Since-baseline, col 9). Like-signal rows group
  together for fast pattern-spotting — eight consecutive "Trending up"
  closed positions at the top of the list make it obvious which exit
  pattern recurs most often. Switching back to Open restores the
  original sort. Sector chips don't trigger the resort; only the status
  chips do.
- **Industry attribution bar labels**: each bar in the Industry
  attribution table now carries an inline numeric label at its tip
  (e.g. `+29.88`). Short bars place the label outside (right of the
  bar tip, color-matched to the fill); long bars (~30%+ of the
  half-width, typically the top 1–2 contributors) flip the label
  INSIDE the bar in dark text on the bright fill, so the dominant
  contributor's number never overflows the scroll container. The
  Contrib column still carries the exact `pp` figure for tabular reading.
- **Quant signal hover interpretations**: each of the five modal quant
  cells (VS 200D, ATR 14D, RSI 14D, 52W POS, Volume) now exposes a
  native HTML `title` tooltip with a value-aware reading. RSI 82 →
  "Overbought (>70) — strong recent buying; momentum may exhaust soon",
  52W POS 100% → "Near 52-week high — potential resistance or strong
  momentum signal", etc. Interpretation is generated client-side from
  the value + threshold zones (zero bytes added to the per-ticker HEAVY
  payload).

### Changed
- **Top-right control toolbar consolidated + iconified**: the four-button
  palette switcher (Default / Soft Dark / Light / Amber) collapses to a
  single button that cycles through the four palettes on each click. All
  topbar buttons are now icon-only — a sliders glyph for Edit layout,
  a brain for Pocket lesson, a question-mark-in-circle for Quiz, and a
  filled circle in `var(--accent)` for Palette (the circle's color
  follows the active palette so the active theme is visible at a
  glance). Hovering any button slides a 150ms-faded `data-tooltip`
  pseudo-element below with the human-readable name (e.g.
  POCKET LESSON, QUIZ). Reset (undo arrow) and Desktop view (monitor)
  also gain icons for visual consistency. Topbar horizontal footprint
  drops ~67% vs the v2.0 text buttons. State persistence unchanged —
  `stocks-dashboard-palette` localStorage key still holds the chosen
  palette name across page loads. `aria-label` retained on every
  button for screen-reader access.
- **Industry-attribution label format**: dropped the redundant `+`
  prefix from positive labels (color carries the sign), and labels now
  anchor at the axis line on the side opposite the bar tip (positives
  right-aligned to the left of axis, negatives left-aligned to the
  right). Same-sign labels form a tight vertical column at the axis;
  digit count (e.g. `1.49` vs `29.88`) is visible by horizontal extent
  rather than label position. Bars and labels never overlap regardless
  of magnitude — the v2.1 part-1's inside/outside conditional became
  unnecessary and was removed.

### Internal
- New constant `QUIZ_POOL` (50 entries × ~250 bytes ≈ 12 KB inline so
  the quiz works immediately on toolbar click without waiting for HEAVY
  fetch). Schema: `{id, category, format, question, options[3], correct,
  explanation}`.
- New IIFE `setupQuiz()` carrying the state engine (load / save /
  pickNext / recordAnswer) plus modal show/hide/answer wiring. ~150
  lines including the rendering + animation triggers.
- New helper `quantTitle(k, v)` in the modal render path builds the
  threshold-aware interpretation strings on demand.
- New CSS classes: `.modal-chart-labels` (axis-label overlay container),
  `.y-tick`, `.x-tick`, `.txn-marker` (HTML label positioning), plus
  refreshed `.ia-bar-label` / `.pos` / `.neg` (no longer needs inside
  variants).
- `renderBigChart` now builds `labelsHtml` alongside the SVG inner HTML
  and writes to `.modal-chart-labels.innerHTML` after the SVG is set.
  Cleared on every render so prior-ticker labels don't bleed into the
  loading state.
- The sort-on-mode-change logic in the chip-click handler checks for
  the `.chips-sectors` parent class so sector-filter chip clicks don't
  trigger a resort — only status chips do.

### Deferred (v2.2 or later)
- **`Purchased` → `Sold` column for closed positions** — user-flagged
  consistency concern. Header label change per-filter is unusual UX;
  needs a sit-down to decide between (a) per-filter header rewriting,
  (b) a separate "Sold" column visible only in Closed mode, or (c) a
  neutral "Last action" column with status-dependent date.

---

## v2.0 — Lazy-loaded modal data + code-review fixes · 2 June 2026

The dashboard had grown to a 2.44 MB single-file payload by the end of
v1.9. v2.0 splits that into a 1.38 MB lean shell + a 1.07 MB sidecar
payload fetched on idle &mdash; the browser parses 43% fewer bytes
before first paint. Modal opens stay instant (the sidecar is usually
already cached by the time the user clicks). Paired with two fixes
surfaced by a code-review pass against the v1.9 baseline.

### Added
- **Lazy-loaded modal data**: per-ticker fields used only inside the
  ticker-detail modal (chart polyline, transactions, FX attribution,
  P&amp;L breakdown, news, quant signals) now live in
  `docs/data/payload.json` rather than inline. The page ships only the
  light fields needed by the main table (`name`, `sector`, `total`,
  `ytd`, `signal`, &hellip;), then `requestIdleCallback` prefetches the
  sidecar after first paint. First modal-open merges the heavy fields
  into `DATA[tkr]` &mdash; subsequent opens are instant via Promise
  memoisation. Cold-path fallback (user clicks before prefetch resolves)
  shows a brief loading spinner in the chart area. Cache-busted with
  `?v={build_timestamp}` so post-rebuild visitors always see fresh data.
- **Spinner + "Loading chart…" overlay** inside `.modal-chart-wrap` for
  the cold-path case, animated via a CSS `@keyframes modalSpin`.

### Fixed
- **Desktop-mode toggle leaving the topbar off-screen** (pre-existing
  bug since v1.7, surfaced during v2.1 verification). On viewports
  &le; 900px wide, clicking the "Desktop view" icon sets
  `body.force-desktop` which gives the page `min-width: 1100px` —
  making it wider than the viewport. The topbar was positioned
  `right: 28px` from the (now 1100px) container's right edge,
  which is ~120px past the viewport edge on a 950px window,
  so the controls became invisible. Fixed by switching the
  topbar to `position: fixed` only when `body.force-desktop`
  is active, anchoring it to the viewport rather than the
  container. Tapping the same icon again exits desktop view
  normally.
- **Empty-contributors crash (H1)**: `main()` printed
  `Contributors: top {contrib.iloc[0].name}` without guarding for an
  empty `contrib` DataFrame. Triggers only when every position is closed
  (no open positions left). Render path was already guarded; only the
  stdout log was exposed. Now prints `Contributors: none (no open
  positions)` instead of `IndexError`-ing.
- **YTD silently wrong for tickers with &lt;1y of history (M1)**: the
  fallback when `ytd_sub` was empty used `avg_buy_price`, which means
  "YTD" displayed return-since-buy rather than return-since-Jan-1
  &mdash; a misleading number for any future recent-IPO ticker. Changed
  the fallback to `NaN` so the column renders as `&mdash;` instead. No
  current basket ticker hits the fallback (every position predates
  `START_DATE = 2024-10-14`); the fix is forward-looking.

### Internal
- **Code-review subagent pass** against the v1.9 baseline using the
  `code-reviewer` subagent from the Claude Code [`feature-dev`
  plugin](https://github.com/anthropics/claude-code) (official
  marketplace `claude-plugins-official`). Other Claude Code users can
  enable it via the in-app plugin manager (`/plugin`) and then dispatch
  it on any repo via the `Agent` tool with
  `subagent_type: "feature-dev:code-reviewer"`. The pass surfaced 4
  findings (1 HIGH, 3 MEDIUM), 2 fixed (H1, M1), 2 deferred (Atom feed
  link extraction in the Worker &mdash; no current Atom feeds, dead
  path; `yfinance` single-pair MultiIndex handling &mdash; doesn't
  trigger on the pinned 1.4.0). Privacy audit: clean across the board
  &mdash; `shares = 1.0` convention holds, `r.weight` resolves to price
  not invested capital, no author-name strings in code paths.
- **New constants**: `HEAVY_JSON` (sidecar path), `LIGHT_KEYS`
  (frozenset of fields kept inline).
- **New helpers**: `split_payload(full) -&gt; (light, heavy)` runs at
  render time to partition the per-ticker payload dict.
- **`HEAVY_URL` JS const** controls the lazy/inline branch &mdash;
  non-null for `docs/index.html`, `null` for `demo.html` (which keeps
  the single-file model so forkers can copy one file and run).
- **CI workflow unchanged** &mdash; per the v1.5 architecture decision,
  CI only rebuilds `demo.html` + `data/*.parquet`. The new
  `docs/data/payload.json` is committed by the author on feature
  rebuilds, same publishing pattern as `docs/index.html`. Expected repo
  growth ~15 MB/year (1 MB per shipped feature × ~10 ships).
- **`daily_rebuild.ps1` updated for v2.0**: the local scheduled-task
  script that auto-rebuilds + auto-pushes nightly now classifies
  `docs/data/*` as "safe to discard before rebuild" (otherwise the
  pre-flight check would abort on a modified `payload.json`) and stages
  `docs/data/` alongside `docs/index.html` + `data/` for the nightly
  commit. Without this, the daily auto-refresh would have broken on the
  first run after the v2.0 push.
- **`docs/index.html`**: 2444 KB &rarr; 1378 KB. **Initial-paint
  parsing cost**: ~200 ms &rarr; ~80 ms estimated. Total wire bytes
  basically unchanged (sidecar adds back what HTML lost).

### Deferred
- **Atom feed link extraction** in `worker/index.js` (no current Atom
  feeds in `FEEDS`).
- **`download_fx` MultiIndex defensiveness** in `build.py` (the
  pinned `yfinance 1.4.0` doesn't trigger the edge case).
- **Structural refactor of `build.py`** into modules &mdash; deferred
  again; the file is now ~7220 lines and pushes against the threshold
  where a split starts paying off.
- **Dividend tracking, sector heatmap** &mdash; still in the backlog.

---

## v1.9 — Educational layer + polish · 2 June 2026

A new opt-in educational layer (Pocket lesson card) gives 100
beginner-friendly explainers tied to dashboard concepts, plus a
currency exposure section and a sweep of polish: drawdown sparkline
hover, per-segment colour on the modal chart, build-health footer
breakdown, and a silenced favicon 404.

### Added
- **Pocket lesson card**: opt-in toggleable card with 100 hand-curated
  3&ndash;4 sentence lessons tied to dashboard concepts &mdash; TWR,
  Sharpe, drawdown, alpha, RSI, sector concentration, the 2&times; ATR
  stop, and more. Category filter chips (Returns / Risk / Behavioural /
  Technical / Analyst / Macro / Trading / Diversification / Dashboard /
  Concepts) narrow the pool; random pick on load + Next-tip rotation.
  Default off; preference persists in `localStorage`.
- **Currency exposure section**: horizontal stacked bar showing
  open-position cost basis split by FX currency, with a per-currency
  legend and a concentration warning above 80%. Useful as a reality
  check against the "I just hold international stocks" framing &mdash;
  most baskets are 70&ndash;90% USD by cost basis even when they look
  diversified by name.
- **Drawdown sparkline hover**: crosshair + dot + date/value tooltip
  mirroring the alpha-sparkline hover.

### Changed
- **Per-segment colour on the modal chart**: each per-ticker modal
  polyline now splits at every zero crossing &mdash; green where the
  position was above its baseline, red where it was below. Interpolation
  lands the colour flip exactly on the baseline. Closed losers that
  recovered (and now total positive) show the period-by-period story at
  a glance rather than hiding under a single "green = profitable" stroke.
- **Build-health footer breakdown**: the footer reads
  `Build: 187/187 tickers (185 held + 2 watch-only)` instead of the bare
  `187/187` that didn't reconcile with the basket size shown elsewhere.

### Fixed
- **Favicon 404**: every page load was logging a single
  `favicon.ico 404` console error. Replaced with an inline SVG
  data-URL favicon (small line-chart glyph in the accent colour);
  no new file, no build pipeline change.

### Internal
- New helpers: `compute_currency_exposure()`, `render_currency_exposure()`,
  `_pocket_lesson_category()`, plus a `segments` field added to the
  `_modal_polyline_d()` payload.
- `POCKET_LESSONS` constant: 100 entries authored inline, retro-tagged
  with categories at module load via title-prefix mapping
  (`POCKET_LESSON_CATEGORIES`).
- README screenshots refreshed end-to-end against v1.8/v1.9 visuals:
  hero, modal, outlook+news, edit-layout. Added pocket-lesson and
  currency-exposure shots.
- `docs/index.html` grows from ~2.18 MB to ~2.44 MB (+12%) on the
  combined feature additions.

### Deferred to later
- Lazy-loaded modal data &mdash; spec written, held for v2.0 audit
  pass when the structural changes can land together (see local
  `lazy-modal-spec.md`).
- Dividend tracking, sector heatmap.

---

## v1.8 — Tighter and broader · 1 June 2026

The polish debt from v1.7 closes (T1&ndash;T3), the universe layer widens
from 150 hand-picked names to the full S&P 500 (T4), and the hero gains
one new visualization &mdash; an inline drawdown sparkline (T5). Then a
follow-up bug-fix pass (B1&ndash;B6) cleared a number of edge cases that
only surfaced once the new data was rendering.

### Added
- **Drawdown sparkline inset**: a thin red line beneath the alpha
  sparkline plots peak-to-trough drawdown at each date over the full
  basket history. The header pill shows the current drawdown (e.g.
  `-0.1%`) and the worst seen (`worst -15.6%`) so the eye can answer
  "how close are we to the prior trough?" without scrolling.
- **YTD return in the hero subtitle**: alongside total + annualized,
  a new YTD % uses true compounding from the last basket value before
  Jan 1 to the latest (`((1+final/100)/(1+jan1/100) - 1) * 100`), not
  the pp-delta on cumulative %.

### Changed
- **Universe expanded to S&P 500**: `universe.csv` jumps from 153
  hand-picked names to the full S&P 500 (503 tickers across 112
  industries). The industry-outlook section now reflects a much richer
  industry mix &mdash; utilities, specialty machinery, regulated
  industrials &mdash; that were thinly sampled before. Class-share
  symbols normalized (`BRK.B` &rarr; `BRK-B`) so yfinance resolves them.
- **Alpha sparkline per-segment color**: the 30-day rolling alpha line
  now splits at every zero crossing &mdash; green above the baseline,
  red below &mdash; instead of using one single color picked from the
  *latest* value. Interpolation lands the color flip exactly on the
  baseline, not the next data point.
- **Closed-position modal label**: closed positions now read
  "between [first buy] and [last sell]" instead of "since [first buy]"
  so the headline % is clearly buy-to-sell, not a current-date return.

### Fixed
- **Hero vs-SPY shading alignment** (T2 / B6): two bugs collapsed
  into one symptom. First, the SPY polyline string was built *before*
  the date-grid remap while the vs-SPY polygons were built *after*, so
  the polyline used SPY's own evenly-spaced x grid and the polygons
  used basket's grid &mdash; visible as a 0.3&ndash;0.5 px gap between
  the dashed gray line and the shading boundary. Remap now runs first.
  Second, when basket has trailing dates SPY doesn't (weekend builds
  where European tickers extend basket past SPY's Friday close), the
  shading had a visible gap at the right edge. The polygon iterator
  now extends one final segment using SPY's last value held flat &mdash;
  the SPY polyline itself stops honestly at its real last data point.
- **Unusual-volume chips disappeared on weekend/holiday builds** (B1):
  the chip detector compared `prices.at[last_idx, tkr]` to
  `prices.at[prev_idx, tkr]`. On a Monday-before-US-open or weekend
  build, `last_idx` falls on a date with no US ticker prices, so all
  32 cache-flagged candidates returned NaN and the row rendered empty.
  Each ticker now uses its own last 2 *valid* prices.
- **Weekly-movers list included closed positions** (B3): the per-week
  top/bottom-5 mover lists walked every ticker in `returns`, but
  `prices.ffill()` keeps a closed ticker's series alive forever. A
  ticker sold in Jan 2025 still topped/bottomed weekly movers for
  every week after. Now each week filters by the ticker's
  `[first_buy_date, last_action_date]` hold window.
- **Modal scroll didn't reset on filter or ticker change** (B5):
  switching from the "Open" filter chip to "Closed" left the table
  scrolled to wherever you'd been; opening a new ticker modal opened
  it scrolled to the previous ticker's last position. Both now reset
  to top.

### Internal
- **Modal chart polylines precomputed in Python** (T1): coordinate
  mapping for the per-ticker modal chart moves out of JS into a fixed
  1000&times;600 viewBox. The SVG uses `preserveAspectRatio="none"`, so
  the browser natively scales to whatever size the modal renders at and
  the resize handler is gone entirely. Saves ~150&ndash;300 ms on first
  modal-open and removes one source of layout coupling. Per-ticker
  payload gains a `chart` field (points string, zero_y, axis ticks);
  the JS render uses these directly with a defensive fallback for any
  ticker missing the field.
- New helpers: `_modal_polyline_d()`, `compute_drawdown_series()`.
- No new dependencies. `docs/index.html` grows from ~1.93 MB to ~2.18 MB
  (+13%) on the precomputed polyline data &mdash; trade for simpler JS.

### Deferred to v1.9
- Drawdown sparkline hover layer (currently static; no date/value
  tooltip on mouseover).
- Per-segment color on the modal chart itself (today still a single
  color based on the position's total return sign).

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
