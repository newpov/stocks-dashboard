# Stocks dashboard — audit

A working document. Captures the full assessment delivered during the
2026-05-30 audit session, organised by the five questions asked.

Grading rule used: the dashboard is graded against its **stated purpose**
(the 6 questions in "Why this exists" in the README), not against a generic
"good dashboard" rubric. Several items below are about *removing* /
*consolidating* rather than adding — a tracker dashboard's worst enemy is
noise that crowds out the few items that actually drive a decision.

---

## 1. What's not going well

| Issue | Why it matters | Fix scope |
|---|---|---|
| **Page weight ~1.26 MB** | Most of the bytes are the per-ticker JSON payload (each modal stores its own price history). Slower first paint on mobile / weak networks; risks GitHub Pages caching weirdness. | Lazy-load: ship the table + heads-up cards immediately; defer the `DATA` blob until a modal is opened. Or trim weekly-points to 90 days × 1 per week. |
| **Information overlap on RSI** | RSI now appears in the modal, on Re-entry cards, and is queued for the main table (Phase 4). The user processes the same signal in three places. | Pick **one** authoritative home. Suggestion: keep modal as the deep dive, drop the Re-entry pill (cards stay clean), skip Phase 4 chips. |
| **No "what changed" since last build** | The page regenerates daily but a returning visitor can't tell what's new. They re-scan everything. | A small "since yesterday's build" diff line in the hero meta — e.g. *"+3 positions opened · 1 closed · 2 analyst-target changes · WMT now `Trending up`"*. |
| **Hidden modules in edit mode look broken** | They render full-width dimmed with `pointer-events:none`. New visitors think the page is glitched. | Render hidden modules as a **slim grey "[hidden — click eye to restore]" placeholder bar** only, not the full content. |
| **"Edit layout" discoverability** | The button is a tiny mono pill in the top-right. Most cloners will never realize the page is rearrangeable. | One-time tooltip / pulse animation on first load (localStorage-gated). |
| **News staleness opacity** | If the Cloudflare Worker is down, the static fallback silently shows day-old headlines. The "23h ago" relative-time keeps incrementing, masking staleness. | Always show the **fetch timestamp** explicitly. If `fetched_at > 6 h` and Worker URL is set, add a small "live feed unreachable" hint. |
| **The 5 hero stats are hardcoded** | You can customize the 8 modules but not the 5 stat cards. The top of the page is the most-viewed real estate and the least configurable. | Make stats opt-in/reorderable too, or at minimum let the cloner pick 5 from a registry of (say) 10 in `build.py`. |
| **Build time / yfinance fragility** | 2-3 min per build, occasional 404s (CBUK.L, CYBR). Errors aren't surfaced in the dashboard. | A tiny **"Build health"** footer line: tickers attempted / succeeded / retry-recovered / failed, with the failed tickers listed. Makes silent failures visible. |
| **Modal opens slow on first click** | First open per session has visible jank because `renderBigChart` recomputes layout. | Cheap fix: precompute the chart polyline strings once at build time per ticker — already partially done; finish the job. |
| **Mobile is a second-class citizen** | The 700/900px breakpoints handle the obvious cases but the **detractor table is unusable** on phones — too many columns, all numeric. | A "Top 3 names to review" mobile-only card view, replacing the table at narrow widths. |
| **No keyboard navigation** | All clicks are pointer-only. No `Tab` order on modules; arrow keys aren't wired. | Low-priority but improves accessibility and signals quality. |

---

## 2. What's missing on basket performance

The dashboard answers "how am I doing?" via cumulative TWR + win rate +
drawdown. There's a layer of **risk-adjusted and structural** info missing:

