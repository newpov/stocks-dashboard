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
| **Hidden modules in edit mode look broken** | They render full-width dimmed with `pointer-events:none`. New visitors think the page is glitched. | Render hidden modules as a **slim grey "[hidden — click eye to restore]" placeholder bar** only, not the full content. |
| **"Edit layout" discoverability** | The button is a tiny mono pill in the top-right. Most cloners will never realize the page is rearrangeable. | One-time tooltip / pulse animation on first load (localStorage-gated). |
| **The 5 hero stats are hardcoded** | You can customize the 8 modules but not the 5 stat cards. The top of the page is the most-viewed real estate and the least configurable. | Make stats opt-in/reorderable too, or at minimum let the cloner pick 5 from a registry of (say) 10 in `build.py`. |
| **Build time / yfinance fragility** | 2-3 min per build, occasional 404s (CBUK.L, CYBR). Errors aren't surfaced in the dashboard. | A tiny **"Build health"** footer line: tickers attempted / succeeded / retry-recovered / failed, with the failed tickers listed. Makes silent failures visible. |
| **Modal opens slow on first click** | First open per session has visible jank because `renderBigChart` recomputes layout. | Cheap fix: precompute the chart polyline strings once at build time per ticker — already partially done; finish the job. |
| **Mobile is a second-class citizen** | The 700/900px breakpoints handle the obvious cases but the **detractor table is unusable** on phones — too many columns, all numeric. | A "Top 3 names to review" mobile-only card view, replacing the table at narrow widths. |

---

## 2. What's missing on basket performance

The dashboard answers "how am I doing?" via cumulative TWR + win rate +
drawdown. There's a layer of **risk-adjusted and structural** info missing:

| Missing | Why it matters | How to surface |
|---|---|---|
| **Annualized return** | A +31% return matters very differently if it took 6 months vs 19 months. Annualised should follow calendar year | Replace or augment the hero subtitle: *"+31.7% since Oct '24 · +18.4% annualized"*. |
| **Risk-adjusted: Sharpe, Sortino** | A basket can be +30% with crippling vol (then losing it all next month) or +30% smooth. The dashboard treats those identically. | One new hero stat ("Sharpe 1.4") replacing max drawdown
| **Avg win £ vs avg loss £** | Win rate alone can hide a destroying strategy. A 70% win rate with avg win £80 / avg loss £400 is awful; the dashboard would flatter it. | Replace "Top contributor" hero stat with a "Win/loss magnitude ratio" (e.g., `1.8×` avg-win-to-loss). |
| **Rolling 30 / 90-day alpha vs SPY** | Cumulative outperformance is one number. **Recent** alpha trend is what tells you whether the edge is persistent or front-loaded. | A small sparkline beneath the hero: "30-day rolling alpha vs SPY". |


---

## 3. Missing news → stock adaptation

This is the **biggest gap**. Today the news feed is decoupled from holdings.

| Missing | Why it matters | How to surface |
|---|---|---|

| **Analyst rating-change watch** | Analyst data caches weekly. Diff this build's cache vs last build's = a "ratings moved" feed: *"NVDA target raised $192 → $215"*. | A small new "Rating moves" panel under News. |
| **Unusual-volume alert** | The quant module already computes `vol_ratio`. Today's >2× volume movers should be flagged at the top of the page, not buried in modals. | "Unusual volume today" pinned chip near the hero. |


---

## 5. Sections that could expand on click

The dashboard is currently very read-only. Lots of opportunities:

| Section | Currently | Could become |
|---|---|---|
| **Industry outlook cards** | Read-only top-3 per industry | Click an industry → modal showing **every** ticker from `universe.csv` in that industry (data is already cached, just not surfaced). |
| **Industry attribution rows** | Read-only list | Click a row → modal showing **every open position in that industry** with their individual contribution, cost basis, current return. The "we're +5pp from semis" → "from these 6 specific names". |
| **Diversification → best diversifiers** | Read-only ticker symbols | Click → open the ticker modal (just needs a click handler; `data-ticker` is already there). |
| **Diversification → histogram bars** | Hover tooltip only | Click a bar → list the actual pairs in that correlation bucket. Surfaces "all my +0.50 to +0.75 correlations" — the almost-redundant zone. |
| **Hero chart points** | Hover tooltip | Click a date → list of that day's top movers in the basket. |