| Missing | Why it matters | How to surface |
|---|---|---|
| **Annualized return** | A +31% return matters very differently if it took 6 months vs 19 months. | Replace or augment the hero subtitle: *"+31.7% since Oct '24 · +18.4% annualized"*. |
| **Risk-adjusted: Sharpe, Sortino** | A basket can be +30% with crippling vol (then losing it all next month) or +30% smooth. The dashboard treats those identically. | One new hero stat ("Sharpe 1.4") replacing the weakest of the existing 5; or a new "Risk" mini-module. |
| **Multi-benchmark** | SPY only is dishonest for a GBP basket. FTSE All-World (VWRL), Nasdaq-100 (QQQ) given the tech tilt, and FTSE 100 (ISF.L for the home bias check) are more useful. | Toggle in the hero legend: cycle benchmark on click. Or stack 3 thin benchmark lines. |
| **Realized vs unrealized P&L** | The split is computed (`returns_df.realized_pnl` / `unrealized_pnl`) but never displayed. Realized is "money in the bank"; unrealized is "what could still disappear". | A small stacked bar above the hero: total = realized + unrealized. Or a stat card. |
| **Avg win £ vs avg loss £** | Win rate alone can hide a destroying strategy. A 70% win rate with avg win £80 / avg loss £400 is awful; the dashboard would flatter it. | Replace "Top contributor" hero stat with a "Win/loss magnitude ratio" (e.g., `1.8×` avg-win-to-loss). |
| **Sector allocation pie / bar** | You have attribution (contribution to return per sector) but not allocation (% of basket weight per sector). Concentration risk is invisible. | A new stacked-bar module or pie. Pairs naturally with the new diversification module. |
| **Currency exposure breakdown** | "% in USD vs GBP vs EUR" affects future FX risk. Hidden in meta. | A small inline stat in the hero. |
| **Underwater-period shading on hero chart** | Max drawdown is a number; *which weeks were spent underwater* and *how long to recover* would be visual. | Light grey wash on the chart for periods where basket < running peak. Pairs with the loss-zone shading shipped 2026-05-30. |
| **Rolling 30 / 90-day alpha vs SPY** | Cumulative outperformance is one number. **Recent** alpha trend is what tells you whether the edge is persistent or front-loaded. | A small sparkline beneath the hero: "30-day rolling alpha vs SPY". |
| **Holding-period stats** | Avg days held by winners vs losers reveals whether you're letting winners run or cutting them early. | A small panel inside Regret tracker. |
| **Best/worst week, month, calendar streaks** | "We just had our worst week since March" is useful context. | A `<details>`-expandable "Calendar records" panel. |

---

## 3. Missing news → stock adaptation

This is the **biggest gap**. Today the news feed is decoupled from holdings.

| Missing | Why it matters | How to surface |
|---|---|---|
| **Per-stock news in the modal** | The modal shows price/quant/FX but no recent news. So clicking into NVDA gives no idea why it moved today. | yfinance has `.news` per ticker (or the Worker per-ticker). Three to five latest headlines as a list in the modal. **(Shipped 2026-05-30.)** |
| **"Holdings only" filter on the global news feed** | Right now the news box is generic finance keywords. A chip "Holdings only" that filters to articles whose title mentions one of the tracked tickers (or a fuzzy match on company name) makes the feed actually yours. | Cheap client-side filter on the existing news payload. |
| **Earnings calendar for holdings** | yfinance returns the next earnings date per ticker. "MSFT reports in 3 days" should be screaming at you. | A new module / hero badge. Could also pin a "this week earnings" sub-row to Re-entry cards. |
| **Analyst rating-change watch** | Analyst data caches weekly. Diff this build's cache vs last build's = a "ratings moved" feed: *"NVDA target raised $192 → $215"*. | A small new "Rating moves" panel under News. |
| **52-week-high / -low alerts** | Hitting new highs (breakouts) or new lows (breakdowns) since the last build = actionable. | A small ribbon at the top of the table: "New 52w highs: NVDA, AVGO, KLAC · New 52w lows: WMT, HII". |
| **Unusual-volume alert** | The quant module already computes `vol_ratio`. Today's >2× volume movers should be flagged at the top of the page, not buried in modals. | "Unusual volume today" pinned chip near the hero. |
| **News sentiment per article** | Harder, but the Worker could attach a simple lexicon-based sentiment score. Aggregate to a "news vibe" gauge for held names. | Optional. Could feel low-signal. Lower priority. |

---

## 4. What could appeal to a GitHub audience

| Idea | Effort | Expected payoff |
|---|---|---|
| **Live demo URL with the demo `transactions.csv`** | Low. CI already runs `build.py`; just also build a `docs/demo/index.html` from the demo log when `log.xlsx` is absent. | Huge. Without a live demo, cloners can't evaluate before forking. |
| **Screenshots in the README** | Low. 4-5 PNG/JPG of the key sections embedded in the existing sections. | Critical. README has zero visuals — this is the #1 conversion barrier. |
| **Animated GIF of the customize-layout feature** | Medium. Record 5s of drag-reorder-hide-reset, encode as GIF. | High. Conveys interactivity better than any prose. |
| **LICENSE file (MIT or Apache-2.0)** | 30 seconds. | Critical for any reuse. Without one, the code is implicitly "all rights reserved." |
| **30-second quickstart code block** | Low. Compress the existing setup section into a fork → enable Pages → push your log block. | Removes friction for the curious. |
| **`CHANGELOG.md`** | Low. Even a hand-written log of "what shipped when" gives newcomers context. The session history is dense; surface the highlights. |
| **A small footer credit on the dashboard itself**: "Built with Claude Opus 4.7 · MIT licensed · [source on GitHub →]" | Low. | Drives traffic from page back to repo. |
| **Acknowledgements / dependencies block** in README | Low. yfinance, Cloudflare Workers, SortableJS, feedparser. | Good citizenship; helps people find adjacent tools. |
| **Contributing guide (1 paragraph)** | Low. | Signals "open to PRs" — a few people will care. |
| **An "Is this for you / not for you?" subsection** | Low. Stops people from forking and being disappointed (e.g., this won't replace a Bloomberg terminal). | Counter-intuitive but improves star-to-actual-use ratio. |
| **A blog post or short essay linked from README** | Medium-high. The dashboard's design choices (TWR, transactional-recency, static-site, custom modules) are essay-worthy. | The repo can grow from "interesting tool" to "case study other people share." |

---

## 5. Sections that could expand on click

The dashboard is currently very read-only. Lots of opportunities:

| Section | Currently | Could become |
|---|---|---|
| **Industry outlook cards** | Read-only top-3 per industry | Click an industry → modal showing **every** ticker from `universe.csv` in that industry (data is already cached, just not surfaced). |
| **Industry attribution rows** | Read-only list | Click a row → modal showing **every open position in that industry** with their individual contribution, cost basis, current return. The "we're +5pp from semis" → "from these 6 specific names". |
| **Diversification → most-correlated pairs** | Read-only list | Click a pair → overlay chart of both tickers' 6-month return, visually confirming the correlation. |
| **Diversification → best diversifiers** | Read-only ticker symbols | Click → open the ticker modal (just needs a click handler; `data-ticker` is already there). |
| **Diversification → histogram bars** | Hover tooltip only | Click a bar → list the actual pairs in that correlation bucket. Surfaces "all my +0.50 to +0.75 correlations" — the almost-redundant zone. |
| **5 hero stat cards** | Static numbers | Click each → inline expand: Win rate → list of recent wins/losses; Max drawdown → date range + which ticker drove it; Top contributor / detractor → per-day contribution sparkline. |
| **News items** | Link out only | Click → expand inline summary if the Worker pre-fetches them. Or: keep external link, add a small "ⓘ" for inline preview. |
| **Hero chart points** | Hover tooltip | Click a date → list of that day's top movers in the basket. |
| **FX bar chart bars** | Hover tooltip | Click a week → which non-GBP holdings contributed how much from FX vs stock. |
| **Regret / escape rows** | Hover-styled, not clickable | Click → modal with full post-exit price chart. |
