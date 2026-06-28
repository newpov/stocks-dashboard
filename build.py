"""Stocks dashboard v4 — per-ticker baseline + weight, basket TWR with SPY overlay.

Schema (tickers.csv):
    ticker,baseline_date,weight
    AAPL,2024-10-21,5.0      # in basket, baseline 21 Oct 2024, weight 5
    GOOGL,,                  # watch-only (no baseline_date, weight defaults to 0)

A weight > 0 puts the stock in the basket. A weight of 0 (or empty cell)
keeps it on the watch-list but excludes it from basket calculations. Baseline
dates default to 21 Oct 2024 for any ticker without a date.

Basket return is computed using Time-Weighted Return (TWR) with renormalization:
at each date t, basket(t) = sum(w_i * ((p_i,t / p_i,base_i) - 1)) / sum(w_i)
over tickers whose baseline_date <= t. This shows the step-and-recover pattern
when new positions enter the basket.

SPY is downloaded as a benchmark overlay, rebased to the earliest baseline_date.

Override industry labels by editing data/meta.csv after first run.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

ROOT = Path(__file__).parent
TRANSACTIONS_CSV = ROOT / "transactions.csv"
LOG_XLSX = ROOT / "log.xlsx"           # Trading 212-style real transaction log
BASKET_SNAPSHOT_CSV = ROOT / "basket.snapshot.csv"   # public, normalized basket for CI
CHANGELOG_MD = ROOT / "CHANGELOG.md"   # v3.0: header version is read from its top entry


def _dashboard_version(path=None) -> str:
    """v3.0: the dashboard version, read from CHANGELOG.md's top '## vX.Y'
    heading (single source of truth — bumping the changelog updates the header
    automatically). Returns '' if the file is missing or has no version heading."""
    path = path or CHANGELOG_MD
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return ""
    m = re.search(r"^##\s*v(\d+\.\d+)", text, re.M)
    return m.group(1) if m else ""

# v2.4 weighting model. "equal" = each position is one unit (privacy-driven,
# the author's default; no monetary scale). "value" = capital-weighted by real
# share quantities (shares x price) — only meaningful when the source actually
# carries real quantities (transactions.csv `shares`, or a quantity column in
# log.xlsx). Override with the WEIGHT_MODE env var or `--weight value`.
WEIGHT_MODE = os.environ.get("WEIGHT_MODE", "equal").strip().lower()
if WEIGHT_MODE not in ("equal", "value"):
    WEIGHT_MODE = "equal"
TICKERS_CSV = ROOT / "tickers.csv"  # legacy fallback
OUT_HTML = ROOT / "docs" / "index.html"
HEAVY_JSON = ROOT / "docs" / "data" / "payload.json"  # v2.0 lazy-modal sidecar
DEMO_OUT_HTML = ROOT / "demo.html"   # standalone self-contained demo at repo root
SORTABLE_VENDOR = ROOT / "docs" / "vendor" / "Sortable.min.js"
CACHE_PARQUET = ROOT / "data" / "prices_cache.parquet"
OHLCV_CACHE = ROOT / "data" / "ohlcv_cache.parquet"  # full OHLCV for ATR / volume metrics
BB_UNIVERSE_OHLCV_CACHE = ROOT / "data" / "bb_universe_ohlcv_cache.parquet"  # Big Brain universe shortlist OHLCV
BENCHMARK_CACHE = ROOT / "data" / "benchmark_cache.parquet"
BENCHMARK2_CACHE = ROOT / "data" / "benchmark2_cache.parquet"
META_CSV = ROOT / "data" / "meta.csv"
WATCHLIST_CSV = ROOT / "watchlist.csv"
ANALYST_CACHE = ROOT / "data" / "analyst_cache.parquet"
# T9: snapshot of the analyst cache from the PREVIOUS build, used to detect
# rating moves (target price changes, recommendation shifts) since last run.
# Written just before ANALYST_CACHE gets overwritten in main().
PRIOR_ANALYST_CACHE = ROOT / "data" / "prior_analyst_cache.parquet"
# v2.8: rolling rating-moves baseline that survives CI. The old mtime-based
# "weekly baseline" broke once CI owned the build -- git checkout resets every
# file's mtime each run, so the baseline never aged. Instead we commit a rolling
# history of daily analyst snapshots and pick a sliding ~2-week baseline from it.
ANALYST_HISTORY = ROOT / "data" / "analyst_history.parquet"
RATING_SEED_META = ROOT / "data" / "rating_seed.json"  # records the cold-start seed date so the UI can flag a provisional baseline
RATING_WINDOW_DAYS = 14        # compare today vs ~this many days ago
RATING_HISTORY_KEEP_DAYS = 18  # prune history a little beyond the window
ANALYST_TTL_DAYS = 7    # refetch a ticker's analyst data when older than this
TICKER_NEWS_CACHE = ROOT / "data" / "ticker_news_cache.parquet"
TICKER_NEWS_TTL_DAYS = 7   # same 7-day cadence as analyst data
TICKER_NEWS_TOP_N = 5      # items stored per ticker

# v1.9 Pocket Lesson: 100 short, beginner-friendly tips tied to concepts the
# dashboard actually surfaces. JS picks one at random on each page load; a
# "Next tip" button rotates without reloading. A topbar toggle hides the card
# entirely (state persisted in localStorage as `pocketLessonOn`).
# Each entry is {"title": str, "body": str (3-4 sentences plain English)}.
# Edit freely; the JS just expects an array of {title, body}.
POCKET_LESSONS: list[dict[str, str]] = [
    {"title": "Time-weighted return (TWR)",
     "body": "TWR measures how your investment skill performed, stripping out the impact of when you added or withdrew money. Adding £1,000 right before a 10% drop shouldn't make your strategy look bad — TWR corrects for that. This dashboard uses TWR so opening a new position doesn't reset the basket's track record."},
    {"title": "Annualised return",
     "body": "Annualised return rescales any return to a per-year figure, letting you compare a 6-month gain to a 3-year one fairly. A 50% return over 2 years is roughly 22.5% annualised, not 25%, because of compounding. Treat single-year annualised numbers with caution — they can over-extrapolate noise."},
    {"title": "Sharpe ratio basics",
     "body": "Sharpe measures return per unit of risk: higher means you got paid more for the volatility you endured. Above 1 is considered solid, above 2 excellent, but the ratio depends on your risk-free rate assumption. It's most useful for comparing strategies, not as an absolute grade."},
    {"title": "Win rate alone misleads",
     "body": "A 90% win rate sounds great until you realise the 10% losers wiped out all the gains. Win rate must be paired with average win size vs average loss size — that's why this dashboard shows both win rate AND win/loss ratio. A 50% win rate with a 3:1 win/loss ratio is far better than 80% at 1:3."},
    {"title": "Win/loss ratio explained",
     "body": "Win/loss ratio is your average winning trade size divided by your average losing trade size. A ratio of 2 means winners are twice the size of losers on average — even a 40% win rate is profitable at that ratio. Pair it with win rate for the full picture."},
    {"title": "Maximum drawdown",
     "body": "Max drawdown is the worst peak-to-trough decline your portfolio has ever experienced. A -25% drawdown means you watched £100k become £75k from some prior high. Recovery time matters as much as depth — a fast 30% drop that bounces back in 3 months is psychologically easier than a 15% drop dragging on for 2 years."},
    {"title": "YTD return",
     "body": "Year-to-date return shows performance from January 1 to today, ignoring everything before. It's useful for tax planning and year-end comparisons but can mislead mid-year — a strong Q1 followed by flat months still shows a great YTD. Always look at YTD alongside longer-horizon numbers."},
    {"title": "Alpha vs benchmark",
     "body": "Alpha is the return you generated above what a passive benchmark like SPY would have delivered. Positive alpha means your stock picks beat the index; negative means you'd have done better just buying the ETF. The 30-day rolling alpha sparkline shows how that gap evolves week by week."},
    {"title": "The loss-zone shading",
     "body": "The subtle red wash below the 0% line marks the loss zone — when the basket dips below the original baseline. Even a brief crossing is immediately visible, so you can quickly tell whether you're in green or red territory. Crossing back above takes more positive momentum than the depth of the dip suggests, because of compounding."},
    {"title": "Why TWR renormalises",
     "body": "When you open a new position, your portfolio's denominator grows but the new stock starts at zero return. TWR renormalises so the line doesn't visually reset just because you added capital. The compounded chain stays smooth — only actual price changes move the line."},
    {"title": "The Δ delta badge",
     "body": "The Δ at the right edge of the hero chart shows your basket's lead (or lag) over SPY in percentage points right now. A Δ +4.2pp means you're 4.2 percentage points ahead of just buying the index. Over years this matters; over weeks it's noise."},
    {"title": "Drawdown vs return",
     "body": "Two strategies can both deliver +15% per year, but one might have a -10% worst drawdown and the other -40%. The first is dramatically easier to live with. The dashboard shows current drawdown next to worst-ever so you can sense how close you are to a familiar low."},
    {"title": "Standard deviation as risk",
     "body": "Investing literature uses standard deviation of returns as the default risk proxy. It captures volatility but not tail risk — a strategy that loses 80% once a decade has the same stdev as one that loses 5% each month. Always look at max drawdown alongside it."},
    {"title": "Sortino vs Sharpe",
     "body": "Sortino is like Sharpe but only penalises downside volatility, ignoring upside swings. It's a better measure for asymmetric strategies — like covered calls — where Sharpe unfairly punishes wins. Most dashboards still use Sharpe because it's the convention."},
    {"title": "Volatility ≠ risk",
     "body": "Volatility measures how much prices swing; risk is the chance of permanent loss. A volatile stock that always recovers (e.g. quality compounders) has low real risk. A flat stock heading toward bankruptcy has low volatility but maximum risk. Don't conflate them."},
    {"title": "Beta basics",
     "body": "Beta measures how much a stock moves relative to the market. Beta of 1.2 means 20% more volatile than SPY; 0.8 means 20% less. Tech stocks typically have beta above 1, utilities below. Beta only matters if you're hedging or constructing factor-based portfolios."},
    {"title": "Correlation basics",
     "body": "Correlation runs from -1 (move opposite) through 0 (independent) to +1 (move together). For portfolio diversification, lower or negative correlations between holdings reduces overall volatility. Two stocks at 0.9 correlation are essentially the same position from a risk lens."},
    {"title": "The 30-stock myth",
     "body": "Academic research suggests ~30 stocks captures most of the diversification benefit available in a single market. Beyond that, adding more names mainly adds tracking-error noise without lowering risk. But concentration in one sector across 30 names doesn't diversify at all."},
    {"title": "Sector concentration",
     "body": "If 40% of your basket is tech, your portfolio's fate is essentially the Nasdaq's — no matter how many individual names you hold. The sector chip filter on the main table lets you sense this concentration at a glance. Diversifying within tech helps slightly, but not much."},
    {"title": "Best diversifiers",
     "body": "The Best diversifiers panel highlights stocks in your basket with the lowest correlation to the rest. Keeping a few of these — even ones you find boring — is what stops your whole basket moving as one body when the market sells off."},
    {"title": "Most-correlated pairs",
     "body": "The Most correlated pairs list flags positions that essentially move as one. Holding both gives you no diversification benefit — you've effectively got a double-weight position in a single bet. Consider whether you really need both."},
    {"title": "RSI 14-day",
     "body": "Relative Strength Index measures momentum on a 0-100 scale from the last 14 days of price action. Above 70 is conventionally overbought; below 30 oversold. RSI works well in range-bound markets and badly in strong trends."},
    {"title": "Why RSI fails in trends",
     "body": "In a strong uptrend, RSI can stay above 70 for weeks while the price keeps climbing. Selling on an overbought signal in 2020 or 2024 would have cost a fortune. Pair RSI with trend context — like distance from the 200-day average."},
    {"title": "200-day moving average",
     "body": "Price above the 200-day SMA is the classic in-an-uptrend signal; below it is downtrend. The 200-day is slow but reliable — it filters short-term noise and rarely flips. The vs 200d stat in the modal shows how far above or below each position sits."},
    {"title": "ATR (Average True Range)",
     "body": "ATR measures the typical price range over the last 14 days, in absolute terms. It's the most honest volatility measure for individual stocks — not annualised, not normalised, just how much does this thing move on a normal day. Useful for setting stops at 2× ATR."},
    {"title": "52-week range position",
     "body": "This stat shows where the current price sits between the 52-week low (0%) and high (100%). Names at 90%+ are near multi-year highs — breakout candidates or due for cool-off. Names below 25% are deep in their range: value or value-trap, context decides."},
    {"title": "Volume ratio",
     "body": "Volume ratio compares today's volume to the 63-day average. Above 2× often signals news — earnings, M&A, rating change, macro event. The dashboard surfaces these as unusual-volume chips. Worth checking why the move happened before reacting."},
    {"title": "Analyst target prices",
     "body": "Analyst targets are 12-month price forecasts, averaged across the analysts covering a stock. Aggregated targets are useful as a sanity check, but individual targets are notoriously biased — sell-side analysts rarely issue sell ratings on their own coverage. Read targets as rough consensus, not gospel."},
    {"title": "The 1-5 recommendation scale",
     "body": "Most data sources convert analyst ratings to a 1-5 scale: 1 = Strong Buy, 5 = Strong Sell, 3 = Hold. A mean below 2 means broad bullish consensus; above 3 means caution. Few stocks ever cross above 3 because analysts are structurally biased to Hold rather than Sell."},
    {"title": "Buy-rating bias",
     "body": "Sell-side analysts work for banks that want their corporate clients (the rated companies) to remain customers. Issuing a Sell rating burns those relationships. Result: ~60% of all analyst ratings are Buy, ~35% Hold, ~5% Sell. Adjust your interpretation accordingly."},
    {"title": "Number of analysts matters",
     "body": "A 30% upside from 25 analysts is more meaningful than the same target from 2 analysts on a thinly-covered small-cap. The Analyst column shows coverage depth — a thin number means take any consensus with extra salt."},
    {"title": "Coverage gaps",
     "body": "Some small-cap and foreign-listed names have zero analyst coverage. That doesn't mean they're bad — it means the institutional buy-side never funded research on them. Some of the best long-term opportunities historically came from this overlooked pool, but you're on your own for analysis."},
    {"title": "Upside % interpretation",
     "body": "Upside = (target price ÷ current price - 1) × 100. A 40% upside on a tightly-followed mega-cap is a strong signal; the same upside on a thinly-covered micro-cap may just reflect optimistic 12-month-out modelling. Pair with number of analysts and recent rating changes."},
    {"title": "Rating moves > ratings",
     "body": "A stock at Hold that just got upgraded from Sell tells you more than one perpetually rated Buy. The Rating moves panel surfaces these changes — analyst opinion shifts often lead price by weeks. Persistent ratings carry little information; recent changes do."},
    {"title": "Market cap tiers",
     "body": "Mega-cap: > $200B. Large: $10B–$200B. Mid: $2B–$10B. Small: < $2B. Different tiers behave differently — small-caps outperform over decades but with much higher volatility and drawdowns. The cap tier badge on each ticker keeps this visible."},
    {"title": "GICS sectors briefly",
     "body": "Global Industry Classification Standard splits the equity market into 11 sectors: Tech, Healthcare, Financials, Communications, Discretionary, Staples, Industrials, Energy, Materials, Utilities, Real Estate. Each behaves differently in different economic regimes — utilities outperform in recessions, tech in growth."},
    {"title": "Industry vs sector",
     "body": "Sector is the top level (e.g. Technology); industry is the sub-classification (e.g. Software, Semiconductors). The dashboard groups by industry where possible because behaviour is more uniform — semiconductor stocks correlate more with each other than with consumer-tech."},
    {"title": "FX attribution",
     "body": "For non-GBP holdings, your return splits into the stock's native return + the currency move against GBP. If a US stock gained 10% but the dollar weakened 5% against the pound, you actually earned ~4.5% in GBP terms. The FX delta row in the modal makes this explicit."},
    {"title": "Currency exposure",
     "body": "Holding US stocks gives you dollar exposure whether you want it or not. A 50% USD allocation means a stronger pound costs you even if every stock did fine. The FX bar at the bottom of the hero chart shows GBP/USD week by week — a visual reminder of currency drift."},
    {"title": "Realised vs unrealised P&L",
     "body": "Realised P&L is locked in from completed sales — it's already in your pocket (or out of it). Unrealised P&L is paper gain or loss on still-open positions, which can swing wildly. Both matter for tax (only realised counts) but only realised matters for actual cash."},
    {"title": "Post-exit move",
     "body": "When you sell, the stock keeps moving without you. The post-exit field shows what it did since: positive means you missed gains (regret), negative means you escaped before a fall (lucky escape). Both are educational — neither means you made a mistake in real-time."},
    {"title": "Renormalised TWR explained",
     "body": "Time-weighted return chains period-by-period returns multiplicatively so each window's gain compounds. Adding new positions starts a new chain link at zero — the basket as a whole keeps compounding from the original baseline. This isolates portfolio skill from cash-flow timing."},
    {"title": "Equal weight vs market cap weight",
     "body": "Equal weight means each position is the same size; market-cap weight means bigger companies get more capital. Equal-weight historically outperforms long-term (you're overweight small/mid caps) but with higher drawdowns. The dashboard's weight field shows your actual allocation."},
    {"title": "The 2% rule",
     "body": "A common rule: don't put more than 2% of your portfolio in any single position. This caps the damage from any single name going to zero. 2% is conservative for high-conviction picks; some allocators run 5%-10% on their best ideas, accepting higher single-name risk."},
    {"title": "Averaging down vs DCA",
     "body": "Averaging down = buying more as the price falls, lowering your cost basis. Dollar-cost averaging = buying fixed amounts on schedule regardless of price. Averaging down only works if your thesis remains intact; DCA is mechanical and doesn't require ongoing judgement."},
    {"title": "Selling winners too early",
     "body": "The most common psychological mistake: cutting profits at +10% while letting losses run to -40%. Letting winners compound is mathematically how portfolios get rich. The lucky escapes / regrets table helps confront the asymmetry directly."},
    {"title": "Holding losers too long",
     "body": "The mirror problem: refusing to sell a loser because that locks in the loss. The loss already happened; selling just acknowledges it. Anchoring on your buy price is the #1 way amateur investors underperform — focus on whether the thesis still holds, not your cost basis."},
    {"title": "Anchoring on buy price",
     "body": "Your buy price is meaningless to the future of the stock. The market doesn't know or care what you paid. When deciding whether to keep or sell, ask: would I buy this stock at today's price with fresh capital? If no, sell — your buy price is sunk."},
    {"title": "Confirmation bias",
     "body": "Once you own a stock, you'll find yourself reading more bullish articles about it. This is normal but dangerous — you'll miss disconfirming evidence until it's too late. Force yourself to actively seek the bear case on your largest holdings every quarter."},
    {"title": "FOMO warning",
     "body": "Stocks that have just rallied 50% feel safest to buy *because* they've rallied — the momentum is visible and exciting. They're actually the most dangerous moments. The 52-week-range stat (near-high vs mid-range) is partly a FOMO check."},
    {"title": "Recency bias",
     "body": "Whatever happened last month feels more important than it is. A stock that's down 20% in 4 weeks feels broken even if its 3-year chart looks fine. The 1m, 3m, YTD, total returns side-by-side in the modal exist to fight recency bias — always check multiple horizons."},
    {"title": "Loss aversion",
     "body": "Behavioural research shows people feel a loss roughly twice as intensely as the equivalent gain. This explains why investors panic-sell at lows and refuse to take losses gracefully. The dashboard's risk metrics (Sharpe, drawdown) help you pre-commit to risk levels before emotion takes over."},
    {"title": "Spread and slippage",
     "body": "Buying at market price means you pay the ask, not the mid-price. For thinly-traded small-caps the spread can be 1%+ of the trade. Limit orders avoid spread cost but risk not filling. For weekly-or-less trading frequency, spread cost is a real drag on returns."},
    {"title": "Stop loss basics",
     "body": "A stop loss is a pre-committed price at which you'll sell, no matter how much you want to hold. The exit strategy table in the dashboard suggests 2× ATR stops — twice the typical daily range. This survives normal noise but cuts losses on real breakdowns."},
    {"title": "2× ATR stop",
     "body": "Setting your stop at 2× ATR below the recent price means you tolerate normal volatility but exit on outsized moves. ATR is honest about how much the stock typically moves, so a 2× ATR stop adapts to volatile vs calm stocks automatically. Static % stops don't."},
    {"title": "Trailing stop",
     "body": "A trailing stop moves up as the price moves up but never down. Combined with 2× ATR, it lets winners run while protecting downside. Trailing stops are the simplest way to mechanise cut losses, let winners ride — the hardest single behaviour in trading."},
    {"title": "Rebalancing",
     "body": "Rebalancing = selling some of your winners to buy more of your laggards, restoring target weights. It's counter-intuitive but historically adds 0.5-1% per year because you're selling high and buying low mechanically. Most do this annually or semi-annually."},
    {"title": "Concentration limits",
     "body": "Even with high conviction, hard-capping any single position at e.g. 10% protects against single-name disasters. The S&P 500 itself rebalances away from over-concentration in any single name. Your portfolio dashboard should make concentration obvious — that's why top contributor is a default stat."},
    {"title": "Cash position",
     "body": "Cash isn't dead weight — it's an option. Holding 5-10% cash means you can act on opportunities when they appear without selling existing positions. Cash also dampens volatility, improving Sharpe ratio. Don't treat 100% invested as the default."},
    {"title": "Time horizon matters",
     "body": "A 10-year horizon means you can ignore monthly noise and tolerate larger drawdowns. A 6-month horizon means liquidity, low volatility, and avoiding tail risk matter most. Mismatched horizon = wrong portfolio. Your stop-loss aggressiveness should match your time horizon."},
    {"title": "Interest rates and stocks",
     "body": "Higher rates make bonds more attractive vs stocks, especially growth stocks whose future earnings get discounted more heavily. Lower rates do the opposite — they're a structural tailwind for stocks, especially growth. The central bank posture matters enormously."},
    {"title": "Inflation impact",
     "body": "Moderate inflation (2-3%) is fine for stocks long-term — companies pass costs through. High unexpected inflation hurts growth stocks (future earnings devalued) and helps commodity, energy, and real-estate names. Inflation-sensitive sectors deserve a place in any long-horizon basket."},
    {"title": "Yield curve briefly",
     "body": "The yield curve plots interest rates across bond maturities (3m, 1y, 5y, 10y, 30y). Normally upward-sloping (longer bonds yield more). When it inverts (short rates higher than long), a recession has historically followed within 18 months. Watch it as a slow macro signal."},
    {"title": "Recession indicators",
     "body": "Reliable historical recession indicators: inverted yield curve, rising unemployment (Sahm rule: +0.5pp from low), falling PMI below 50, and credit spreads widening. Stocks often peak 6-12 months before recessions actually start. Lead times are long, false signals exist."},
    {"title": "Geopolitical risk",
     "body": "Wars, sanctions, supply-chain disruptions, and trade tariffs all hit stocks — but rarely permanently. Markets generally recover within 6-18 months of major geopolitical shocks. Don't sell into the panic; rebalance toward the names most unfairly punished."},
    {"title": "The WATCH tag",
     "body": "Watchlist tickers are stocks you're tracking but don't own. They show up in the modal with their 12-month return for context but contribute zero weight to your basket stats. The watchlist is your idea funnel — keep it short and curated."},
    {"title": "Closed positions in stats",
     "body": "Win rate, win/loss ratio, and exit-strategy metrics all use closed positions to give you historical performance data. Open positions don't count yet because the outcome is unknown. The dashboard's closed-position filter shows just these — useful for honest self-assessment."},
    {"title": "Why some positions show CLOSED",
     "body": "A position is classified closed when the most recent transaction is a SELL. This is a transactional-recency rule — robust to partial exits and to multiple open/close cycles on the same ticker. It doesn't require knowing exact share quantities."},
    {"title": "The rolling alpha sparkline",
     "body": "The thin line under the hero chart shows your basket's excess return vs SPY over a rolling 30-day window. Green segments = you beat SPY; red = you trailed. Persistent green means your stock picks are adding real value; persistent red means you might as well own the index."},
    {"title": "Drawdown sparkline",
     "body": "The red drawdown sparkline shows how far below the prior peak the basket sits at each date. A nearly-flat line at 0% means a smooth ride; deep red excursions are stress tests you survived. Pair the current drawdown with the worst-seen number to gauge familiarity."},
    {"title": "Weekly movers",
     "body": "Click any weekly point on the hero chart to see that week's biggest up and down movers in your basket. Most weeks are dominated by 2-3 names; concentration of P&L into a few stocks is normal but worth noticing. If the same name dominates week after week, it's becoming your portfolio."},
    {"title": "Industry outlook",
     "body": "The industry-outlook section shows 12-month returns across an S&P 500-wide universe, grouped by industry. It tells you which industries are hot or cold — useful for spotting where the market is allocating attention. Industries already up 50%+ tend to mean-revert; bottom-quartile industries often lead the next cycle."},
    {"title": "Top contributor / detractor",
     "body": "These stats name your basket's single biggest positive and negative driver. Healthy diversification looks like contributions spread across many names. If your top contributor is +12pp while everyone else is +1pp each, your basket's fate is really one stock's fate."},
    {"title": "Renormalised contribution",
     "body": "A position's contribution = its return × its weight in the portfolio. A 100% winner at 1% weight contributes the same as a 10% winner at 10% weight. The attribution breakdown makes this math obvious — you can't ignore weight when judging position impact."},
    {"title": "Pyramid building",
     "body": "Pyramid building = adding to winners as they prove themselves, not at the start. Risk is small early (small position, easy stop) but you compound into the names actually working. The opposite of averaging down — and historically more profitable in trend-following styles."},
    {"title": "Conviction sizing",
     "body": "Some allocators size positions by conviction: 5% for high-conviction ideas, 2% for medium, 1% for exploratory. This makes failure cheap and success meaningful. Equal-weighting everything means your conviction effectively doesn't show up in your portfolio."},
    {"title": "Trading commissions",
     "body": "Most retail brokers are commission-free, but spread, slippage, and FX conversion costs still apply. International trades often cost 0.5-1% in FX alone. Always check the effective cost of a trade, not just the headline commission-free claim."},
    {"title": "Cost basis methods",
     "body": "FIFO (first-in-first-out) is the default cost basis in most jurisdictions: when you sell, the oldest shares leave first. Some allow LIFO or specific-lot identification — these matter for tax optimisation, especially in volatile names where lot selection can swing realised gains by 30%+."},
    {"title": "Capital gains short vs long",
     "body": "In most countries, capital gains tax is lower on positions held longer than 12 months. Selling at 11 months instead of 13 months can cost you 10-20% extra in tax. Worth checking your jurisdiction's specific threshold before exit-timing decisions."},
    {"title": "Tax-loss harvesting",
     "body": "Selling losers to crystallise capital losses you can offset against gains is tax-loss harvesting. UK and US both allow this, with rules around wash sales (re-buying immediately disqualifies the loss). End of tax year is the usual season — but it's a year-round opportunity."},
    {"title": "The realised P&L field",
     "body": "Realised P&L is the locked-in profit from closed positions: (avg sell price - avg buy price) × shares sold. Once you've sold, this number doesn't move. It's the cleanest measure of historical trading skill — separate from open-position swings that are still subject to market noise."},
    {"title": "Dividend yield",
     "body": "Dividend yield = annual dividend / current price. Yields above 5-6% are unusual and often a red flag (price may have collapsed). Stable dividend payers (utilities, consumer staples) yield 2-4%; growth stocks often pay nothing. This dashboard doesn't model dividends — assume them as bonus return."},
    {"title": "The Top 10 filter",
     "body": "The Top 10 filter on the main table shows your 10 best-returning positions. Useful for confirming your conviction names are actually delivering. If your best ideas aren't in your top 10, your conviction sizing might not be matching reality."},
    {"title": "The Losers filter",
     "body": "Losers filter = open positions with negative return. These deserve more attention than winners: a thesis broken? Sector rotation against you? An earnings miss? Going through losers once a month and asking why is this here is one of the highest-ROI portfolio reviews."},
    {"title": "The sector chip filter",
     "body": "The sector chip lets you focus on one slice of the basket at a time. Use it to spot-check whether your tech allocation has overconcentrated, or whether your defensive sleeve is doing its job in a drawdown. Concentration is most visible when you isolate one sector."},
    {"title": "What renormalised TWR means",
     "body": "Time-weighted return where opening a new position doesn't reset the line. Each existing position keeps compounding; new positions start their own chain. The overall basket is the weighted average — the fairest measure of multi-period multi-position portfolio skill."},
    {"title": "Why the hero compares to SPY",
     "body": "S&P 500 (SPY) is the default benchmark because it's the cheapest, most liquid, most-held diversified US equity exposure. If your stock picks can't beat SPY over a few years, you'd be better off just owning the ETF. The Δ delta badge keeps that comparison front-and-centre."},
    {"title": "Basket leading SPY temporarily",
     "body": "Even passive investors who own SPY can outperform for a year through luck. The relevant question: does your basket beat SPY on a Sharpe-adjusted basis over 3+ years? Single-year alpha is mostly noise; multi-year alpha is some signal."},
    {"title": "The Buy rating's silent contract",
     "body": "When an analyst issues a Buy, they're effectively saying this stock will return 10%+ over 12 months relative to alternatives. Read the rating, then check whether the stated upside (target ÷ current) actually delivers that. Sometimes a Buy has only 3% implied upside — the rating doesn't match the math."},
    {"title": "Why Strong Sell is rare",
     "body": "Strong Sell ratings damage an analyst's relationship with the rated company. Companies retaliate by cutting access to management, conferences, and earnings calls. So analysts use euphemisms: Underperform, Hold (when they really mean sell), or coverage drop. Read between the lines."},
    {"title": "The news section's purpose",
     "body": "The news feed isn't a trading signal — it's situational awareness. Markets often move on news, but by the time it's in headlines, it's priced in. Use the news to know why things are moving, not as a buy/sell trigger."},
    {"title": "Per-ticker news in the modal",
     "body": "Each ticker modal shows up to 5 recent headlines about that specific company. Useful when something moves and you want to know if there's a known catalyst — earnings beat, downgrade, merger rumour. Absence of news + a big move often means index/sector flow, not company-specific."},
    {"title": "The volume signal",
     "body": "The Volume stat in the modal shows today's volume ÷ 63-day average. Above 1.5× without obvious news often precedes a price move. The unusual-volume chips surface the most extreme examples — worth checking why before deciding what to do."},
    {"title": "The breakout pattern",
     "body": "A breakout = price closes above a multi-month resistance level on heavy volume. Combined with a 52-week-high stat near 100% and elevated volume ratio, breakouts have been one of the most robust technical patterns historically. But failed breakouts are common — confirmation matters."},
    {"title": "The oversold bounce",
     "body": "RSI below 30 combined with proximity to 52-week-low historically yields short-term bounces. Doesn't mean the long-term trend is up — just that mean-reversion plays a role in the short term. The re-entry ideas section identifies these in your historical holdings."},
    {"title": "Survivorship bias warning",
     "body": "Past performance studies have a survivorship-bias problem: they include companies that still exist but exclude bankruptcies. Long-run stock returns reported as the market returns 10% implicitly assume you avoided the disasters. Your portfolio is exposed to disasters academic studies hide."},
    {"title": "The compound interest question",
     "body": "At 7% per year, money doubles every ~10 years (rule of 72). At 10%, every 7 years. The difference between 7% and 10% annualised return doesn't sound dramatic, but over 30 years it's the difference between 8× and 17× — more than twice as much money."},
    {"title": "Fees compound too",
     "body": "A 1% fee per year sounds small. Over 30 years it compounds to ~26% of final value. Funds with 2% expense ratios end up with roughly half the final value of equivalent 0.05% index funds, even if gross returns match. Fees are the silent return killer."},
    {"title": "Time in the market",
     "body": "Studies of market timers find that missing the 10 best days over decades cuts long-term returns dramatically. Those best days often cluster right after the worst days. Lesson: it's almost impossible to time exits well, so disciplined buy-and-hold tends to beat trying to dodge."},
    {"title": "What this dashboard won't tell you",
     "body": "No metric on this dashboard tells you whether to buy or sell. They're decision aids: context to make the call yourself, then accountability for the choice. The most valuable thing any portfolio tool does is force you to look at uncomfortable numbers — drawdown, losers, FX drag — that emotion would otherwise hide."},
]

# v1.9 #7: tag each tip with a category for the filter chips. Categories are
# tied to dashboard sections so a user can say "I'm looking at the modal --
# what's there to learn about quant signals?" and narrow the pool. Mapping is
# title-prefix-based to keep edits surgical -- adding a new tip means picking
# the matching prefix below or extending the list. Anything unmapped falls
# into "Concepts" which is fine for the catch-all framing tips.
POCKET_LESSON_CATEGORIES: dict[str, list[str]] = {
    "Returns": [
        "Time-weighted return", "Annualised return", "YTD return", "Alpha vs",
        "Why TWR", "Renormalised TWR", "What renormalised TWR",
        "The Δ delta", "Realised vs unrealised", "Post-exit move",
        "The compound interest", "The realised P&L",
    ],
    "Risk": [
        "Sharpe", "Maximum drawdown", "Drawdown vs", "Drawdown sparkline",
        "Standard deviation", "Sortino", "Volatility", "Beta",
        "Survivorship bias",
    ],
    "Diversification": [
        "Correlation", "The 30-stock", "Sector concentration", "Best diversifiers",
        "Most-correlated", "Equal weight", "The 2% rule", "Concentration limits",
        "Cash position",
    ],
    "Technical": [
        "RSI", "Why RSI fails", "200-day", "ATR", "52-week range",
        "Volume ratio", "The volume signal", "The breakout", "The oversold",
        "The rolling alpha sparkline",
    ],
    "Analyst": [
        "Analyst target", "The 1-5 recommendation", "Buy-rating", "Number of analysts",
        "Coverage gaps", "Upside %", "Rating moves", "The Buy rating",
        "Why Strong Sell", "The Top 10", "The Losers",
    ],
    "Macro": [
        "Interest rates", "Inflation", "Yield curve", "Recession",
        "Geopolitical", "FX attribution", "Currency exposure",
        "Market cap tiers", "GICS sectors", "Industry vs sector",
    ],
    "Behavioural": [
        "Win rate alone", "Win/loss ratio", "Averaging down", "Selling winners",
        "Holding losers", "Anchoring", "Confirmation bias", "FOMO",
        "Recency bias", "Loss aversion", "Pyramid building", "Conviction sizing",
    ],
    "Trading": [
        "Spread and slippage", "Stop loss", "2× ATR", "Trailing stop",
        "Rebalancing", "Time horizon", "Trading commissions",
        "Cost basis", "Capital gains", "Tax-loss harvesting", "Dividend yield",
    ],
    "Dashboard": [
        "The loss-zone", "The WATCH", "Closed positions", "Why some positions",
        "Weekly movers", "Industry outlook", "Top contributor",
        "Renormalised contribution", "Why the hero", "Basket leading SPY",
        "The news section", "Per-ticker news", "The sector chip filter",
        "What this dashboard",
    ],
}


def _pocket_lesson_category(title: str) -> str:
    """Return the category bucket for a tip title via prefix match."""
    for cat, prefixes in POCKET_LESSON_CATEGORIES.items():
        for prefix in prefixes:
            if title.startswith(prefix):
                return cat
    return "Concepts"


# Inject the category into each lesson dict. Done at module load so the JS
# payload always carries it; tip authoring stays a single edit to POCKET_LESSONS.
for _lesson in POCKET_LESSONS:
    _lesson["category"] = _pocket_lesson_category(_lesson["title"])
del _lesson

# v2.1 / v2.4: 100-question quiz pool (practical tone) covering finance knowledge
# COMPLEMENTARY to the dashboard's own teaching — Pocket Lesson covers what the
# dashboard surfaces; QUIZ_POOL covers what it doesn't: market mechanics,
# corporate actions, fixed income / commodities / REITs, derivatives, financial
# history & regulation. 20 questions per category (the original 10 medium, plus
# v2.4's 5 entry-level + 5 hard), all 3-option multiple choice with a 1-sentence
# explanation on reveal.
# Schema (per entry):
#   id          - stable integer id (used by the seen-set in localStorage)
#   category    - one of the 5 category labels (used by category filter chips)
#   format      - "cloze" (fill-in-blank) or "direct" (full question)
#   question    - the question text (ends with `___` for cloze)
#   options     - list of 3 strings; one is correct, two are plausible distractors
#   correct     - 0-indexed position of the correct option
#   explanation - 1-sentence reveal text shown after the user answers
#   difficulty  - "entry" | "medium" | "hard" (v2.4; original 50 are implicitly
#                 "medium" and omit the field — default to medium when absent)
QUIZ_POOL: list[dict] = [
    # ---- Market mechanics ----
    {"id": 1, "category": "Market mechanics", "format": "cloze",
     "question": "T+1 settlement, adopted by US exchanges in May 2024, means a stock trade settles ___.",
     "options": ["The same business day (T+0)", "One business day after execution", "Two business days after execution"],
     "correct": 1,
     "explanation": "Before May 2024 US settlement was T+2; the shift to T+1 cuts counterparty risk but tightens cash-management timelines on both sides of the trade."},
    {"id": 2, "category": "Market mechanics", "format": "direct",
     "question": "A market maker's bid-ask spread on a thinly-traded stock is typically wider than on a heavily-traded one because:",
     "options": ["They charge higher commissions on low-volume names", "Inventory risk grows when they can't quickly offload positions", "Exchange listing fees scale with daily volume"],
     "correct": 1,
     "explanation": "Wider spreads compensate the market maker for holding inventory in low-liquidity names where unwinding a position can take days rather than seconds."},
    {"id": 11, "category": "Market mechanics", "format": "cloze",
     "question": "When you place a 'limit buy' order at £50 on a stock trading at £52, your order ___.",
     "options": ["Executes immediately at the next available offer", "Waits in the order book until the price drops to £50 or lower", "Cancels automatically if the stock doesn't reach £50 within 1 minute"],
     "correct": 1,
     "explanation": "Limit orders set a maximum acceptable price for buys; they trade off 'may not execute' for 'won't pay more than you specified'."},
    {"id": 12, "category": "Market mechanics", "format": "direct",
     "question": "After-hours trading carries elevated risk primarily because:",
     "options": ["Exchanges charge higher fees outside regular session", "Spreads widen and liquidity thins, amplifying price impact", "Trades don't settle until the next business day"],
     "correct": 1,
     "explanation": "After-hours sessions have a fraction of regular-hours volume; small orders can move prices significantly because counterparties are scarce."},
    {"id": 13, "category": "Market mechanics", "format": "cloze",
     "question": "Dark pools are private trading venues that ___.",
     "options": ["Operate only outside regular market hours", "Hide order details until after execution to reduce market impact", "Exclude retail investors by regulation"],
     "correct": 1,
     "explanation": "Large institutional orders route through dark pools to avoid signaling intent; the trade prints publicly only after execution."},
    {"id": 14, "category": "Market mechanics", "format": "direct",
     "question": "To short a stock in the US, your broker typically must first:",
     "options": ["Receive regulatory approval from the SEC for each transaction", "Locate shares available to borrow", "Hold the stock in their own proprietary inventory"],
     "correct": 1,
     "explanation": "Regulation SHO's 'locate' requirement prevents naked shorting; brokers must confirm borrow availability before allowing the short sale."},
    {"id": 15, "category": "Market mechanics", "format": "cloze",
     "question": "A margin call is triggered when ___.",
     "options": ["Your broker decides to reduce leverage limits unilaterally", "Your account equity falls below the maintenance margin requirement", "You exceed the maximum number of trades allowed per day"],
     "correct": 1,
     "explanation": "Margin calls force you to add cash, deposit securities, or close positions to restore equity above the maintenance threshold (typically 25-30% for US equities)."},
    {"id": 16, "category": "Market mechanics", "format": "direct",
     "question": "The key difference between a stop-loss and a stop-limit order is that:",
     "options": ["Stop-loss orders convert to market orders once triggered", "Stop-limit orders execute instantly at any price", "Stop-loss orders are only available during regular hours"],
     "correct": 0,
     "explanation": "Once triggered, a stop-loss becomes a market order (fills at next available price, even if much worse); a stop-limit becomes a limit order (may not fill if price gaps through)."},
    {"id": 17, "category": "Market mechanics", "format": "cloze",
     "question": "In an IPO, the underwriter's 'greenshoe' option lets them ___.",
     "options": ["Cancel the IPO if subscription falls below a threshold", "Sell up to 15% more shares than originally planned if demand is strong", "Lock up insider shares for an extended period"],
     "correct": 1,
     "explanation": "The greenshoe (overallotment option) stabilizes post-IPO prices by giving underwriters a way to cover their short position when the stock trades above the offer price."},
    {"id": 18, "category": "Market mechanics", "format": "direct",
     "question": "NYSE differs structurally from NASDAQ primarily because:",
     "options": ["NYSE uses Designated Market Makers; NASDAQ uses competing dealers", "NASDAQ requires higher minimum listing standards than NYSE", "NYSE only lists US companies; NASDAQ lists international"],
     "correct": 0,
     "explanation": "NYSE has a single Designated Market Maker per stock providing continuous two-sided quotes; NASDAQ is a network of multiple competing market makers per security."},
    # ---- Corporate actions ----
    {"id": 3, "category": "Corporate actions", "format": "direct",
     "question": "After a 1-for-4 reverse stock split, you held 200 shares at £5 each (£1,000 total). Post-split your position is:",
     "options": ["50 shares at £20 each", "800 shares at £1.25 each", "200 shares at £20 each"],
     "correct": 0,
     "explanation": "Reverse splits consolidate share count while proportionally raising price-per-share; total position value is unchanged at the moment of the split."},
    {"id": 4, "category": "Corporate actions", "format": "cloze",
     "question": "A 'special dividend' differs from a regular dividend mainly because ___.",
     "options": ["It's paid in stock rather than cash", "It's a one-time distribution outside the normal quarterly schedule", "It's taxed at a higher rate than regular dividends"],
     "correct": 1,
     "explanation": "Special dividends typically come from one-time events (asset sale, accumulated cash) and don't signal a permanent increase in the recurring yield."},
    {"id": 19, "category": "Corporate actions", "format": "cloze",
     "question": "After a 3-for-1 forward stock split, your 100-share position at £60 (£6,000 cost basis) becomes ___.",
     "options": ["300 shares at £20 with cost basis £20/share", "100 shares at £180 with cost basis £60/share", "300 shares at £60 with cost basis £20/share"],
     "correct": 0,
     "explanation": "Forward splits multiply share count and divide price-per-share proportionally; total position value and total cost basis are unchanged, only per-share basis changes."},
    {"id": 20, "category": "Corporate actions", "format": "direct",
     "question": "When Company A spins off Subsidiary B as a separate public company, your cost basis is typically:",
     "options": ["Reallocated between A and B based on their relative fair market values", "Reset to zero on A and applied entirely to B", "Unchanged on A, with B's basis set to first-day closing price"],
     "correct": 0,
     "explanation": "Cost basis is split proportionally between the surviving entity and spinoff per the IRS-published allocation (often relative market caps on the distribution date)."},
    {"id": 21, "category": "Corporate actions", "format": "cloze",
     "question": "In an all-cash M&A deal at £50/share, your 100 shares of the target company are ___.",
     "options": ["Automatically converted to acquirer shares at the exchange ratio", "Cashed out at £50/share, typically triggering a taxable event", "Held in escrow until the deal closes 12 months later"],
     "correct": 1,
     "explanation": "All-cash deals are realization events; your basis is compared to cash received and any gain/loss is reportable in that tax year, regardless of whether you wanted to sell."},
    {"id": 22, "category": "Corporate actions", "format": "direct",
     "question": "A rights issue at a 20% discount to current market typically causes:",
     "options": ["Existing shareholders to receive new shares automatically as a stock dividend", "The stock to trade lower after issuance, even for non-participants", "The company's total equity to decrease by 20%"],
     "correct": 1,
     "explanation": "Rights issues dilute share count; non-participating shareholders see their stake shrink, and the post-issue 'theoretical ex-rights price' sits below the pre-issue price."},
    {"id": 23, "category": "Corporate actions", "format": "cloze",
     "question": "A company buying back 5% of its outstanding shares typically causes EPS to ___.",
     "options": ["Decrease by approximately 5%", "Increase by approximately 5% (assuming flat earnings)", "Remain unchanged, since net income is unaffected"],
     "correct": 1,
     "explanation": "Buybacks shrink the EPS denominator; with constant net income, EPS rises by about 1/(1-buyback%) - 1, roughly 5.3% for a 5% reduction."},
    {"id": 24, "category": "Corporate actions", "format": "direct",
     "question": "A DRIP (Dividend Reinvestment Plan) does what?",
     "options": ["Defers dividend taxes until shares are sold", "Automatically uses cash dividends to purchase additional shares", "Pays dividends in stock instead of cash for tax efficiency"],
     "correct": 1,
     "explanation": "DRIPs reinvest cash dividends into more shares (often fractional), compounding the holding; the dividend is still taxable in the year received."},
    {"id": 25, "category": "Corporate actions", "format": "cloze",
     "question": "A tender offer differs from open-market buybacks because the company ___.",
     "options": ["Offers a fixed price (often above market) to repurchase a specified number of shares", "Buys shares anonymously through a broker over many months", "Issues new shares at a discount to existing holders"],
     "correct": 0,
     "explanation": "Tender offers are time-bound public offers at a premium; shareholders choose whether to tender, and if oversubscribed the company prorates the purchase."},
    {"id": 26, "category": "Corporate actions", "format": "direct",
     "question": "The 'ex-dividend date' is the date on which:",
     "options": ["The dividend is paid to shareholders of record", "New buyers are NOT entitled to the upcoming dividend", "The company's board approves the dividend amount"],
     "correct": 1,
     "explanation": "You must own the stock BEFORE the ex-dividend date to receive the dividend; on the ex-date the stock typically opens lower by approximately the dividend amount."},
    # ---- Beyond equities ----
    {"id": 5, "category": "Beyond equities", "format": "direct",
     "question": "A bond with 7-year duration loses approximately how much of its market value if yields rise by 1%?",
     "options": ["1%", "7%", "14%"],
     "correct": 1,
     "explanation": "Duration measures price sensitivity: a 1% yield change moves a 7-year-duration bond by roughly 7% in the opposite direction, ignoring convexity."},
    {"id": 6, "category": "Beyond equities", "format": "cloze",
     "question": "A REIT must distribute at least ___ of its taxable income to shareholders annually to maintain its pass-through tax status.",
     "options": ["50%", "75%", "90%"],
     "correct": 2,
     "explanation": "The 90% distribution rule is why REITs typically offer higher dividend yields than common stocks but retain little internally for growth reinvestment."},
    {"id": 27, "category": "Beyond equities", "format": "cloze",
     "question": "A yield curve inversion (short-term yields above long-term) has historically preceded ___.",
     "options": ["Equity bull markets within 6 months", "Recessions within 12-24 months", "Currency devaluations of the home country"],
     "correct": 1,
     "explanation": "Most US recessions since 1955 have been preceded by yield curve inversions (typically 2y/10y or 3m/10y), though the lag varies and false signals do occur."},
    {"id": 28, "category": "Beyond equities", "format": "direct",
     "question": "A 'credit spread' on a corporate bond represents:",
     "options": ["The bid-ask spread on the bond", "The yield premium over a comparable-maturity government bond", "The difference between the bond's coupon and current yield"],
     "correct": 1,
     "explanation": "The credit spread compensates investors for default risk; spreads widen sharply during stress (2008, 2020) and narrow during calm conditions."},
    {"id": 29, "category": "Beyond equities", "format": "cloze",
     "question": "A corporate bond rated BB+ by S&P is classified as ___.",
     "options": ["Investment grade, top tier", "High yield / 'junk', just below investment grade", "Default imminent"],
     "correct": 1,
     "explanation": "Investment grade ends at BBB-/Baa3; BB+ is the highest non-investment-grade rating, signaling speculative credit quality and typically wider spreads."},
    {"id": 30, "category": "Beyond equities", "format": "direct",
     "question": "A key structural difference between ETFs and mutual funds is:",
     "options": ["ETFs trade intraday at market prices; mutual funds price once daily at NAV", "ETFs guarantee liquidity for redemptions; mutual funds don't", "ETFs are tax-free; mutual funds aren't"],
     "correct": 0,
     "explanation": "ETFs have continuous market pricing with bid-ask spreads; mutual fund orders all execute at the single NAV computed after market close."},
    {"id": 31, "category": "Beyond equities", "format": "cloze",
     "question": "An oil futures market in 'contango' means that ___.",
     "options": ["Spot prices are higher than futures prices", "Futures prices are higher than spot prices", "The market is closed due to regulatory action"],
     "correct": 1,
     "explanation": "Contango (futures > spot) is the normal state for storable commodities; it imposes a 'roll cost' on long-only futures-based ETFs as expiring contracts are replaced by more expensive ones."},
    {"id": 32, "category": "Beyond equities", "format": "direct",
     "question": "TIPS (Treasury Inflation-Protected Securities) protect against inflation by:",
     "options": ["Paying a higher fixed coupon than nominal Treasuries", "Adjusting the principal upward with the CPI", "Maturing earlier when inflation exceeds 2%"],
     "correct": 1,
     "explanation": "TIPS principal is reset twice yearly using CPI; coupon payments (a fixed % of adjusted principal) rise with inflation, preserving real purchasing power."},
    {"id": 33, "category": "Beyond equities", "format": "cloze",
     "question": "A 'callable' bond gives the issuer the right to ___.",
     "options": ["Demand additional collateral from the bondholder", "Redeem the bond before maturity at a specified price", "Convert the bond into common stock"],
     "correct": 1,
     "explanation": "Callable bonds compensate investors via higher yields; issuers typically call when rates fall (refinancing at lower rates), capping investor upside on the position."},
    {"id": 34, "category": "Beyond equities", "format": "direct",
     "question": "A money market fund that 'breaks the buck' has:",
     "options": ["Frozen redemptions to prevent a run on the fund", "Fallen below $1.00 NAV per share, signaling losses", "Doubled its dividend yield unexpectedly"],
     "correct": 1,
     "explanation": "Money market funds target stable $1 NAV; breaking the buck (rare - notably the Reserve Primary Fund in 2008) means losses on the underlying short-term debt holdings."},
    # ---- Derivatives ----
    {"id": 7, "category": "Derivatives", "format": "direct",
     "question": "You own 100 shares of a stock at £95 and sell one covered call at strike £100. Maximum profit on the combined position occurs when the stock at expiration is:",
     "options": ["Below £95 (any drop)", "Exactly £100", "£120 or higher"],
     "correct": 1,
     "explanation": "At £100, the call expires worthless (you keep the premium) AND you captured the full £5/share stock appreciation; above £100 your stock gain is capped at £100."},
    {"id": 8, "category": "Derivatives", "format": "cloze",
     "question": "Theta on an option position represents ___.",
     "options": ["Sensitivity to changes in the underlying stock's price", "The rate of value decay from time passing", "Sensitivity to changes in implied volatility"],
     "correct": 1,
     "explanation": "Theta is daily time decay - options lose extrinsic value as expiration approaches, so long-option holders 'pay theta' and short-option sellers 'collect theta'."},
    {"id": 35, "category": "Derivatives", "format": "cloze",
     "question": "Put-call parity says that for a given strike and expiration, a call's price relates to the put's price via ___.",
     "options": ["Call + Put = Stock", "Call - Put = Stock - PV(Strike)", "Call × Put = Stock × Strike"],
     "correct": 1,
     "explanation": "Put-call parity (C - P = S - Ke^(-rT)) lets you replicate one option using the other plus the underlying; arbitrageurs enforce it tightly in liquid markets."},
    {"id": 36, "category": "Derivatives", "format": "direct",
     "question": "A delta of 0.6 on a call option means:",
     "options": ["The option has a 60% chance of expiring in-the-money", "The option price moves approximately £0.60 for every £1 move in the underlying", "The option costs £0.60 per share"],
     "correct": 1,
     "explanation": "Delta measures price sensitivity to the underlying; deep in-the-money calls approach delta=1, at-the-money calls cluster near 0.5, and deep OTM calls approach 0."},
    {"id": 37, "category": "Derivatives", "format": "cloze",
     "question": "Gamma measures how much ___ changes as the underlying stock moves.",
     "options": ["Time decay (theta)", "Delta", "Implied volatility"],
     "correct": 1,
     "explanation": "Gamma is the 'delta of delta'; it's highest for at-the-money options near expiration, meaning option exposure changes rapidly as the underlying moves around the strike."},
    {"id": 38, "category": "Derivatives", "format": "direct",
     "question": "Rising implied volatility on a stock typically causes:",
     "options": ["Both call and put prices to rise simultaneously", "Calls to rise and puts to fall", "Options to become harder to exercise"],
     "correct": 0,
     "explanation": "Implied volatility reflects expected magnitude of future moves; higher IV raises the value of both upside (calls) and downside (puts) optionality."},
    {"id": 39, "category": "Derivatives", "format": "cloze",
     "question": "The key difference between futures and forwards is that futures ___.",
     "options": ["Are traded on exchanges with daily mark-to-market settlement", "Can never result in physical delivery of the underlying", "Are only available to institutional investors"],
     "correct": 0,
     "explanation": "Futures are standardized exchange-traded contracts with daily settlement of gains/losses; forwards are bilateral OTC contracts with cash flows typically only at maturity."},
    {"id": 40, "category": "Derivatives", "format": "direct",
     "question": "A long straddle (buy call + buy put at same strike) is profitable when:",
     "options": ["The stock stays within a narrow range around the strike", "The stock moves significantly in either direction, beyond the combined premium", "Implied volatility drops sharply after purchase"],
     "correct": 1,
     "explanation": "Long straddles bet on big moves regardless of direction; the trade pays off when the stock moves enough to exceed the cost of both options combined."},
    {"id": 41, "category": "Derivatives", "format": "cloze",
     "question": "Selling a cash-secured put on a stock at strike £50 obligates you to ___.",
     "options": ["Buy 100 shares at £50 if assigned", "Sell 100 shares at £50 if assigned", "Pay the buyer £50 in cash immediately"],
     "correct": 0,
     "explanation": "Cash-secured puts require setting aside £5,000 (100 × £50) at trade time; if the stock falls below £50 at expiry, you're obligated to buy at the strike."},
    {"id": 42, "category": "Derivatives", "format": "direct",
     "question": "The 'intrinsic value' of a call option at strike £50 when the stock is £55 is:",
     "options": ["£0 (intrinsic value is always zero for OTM options)", "£5 (max(stock - strike, 0))", "£55 (the current stock price)"],
     "correct": 1,
     "explanation": "Intrinsic value = max(S - K, 0) for calls; the rest of an option's price (extrinsic value) reflects time, volatility, and the other Greeks."},
    # ---- History & regs ----
    {"id": 9, "category": "History & regs", "format": "direct",
     "question": "The 1933 Glass-Steagall Act primarily:",
     "options": ["Created the SEC to regulate stock exchanges", "Separated commercial banking from investment banking", "Required public companies to file annual 10-K reports"],
     "correct": 1,
     "explanation": "Glass-Steagall walled off deposit-taking banks from securities underwriting after the 1929 crash; it was substantially repealed by Gramm-Leach-Bliley in 1999."},
    {"id": 10, "category": "History & regs", "format": "cloze",
     "question": "US exchange 'Level 1' circuit breakers halt all trading when the S&P 500 drops ___ from the previous close.",
     "options": ["5%", "7%", "10%"],
     "correct": 1,
     "explanation": "Level 1 (7%) triggers a 15-minute pause; Levels 2 (13%) and 3 (20%) add longer halts, all designed to slow panic-selling cascades during volatility spikes."},
    {"id": 43, "category": "History & regs", "format": "cloze",
     "question": "Black Monday (October 19, 1987) saw the Dow Jones Industrial Average drop ___ in a single session.",
     "options": ["Approximately 10%", "Approximately 22%", "Approximately 35%"],
     "correct": 1,
     "explanation": "The 22.6% single-day drop remains the largest in DJIA history; portfolio insurance and program trading accelerated the cascade, prompting later introduction of circuit breakers."},
    {"id": 44, "category": "History & regs", "format": "direct",
     "question": "The 2008 Global Financial Crisis was triggered most directly by:",
     "options": ["A sovereign debt default by a major European country", "The collapse of subprime mortgage-backed securities and Lehman Brothers' bankruptcy", "A sudden Fed rate hike to 6%"],
     "correct": 1,
     "explanation": "The MBS collapse exposed leveraged positions across the financial system; Lehman's September 2008 failure crystallized counterparty fears and froze interbank lending."},
    {"id": 45, "category": "History & regs", "format": "cloze",
     "question": "The Dodd-Frank Act (2010) was primarily aimed at ___.",
     "options": ["Reducing tax rates on capital gains", "Increasing regulation of financial institutions and creating the CFPB", "Privatizing Fannie Mae and Freddie Mac"],
     "correct": 1,
     "explanation": "Dodd-Frank introduced the Volcker Rule (limiting proprietary trading by banks), stress tests for large institutions, and the Consumer Financial Protection Bureau."},
    {"id": 46, "category": "History & regs", "format": "direct",
     "question": "MiFID II (EU regulation, 2018) primarily targeted:",
     "options": ["Transparency in trading and unbundling of research from execution costs", "A ban on short-selling EU-listed equities", "Harmonization of dividend tax rates across member states"],
     "correct": 0,
     "explanation": "MiFID II forced firms to separately price research and execution, reducing implicit subsidies and increasing pre/post-trade transparency across asset classes."},
    {"id": 47, "category": "History & regs", "format": "cloze",
     "question": "The SEC's primary mission is to ___.",
     "options": ["Set interest rates and conduct monetary policy", "Protect investors, maintain fair markets, and facilitate capital formation", "Regulate commercial banking activities"],
     "correct": 1,
     "explanation": "The SEC enforces securities laws (disclosure, anti-fraud, market structure); monetary policy is the Federal Reserve; banking regulation is OCC/FDIC."},
    {"id": 48, "category": "History & regs", "format": "direct",
     "question": "The UK's Financial Conduct Authority (FCA) regulates:",
     "options": ["Only retail banking deposits", "Conduct of financial services firms and UK securities markets", "Energy and utility pricing"],
     "correct": 1,
     "explanation": "The FCA supervises about 50,000 financial firms; prudential regulation of large banks/insurers sits with the Prudential Regulation Authority (PRA), part of the Bank of England."},
    {"id": 49, "category": "History & regs", "format": "cloze",
     "question": "The 1929 stock market crash was significantly amplified by ___.",
     "options": ["Widespread use of high margin (leverage) by retail investors", "A sudden collapse in gold prices", "Federal Reserve emergency liquidity withdrawal"],
     "correct": 0,
     "explanation": "Investors routinely bought stocks with 10% down and 90% margin loans; when prices fell, margin calls forced selling, which dropped prices further, triggering more calls."},
    {"id": 50, "category": "History & regs", "format": "direct",
     "question": "The Sarbanes-Oxley Act (2002) was enacted primarily in response to:",
     "options": ["The dot-com bubble's collapse", "Major accounting fraud at Enron and WorldCom", "The 9/11 terrorist attacks' impact on markets"],
     "correct": 1,
     "explanation": "SOX imposed strict accounting controls, CEO/CFO certification of financial statements, and criminal penalties for fraud; it also created the PCAOB to oversee auditors."},

    # ===== v2.4: entry-level + hard tiers (5 entry + 5 hard per category) =====
    # ---- Market mechanics: entry ----
    {"id": 51, "category": "Market mechanics", "format": "cloze", "difficulty": "entry",
     "question": "A stock's 'bid' price is the highest price ___.",
     "options": ["a buyer is currently willing to pay", "a seller is asking to receive", "the stock last traded at"],
     "correct": 0,
     "explanation": "The bid is the best price buyers will pay and the ask is the best price sellers want; the gap between them is the spread."},
    {"id": 52, "category": "Market mechanics", "format": "direct", "difficulty": "entry",
     "question": "A 'market order' to buy a stock tells your broker to:",
     "options": ["Buy only if the price first falls to a set level", "Buy immediately at the best available current price", "Buy at the day's official closing price"],
     "correct": 1,
     "explanation": "Market orders prioritise immediate execution over price, filling at whatever the best current offer is."},
    {"id": 53, "category": "Market mechanics", "format": "cloze", "difficulty": "entry",
     "question": "After its IPO, a company's shares trade between investors in the ___ market.",
     "options": ["primary", "forward", "secondary"],
     "correct": 2,
     "explanation": "The IPO is the primary market where the company raises cash; all later investor-to-investor trading happens in the secondary market."},
    {"id": 54, "category": "Market mechanics", "format": "direct", "difficulty": "entry",
     "question": "A stock's daily trading 'volume' measures:",
     "options": ["The number of shares traded that day", "The total market value of the company", "The number of people who own the stock"],
     "correct": 0,
     "explanation": "Volume counts shares traded in the session; higher volume usually means tighter spreads and easier entry and exit."},
    {"id": 55, "category": "Market mechanics", "format": "cloze", "difficulty": "entry",
     "question": "A 'ticker symbol' such as AAPL is ___.",
     "options": ["the company's current stock price", "a measure of how volatile it is", "the unique short code identifying a listed security"],
     "correct": 2,
     "explanation": "Tickers are exchange-assigned shorthand so orders route to the correct security."},
    # ---- Market mechanics: hard ----
    {"id": 56, "category": "Market mechanics", "format": "direct", "difficulty": "hard",
     "question": "'Payment for order flow' (PFOF) is:",
     "options": ["A fee retail brokers pay an exchange to list orders", "Market makers paying brokers to route their clients' orders", "A government levy on high-frequency trades"],
     "correct": 1,
     "explanation": "Wholesalers pay brokers for their retail order flow and profit from the spread, which is how many brokers fund zero-commission trading."},
    {"id": 57, "category": "Market mechanics", "format": "cloze", "difficulty": "hard",
     "question": "Under US Reg NMS, an order must generally execute at the ___.",
     "options": ["National Best Bid and Offer (NBBO)", "exchange with the highest daily volume", "investor's designated home exchange"],
     "correct": 0,
     "explanation": "The order-protection rule makes trades fill at the best displayed price across all exchanges (the NBBO), preventing trade-throughs."},
    {"id": 58, "category": "Market mechanics", "format": "direct", "difficulty": "hard",
     "question": "A 'market-on-close' (MOC) order is designed to:",
     "options": ["Cancel any unfilled orders at the close", "Execute in the official closing auction", "Trade only in the after-hours session"],
     "correct": 1,
     "explanation": "MOC orders pool into the closing auction, which sets one price on heavy volume and is favoured by index funds tracking the close."},
    {"id": 59, "category": "Market mechanics", "format": "cloze", "difficulty": "hard",
     "question": "A 'locked market' occurs when the bid ___ the ask.",
     "options": ["equals", "sits far below", "is exactly double"],
     "correct": 0,
     "explanation": "A locked market has bid equal to ask (crossed means bid above ask); both are abnormal and discouraged because they signal routing or fee quirks."},
    {"id": 60, "category": "Market mechanics", "format": "direct", "difficulty": "hard",
     "question": "Firms pay for 'co-location' (servers inside the exchange's data centre) mainly to:",
     "options": ["Meet regulatory data-storage rules", "Cut their electricity costs", "Minimise latency for speed-sensitive strategies"],
     "correct": 2,
     "explanation": "Shaving microseconds off the round-trip matters for market-making and arbitrage, so firms pay to sit beside the exchange's matching engine."},
    # ---- Corporate actions: entry ----
    {"id": 61, "category": "Corporate actions", "format": "cloze", "difficulty": "entry",
     "question": "A cash dividend is ___.",
     "options": ["a share of profits paid out to shareholders", "a loan the company takes from investors", "a discount on buying additional shares"],
     "correct": 0,
     "explanation": "Dividends hand a slice of company profits to shareholders, usually quarterly, though not every company pays one."},
    {"id": 62, "category": "Corporate actions", "format": "direct", "difficulty": "entry",
     "question": "After a 2-for-1 stock split, a share that traded at £100 will trade at roughly:",
     "options": ["£200 with half as many shares", "£50 with twice as many shares", "£100 with twice as many shares"],
     "correct": 1,
     "explanation": "A split multiplies the share count and divides the price proportionally, so the total value of your holding is unchanged."},
    {"id": 63, "category": "Corporate actions", "format": "cloze", "difficulty": "entry",
     "question": "An IPO (initial public offering) is when a company ___.",
     "options": ["sells shares to the public for the first time", "buys back its own shares", "merges with a rival"],
     "correct": 0,
     "explanation": "An IPO takes a company from private to publicly traded, raising capital and giving early backers a way to cash out."},
    {"id": 64, "category": "Corporate actions", "format": "direct", "difficulty": "entry",
     "question": "To receive a declared dividend you must own the shares before the:",
     "options": ["Payment date", "Ex-dividend date", "Announcement date"],
     "correct": 1,
     "explanation": "Buy on or after the ex-dividend date and the seller keeps the payout; the price typically drops by the dividend on that date."},
    {"id": 65, "category": "Corporate actions", "format": "cloze", "difficulty": "entry",
     "question": "A share buyback is when a company ___.",
     "options": ["issues new shares to raise cash", "splits its existing stock", "buys its own shares back from the market"],
     "correct": 2,
     "explanation": "Buybacks return cash by shrinking the share count, which mechanically lifts earnings per share."},
    # ---- Corporate actions: hard ----
    {"id": 66, "category": "Corporate actions", "format": "direct", "difficulty": "hard",
     "question": "After a tax-free spin-off, your original cost basis is:",
     "options": ["Entirely assigned to the new spun-off shares", "Allocated between parent and spin-off by relative market value", "Reset to the spin-off day's closing price"],
     "correct": 1,
     "explanation": "Tax rules split your original basis across the parent and spun-off shares in proportion to their market values just after the split."},
    {"id": 67, "category": "Corporate actions", "format": "cloze", "difficulty": "hard",
     "question": "In a rights issue, the theoretical ex-rights price (TERP) is ___.",
     "options": ["the weighted average of the old shares and the discounted new shares", "always exactly the subscription price", "the price the day before the rights were announced"],
     "correct": 0,
     "explanation": "TERP blends the old share price with the cheaper new shares, and holders who don't take up their rights are diluted toward that level."},
    {"id": 68, "category": "Corporate actions", "format": "direct", "difficulty": "hard",
     "question": "When a company pays a large 'special' dividend, listed option strike prices are usually:",
     "options": ["Left unchanged", "Adjusted down by the dividend amount", "Doubled"],
     "correct": 1,
     "explanation": "The clearing house adjusts strikes for special (non-ordinary) dividends so option holders aren't wiped out by the price drop; ordinary dividends are not adjusted."},
    {"id": 69, "category": "Corporate actions", "format": "cloze", "difficulty": "hard",
     "question": "A Dutch-auction tender offer buys back shares at ___.",
     "options": ["the lowest price that secures the desired number of shares", "a fixed premium fixed in advance", "the simple average of all submitted bids"],
     "correct": 0,
     "explanation": "Holders name prices within a range and the company pays the single lowest price that buys the shares it wants, to everyone who bid at or below it."},
    {"id": 70, "category": "Corporate actions", "format": "direct", "difficulty": "hard",
     "question": "A reverse stock split is most often used to:",
     "options": ["Return surplus cash to shareholders", "Lift the share price to meet a listing minimum", "Increase the number of shares outstanding"],
     "correct": 1,
     "explanation": "Consolidating shares (e.g. 1-for-10) raises the quoted price to avoid delisting, without changing the company's underlying value."},
    # ---- Beyond equities: entry ----
    {"id": 71, "category": "Beyond equities", "format": "cloze", "difficulty": "entry",
     "question": "A bond's 'coupon' is ___.",
     "options": ["the periodic interest it pays the holder", "the price you pay to buy it", "the date it matures"],
     "correct": 0,
     "explanation": "The coupon is the fixed interest a bond pays, usually semi-annually, until it matures and repays the principal."},
    {"id": 72, "category": "Beyond equities", "format": "direct", "difficulty": "entry",
     "question": "A REIT (real estate investment trust) lets investors:",
     "options": ["Own physical property directly and tax-free", "Invest in income-producing real estate via a tradable share", "Borrow cheaply against their home"],
     "correct": 1,
     "explanation": "REITs hold portfolios of property and must pay out most of their income as dividends, giving stock-like access to real estate."},
    {"id": 73, "category": "Beyond equities", "format": "cloze", "difficulty": "entry",
     "question": "An ETF (exchange-traded fund) is ___.",
     "options": ["a single government bond", "a type of savings account", "a basket of securities that trades like one stock"],
     "correct": 2,
     "explanation": "ETFs bundle many holdings (often an index) into one ticker that trades intraday, typically at low cost."},
    {"id": 74, "category": "Beyond equities", "format": "direct", "difficulty": "entry",
     "question": "Gold is called a 'safe-haven' asset because investors tend to buy it:",
     "options": ["When equity markets are booming", "During market stress or high inflation", "Only when interest rates are rising"],
     "correct": 1,
     "explanation": "Gold carries no credit risk and has historically held value in crises, so demand often rises when confidence in other assets falls."},
    {"id": 75, "category": "Beyond equities", "format": "cloze", "difficulty": "entry",
     "question": "A government bond is generally ___ than a corporate bond of the same maturity.",
     "options": ["lower risk", "higher risk", "far more volatile"],
     "correct": 0,
     "explanation": "Sovereigns borrowing in their own currency rarely default, so government bonds usually yield less than riskier corporate debt."},
    # ---- Beyond equities: hard ----
    {"id": 76, "category": "Beyond equities", "format": "direct", "difficulty": "hard",
     "question": "A bond with higher 'duration' will:",
     "options": ["Pay a higher coupon", "Fall more in price when interest rates rise", "Mature sooner"],
     "correct": 1,
     "explanation": "Duration measures price sensitivity to rates; a bond with seven-year duration loses roughly 7% if yields rise one percentage point."},
    {"id": 77, "category": "Beyond equities", "format": "cloze", "difficulty": "hard",
     "question": "A futures market is in 'contango' when ___.",
     "options": ["futures prices sit above the spot price", "the spot price sits above futures", "storage is free"],
     "correct": 0,
     "explanation": "Contango (futures above spot) reflects storage and carrying costs, so continually rolling long futures bleeds value over time."},
    {"id": 78, "category": "Beyond equities", "format": "direct", "difficulty": "hard",
     "question": "REIT investors track 'FFO' (funds from operations) instead of net income because it:",
     "options": ["Strips out rental revenue", "Adds back property depreciation", "Ignores all interest expense"],
     "correct": 1,
     "explanation": "Property depreciation is a big non-cash charge that understates a REIT's cash earnings, so FFO adds it back to net income."},
    {"id": 79, "category": "Beyond equities", "format": "cloze", "difficulty": "hard",
     "question": "An 'inverted yield curve' (short-term yields above long-term) has historically ___.",
     "options": ["preceded many US recessions", "reliably signalled strong growth ahead", "meant nothing for the economy"],
     "correct": 0,
     "explanation": "Inversion implies markets expect rate cuts into a slowdown; the 10-year-minus-2-year spread is a closely watched recession signal."},
    {"id": 80, "category": "Beyond equities", "format": "direct", "difficulty": "hard",
     "question": "'Convexity' in bonds describes:",
     "options": ["The curvature in the price/yield relationship that duration alone misses", "A bond's credit rating tier", "How often the bond pays coupons"],
     "correct": 0,
     "explanation": "Duration is only a straight-line estimate; convexity captures that prices rise more when yields fall than they drop when yields rise."},
    # ---- Derivatives: entry ----
    {"id": 81, "category": "Derivatives", "format": "cloze", "difficulty": "entry",
     "question": "A call option gives the holder the right to ___ the underlying at the strike price.",
     "options": ["buy", "sell", "short"],
     "correct": 0,
     "explanation": "A call is the right, not the obligation, to buy at the strike, so you exercise it when the stock is above that price."},
    {"id": 82, "category": "Derivatives", "format": "direct", "difficulty": "entry",
     "question": "A put option gives the holder the right to:",
     "options": ["Buy the stock at the strike price", "Sell the stock at the strike price", "Collect the stock's dividend"],
     "correct": 1,
     "explanation": "A put is the right to sell at the strike, so it gains value as the underlying falls and can act as portfolio insurance."},
    {"id": 83, "category": "Derivatives", "format": "cloze", "difficulty": "entry",
     "question": "The 'strike price' of an option is ___.",
     "options": ["the premium paid for the option", "the underlying's current market price", "the price at which the option can be exercised"],
     "correct": 2,
     "explanation": "The strike is the fixed buy or sell price written into the contract, and its distance from the spot drives the option's value."},
    {"id": 84, "category": "Derivatives", "format": "direct", "difficulty": "entry",
     "question": "The 'premium' of an option is:",
     "options": ["The profit locked in at expiration", "The price paid to buy the contract", "The strike minus the stock price"],
     "correct": 1,
     "explanation": "The premium is the upfront price of the contract, made up of intrinsic value plus time value."},
    {"id": 85, "category": "Derivatives", "format": "cloze", "difficulty": "entry",
     "question": "One standard US equity option contract represents ___ shares.",
     "options": ["10", "100", "1,000"],
     "correct": 1,
     "explanation": "One standard contract controls 100 shares, so a $2 premium costs $200 plus fees."},
    # ---- Derivatives: hard ----
    {"id": 86, "category": "Derivatives", "format": "direct", "difficulty": "hard",
     "question": "An option with a 'delta' of 0.5 should move about:",
     "options": ["50 cents for every $1 move in the stock", "Half a percent in value each day", "50% of its value by expiration"],
     "correct": 0,
     "explanation": "Delta is how much the option price moves per $1 move in the stock; around 0.5 is typical for an at-the-money option."},
    {"id": 87, "category": "Derivatives", "format": "cloze", "difficulty": "hard",
     "question": "'Theta' measures an option's ___.",
     "options": ["sensitivity to volatility", "sensitivity to interest rates", "loss of value as time passes"],
     "correct": 2,
     "explanation": "Theta is time decay: all else equal an option loses a little value each day, and the bleed accelerates near expiry."},
    {"id": 88, "category": "Derivatives", "format": "direct", "difficulty": "hard",
     "question": "Put-call parity links a call and put of the same strike and expiry via:",
     "options": ["A requirement that the call always cost more than the put", "A fixed relationship involving the stock price and the discounted strike", "Forcing both premiums to be equal"],
     "correct": 1,
     "explanation": "Call minus put equals stock minus the present value of the strike; sizeable violations open up riskless arbitrage."},
    {"id": 89, "category": "Derivatives", "format": "cloze", "difficulty": "hard",
     "question": "'IV crush' is when ___ falls sharply right after an event like earnings.",
     "options": ["implied volatility", "the underlying's share price", "total open interest"],
     "correct": 0,
     "explanation": "Implied volatility inflates premiums ahead of a known event and collapses once it passes, which can hurt option buyers even when they pick the direction."},
    {"id": 90, "category": "Derivatives", "format": "direct", "difficulty": "hard",
     "question": "The maximum profit on a long call (debit) vertical spread is:",
     "options": ["Unlimited", "The width between strikes minus the net premium paid", "The premium you received"],
     "correct": 1,
     "explanation": "Buying a lower-strike call and selling a higher one caps the upside at the strike width minus the net premium paid."},
    # ---- History & regs: entry ----
    {"id": 91, "category": "History & regs", "format": "cloze", "difficulty": "entry",
     "question": "The S&P 500 is ___.",
     "options": ["an index of 500 large US companies", "a single large technology stock", "a US government bond"],
     "correct": 0,
     "explanation": "It's a market-cap-weighted index of 500 large US companies, the standard benchmark for US large-cap performance."},
    {"id": 92, "category": "History & regs", "format": "direct", "difficulty": "entry",
     "question": "The main job of the US Securities and Exchange Commission (SEC) is to:",
     "options": ["Set the country's interest rates", "Regulate securities markets and protect investors", "Print the currency"],
     "correct": 1,
     "explanation": "The SEC enforces disclosure and anti-fraud rules in securities markets; monetary policy is the Federal Reserve's job."},
    {"id": 93, "category": "History & regs", "format": "cloze", "difficulty": "entry",
     "question": "A 'bull market' is a period of ___ prices.",
     "options": ["generally falling", "broadly flat", "generally rising"],
     "correct": 2,
     "explanation": "A bull market is a sustained rise; a bear market is a sustained fall (commonly 20% or more) — the terms describe direction and sentiment."},
    {"id": 94, "category": "History & regs", "format": "direct", "difficulty": "entry",
     "question": "In the US, FDIC insurance protects:",
     "options": ["Stock investments against market losses", "Bank deposits up to a set limit per depositor", "Corporate bonds against default"],
     "correct": 1,
     "explanation": "The FDIC insures bank deposits (currently $250,000 per depositor, per bank) but does not cover investment losses."},
    {"id": 95, "category": "History & regs", "format": "cloze", "difficulty": "entry",
     "question": "The Federal Reserve influences the economy mainly by ___.",
     "options": ["setting interest-rate policy", "approving company IPOs", "auditing public companies' accounts"],
     "correct": 0,
     "explanation": "The Fed sets the policy interest rate and steers the money supply to pursue stable prices and maximum employment."},
    # ---- History & regs: hard ----
    {"id": 96, "category": "History & regs", "format": "direct", "difficulty": "hard",
     "question": "The Volcker Rule (part of Dodd-Frank) primarily restricts banks from:",
     "options": ["Paying dividends to shareholders", "Proprietary trading with their own capital", "Offering basic checking accounts"],
     "correct": 1,
     "explanation": "The rule bars deposit-taking banks from speculative proprietary trading to limit risk-taking with insured funds."},
    {"id": 97, "category": "History & regs", "format": "cloze", "difficulty": "hard",
     "question": "Basel III is an international framework that raised banks' ___ requirements.",
     "options": ["capital and liquidity", "advertising", "dividend-payout"],
     "correct": 0,
     "explanation": "After 2008, Basel III forced banks to hold more high-quality capital and bigger liquidity buffers to absorb shocks."},
    {"id": 98, "category": "History & regs", "format": "direct", "difficulty": "hard",
     "question": "The May 2010 'Flash Crash' was notable because the market:",
     "options": ["Closed for a week", "Plunged about 9% and largely rebounded within minutes", "Was triggered by a major bank failure"],
     "correct": 1,
     "explanation": "Automated selling cascaded into a near-1,000-point Dow drop and a rapid rebound, prompting today's single-stock circuit breakers."},
    {"id": 99, "category": "History & regs", "format": "cloze", "difficulty": "hard",
     "question": "Long-Term Capital Management collapsed in 1998 mainly due to ___.",
     "options": ["excessive leverage on convergence trades", "a large accounting fraud", "an insider-trading scandal"],
     "correct": 0,
     "explanation": "LTCM's enormous leverage turned small, correlated losses after Russia's default into a systemic threat, forcing a Fed-organised rescue."},
    {"id": 100, "category": "History & regs", "format": "direct", "difficulty": "hard",
     "question": "The Gramm-Leach-Bliley Act (1999) is significant because it:",
     "options": ["Created the SEC", "Repealed much of Glass-Steagall, letting commercial and investment banking recombine", "Introduced market-wide circuit breakers"],
     "correct": 1,
     "explanation": "It tore down the Depression-era wall between commercial and investment banking, reshaping the modern financial-services industry."},
]
assert len(QUIZ_POOL) == 100, f"QUIZ_POOL has {len(QUIZ_POOL)} entries (expected 100)"

# Universe - large-cap reference list for the industry outlook section.
# Refreshed monthly (file-mtime TTL); daily builds reuse cached results.
UNIVERSE_CSV = ROOT / "universe.csv"
UNIVERSE_CACHE = ROOT / "data" / "universe_outlook_cache.parquet"
UNIVERSE_TTL_DAYS = 30

# v2.3 Market expectations - prediction-market sentiment (Kalshi + Polymarket).
PREDICTIONS_CSV = ROOT / "predictions.csv"
PREDICTIONS_CACHE = ROOT / "data" / "predictions_cache.parquet"
PRIOR_PREDICTIONS_CACHE = ROOT / "data" / "prior_predictions_cache.parquet"
BIGBRAIN_LOG_CSV = ROOT / "data" / "bigbrain_log.csv"   # v2.3 Big Brain flag history
KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
POLYMARKET_API = "https://gamma-api.polymarket.com"
PRED_VOLUME_FLOOR = 1000.0   # legacy single floor (fallback for unknown sources)
# M1: per-source liquidity floors — the two sources' volumes are NOT commensurable
# (Kalshi reports contracts traded; Polymarket reports cumulative USD), so a single
# floor filters one far more aggressively than the other. Tune each in its own unit.
PRED_VOLUME_FLOORS = {
    "kalshi": 1000.0,       # contracts traded
    "polymarket": 5000.0,   # cumulative USD volume
}
PRED_MAX_ROWS = 20           # v2.5 #3: deepen the pool; render shows a window
PRED_WINDOW = 5              # v2.5 #3: themes shown at once; "Reshuffle" cycles
PRED_HORIZON_DAYS = 150      # v3.0 #6: only show markets resolving within ~5 months
                             # (sits in the 123d/186d gap: keeps near-term macro,
                             #  drops the year-end geopolitical + long-horizon cluster)
BB_MACRO_DELTA_PP = 8.0      # min |delta| (pp) for the Big Brain macro callout

# v2.6 Value screen — quality+value names trading near their 52-week low.
# All thresholds are tunable. Filter #1 (near-low) is a required gate; #2-#6 are
# scored; a name needs VALUE_MIN_PASS of 6 to appear.
VALUE_NEAR_LOW_PCT = 10.0    # gate: within this % of the 52-week range bottom
VALUE_MIN_ROE      = 0.10    # #4 profitability (ROE; ROIC proxy)
VALUE_MAX_DE       = 1.5     # #6 balance sheet (debt/equity below this)
VALUE_MIN_PASS     = 6       # need >= this many of the 6 filters (strict all-6)
VALUE_MAX_ROWS     = 20      # cap; rendered in pages of VALUE_PAGE
VALUE_PAGE         = 10      # rows shown per page (arrows flip to the next set)
VALUE_MIN_SECTOR_N = 3       # #2 sector-median P/E needs >= this many priced peers
# v3.0 #4/fix: watchlist pages by how many cards fill the row (measured live in
# JS), so a page is always gap-free. CSS forces these column counts per breakpoint.
WATCH_COLS_DESKTOP = 6       # cards per row on desktop
WATCH_COLS_MOBILE  = 3       # cards per row on mobile; also the "needs paging" floor
AUTO_WATCH_MAX     = 4       # v3.0 #5: max auto (Value ∩ Big Brain) watchlist picks

# How many candidates the analyst panel shows in total (the grid scrolls
# internally past ~6 visible). Safety cap for very large closed-position lists.
ANALYST_TOP_N = 50

DEFAULT_BASELINE = pd.Timestamp("2024-10-21")
START_DATE = "2024-10-14"
BENCHMARK = "SPY"
BENCHMARK_CCY = "USD"            # native currency of the benchmark
BENCHMARK2 = "QQQ"               # v3.0 #3: Nasdaq-100 ETF, optional hero overlay
BENCHMARK2_CCY = "USD"           # native currency of the overlay (FX-clean, like SPY)
BASE_CCY = "GBP"                 # all displayed values normalize to this
BASE_SYMBOL = "£"
CCY_SYMBOLS = {"GBP": "£", "USD": "$", "EUR": "€", "JPY": "¥", "CHF": "CHF "}
# Pence/cent variants reported by yfinance are normalized by dividing by 100:
PENCE_CODES = {"GBp": "GBP", "GBX": "GBP", "ZAc": "ZAR", "ILA": "ILS"}
FX_CACHE = ROOT / "data" / "fx_cache.parquet"

# Free RSS feeds polled at build time for the news panel. Order matters only
# for the tie-break when two feeds publish the same headline at the same second.
NEWS_FEEDS = [
    ("Yahoo Finance", "https://finance.yahoo.com/news/rssindex"),
    ("MarketWatch",   "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
]
NEWS_MAX_ITEMS = 12

# URL of the deployed Cloudflare Worker (see worker/README.md for how to
# deploy). Empty string disables live refresh — the page then shows only the
# build-time fetch (which is fine but caps at the workflow's cron cadence).
# After deploying, paste the URL here including the /news path, e.g.:
#   NEWS_WORKER_URL = "https://stocks-dashboard-news.example.workers.dev/news"
NEWS_WORKER_URL = "https://stocks-dashboard-news.newpov.workers.dev/news"


# --------------------------------------------------------------------------
# Data layer
# --------------------------------------------------------------------------
# v2.4: broker-agnostic column aliases so a forker can drop in most brokers'
# CSV exports with little/no editing. The canonical schema is still
# ticker,date,action[,shares]; these just map common header variants onto it.
_TXN_COL_ALIASES = {
    "ticker": ["ticker", "symbol", "instrument", "stock", "security"],
    "date":   ["date", "time", "trade date", "executed at", "datetime", "settled"],
    "action": ["action", "type", "side", "transaction type", "activity", "buy/sell"],
    "shares": ["shares", "quantity", "qty", "no. of shares", "no of shares", "units", "amount"],
}


def _normalize_txn_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Rename whatever header variants are present to the canonical names."""
    lower = {str(c).lower().strip(): c for c in df.columns}
    rename = {}
    for canon, aliases in _TXN_COL_ALIASES.items():
        for a in aliases:
            if a in lower and lower[a] not in rename:
                rename[lower[a]] = canon
                break
    return df.rename(columns=rename)


def _normalize_action(v) -> str:
    """Map free-text action values onto BUY / SELL (or '' to drop). Handles
    bare single-letter broker codes ("B"/"S") as well as phrases."""
    s = str(v).strip().lower()
    if s in ("b", "buy") or any(k in s for k in ("buy", "bought", "purchase")):
        return "BUY"
    if s in ("s", "sell") or any(k in s for k in ("sell", "sold", "sale", "disposal")):
        return "SELL"
    return ""


def load_transactions(path: Path = TRANSACTIONS_CSV) -> pd.DataFrame:
    """Load and validate transactions.csv.

    Canonical schema: ``ticker, date, action`` (+ optional ``shares``). v2.4
    tolerates common broker header variants (symbol/quantity/side/...) and
    free-text actions ("Market buy", "Sold", "B"). With no ``shares`` column
    every row counts as one unit, which is exactly what equal-weight wants.

    Each row is a single BUY or SELL event, aggregated per ticker into positions
    with prices looked up from yfinance for the transaction dates.
    """
    df = pd.read_csv(path)
    df = _normalize_txn_columns(df)
    missing = {"ticker", "date", "action"} - set(df.columns)
    if missing:
        raise ValueError(
            f"{path.name} is missing required column(s): {sorted(missing)}. "
            f"Accepted header names per field: {_TXN_COL_ALIASES}")
    df["ticker"] = df["ticker"].astype(str).str.strip().str.upper()
    df = df[df["ticker"] != ""].copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["action"] = df["action"].map(_normalize_action)
    if "shares" in df.columns:
        df["shares"] = pd.to_numeric(df["shares"], errors="coerce").fillna(0.0)
    else:
        df["shares"] = 1.0   # no quantity column -> one unit per row
    df = df[df["action"].isin(["BUY", "SELL"])]
    df = df[df["shares"] > 0]
    df = df.dropna(subset=["date"])
    return df.sort_values(["ticker", "date"]).reset_index(drop=True)


def load_transactions_from_snapshot() -> pd.DataFrame:
    """Load the committed, normalized basket.snapshot.csv (same schema/validation
    as transactions.csv). This is the render source for both the author's local
    build (after regenerating the snapshot) and CI (--from-snapshot)."""
    return load_transactions(BASKET_SNAPSHOT_CSV)


def export_basket_snapshot(transactions: pd.DataFrame) -> pd.DataFrame:
    """Normalize the real ticker'd transactions into a privacy-safe snapshot.

    Strict-privacy equal-weight rules (Option B — reset on every sell):
      - every BUY  -> 1 unit
      - EVERY SELL closes the current cycle: emit K units (K = normalized open units
        in the cycle), forcing normalized net -> 0 so the cost-basis cycle resets.
        The equal-weight snapshot carries no share sizes, so it cannot tell a trim
        from a full exit; by author decision a sell always re-anchors the baseline to
        the buys SINCE that sell (matches the "last entry = current position" mental
        model). A buy-trim-add therefore re-anchors to the add (discarding the cost
        of shares carried through the trim — unavoidable without real quantities).
      - an orphan SELL with no open units -> emitted as nothing (no bogus SELL 0 row).

    NOTE: this differs from the average-cost basis the shared `_active_cycle_basis`
    helper applies to value mode / forkers (where real quantities keep a genuine trim
    open). The two weight modes intentionally diverge for trimmed-then-added names.

    Manual-fund/untracked rows are not present (caller passes only ticker'd
    transactions), so they are excluded by construction. Output schema matches
    transactions.csv: ticker,date,action,shares (dates as YYYY-MM-DD strings).
    """
    out_rows = []
    for ticker, grp in transactions.sort_values("date").groupby("ticker", sort=True):
        open_units = 0     # normalized units open in the current cycle
        for r in grp.itertuples(index=False):
            action = str(r.action).upper()
            if action == "BUY":
                out_rows.append((ticker, r.date, "BUY", 1))
                open_units += 1
            elif action == "SELL" and open_units > 0:
                out_rows.append((ticker, r.date, "SELL", open_units))
                open_units = 0
            # else: orphan sell (no open units) -> emit nothing
    out = pd.DataFrame(out_rows, columns=["ticker", "date", "action", "shares"])
    out["date"] = pd.to_datetime(out["date"]).dt.strftime("%Y-%m-%d")
    out["shares"] = out["shares"].astype(int)
    return out


def write_basket_snapshot(transactions: pd.DataFrame) -> pd.DataFrame:
    """Regenerate basket.snapshot.csv from the real ticker'd transactions."""
    snap = export_basket_snapshot(transactions)
    snap.to_csv(BASKET_SNAPSHOT_CSV, index=False)
    print(f"  wrote {len(snap)} normalized rows -> {BASKET_SNAPSHOT_CSV.name}")
    return snap


# ISIN country prefix → yfinance exchange suffix.
# Empty string = the security trades unsuffixed (typically US-listed).
# Notes:
#   - IE (Ireland) is split below: UCITS ETFs are LSE-listed (.L) but
#     several Irish-domiciled operating companies are NYSE-listed (ETN, PNR…).
#   - CA (Canada): the big bank ADRs (BNS, RY, TD, BMO) and growth names
#     (SHOP, CNQ, CCJ, CLS) are dual-listed; Trading-212 UK serves the NYSE
#     version, so we leave them unsuffixed.
ISIN_SUFFIX_MAP: dict[str, str] = {
    "GB": ".L",
    "JE": ".L",  # Jersey-domiciled ETPs (WisdomTree Coffee, etc.) on LSE
    "XS": ".L",  # Eurobond / Leverage Shares ETPs on LSE
    "DE": ".DE",
    "FR": ".PA",
    "NL": ".AS",
    "US": "", "CA": "", "IL": "", "KY": "", "BM": "", "SG": "", "CH": "", "AN": "",
}

# Irish-domiciled operating companies that trade primarily on NYSE
# (override the default .L treatment of IE ISINs).
IE_US_LISTED: set[str] = {"ETN", "PNR", "JCI", "ACN", "MDT", "TT", "LIN", "STX", "CRH"}

# Explicit overrides for edge cases where ISIN heuristic gets it wrong.
# Key = (raw_ticker, isin_country); value = yfinance ticker.
TICKER_OVERRIDES: dict[tuple[str, str], str] = {
    # Airbus has a Dutch ISIN but yfinance's primary listing is Paris.
    ("AIR", "NL"): "AIR.PA",
    # CyberArk was acquired by Palo Alto Networks in 2025 → fully delisted.
    # Leaving CYBR here documents the issue; ticker stays as-is and the
    # download_prices() WARN will surface the miss.
}


# Manual-fund rows in log.xlsx have no Ticker/ISIN — only a free-text Name.
# Match a lowercased substring of the Name against this table to recover the
# LSE-listed (.L) yfinance ticker. First match wins; order longer/specific
# patterns ahead of generic ones if you add more.
FUND_NAME_TICKERS: list[tuple[str, str]] = [
    ("invesco markets iii plc eqqq nasdaq 100",         "EQQQ.L"),
    ("invesco ftse all-world ucits etf usd distribution","FTWG.L"),
    ("ishares iv plc edge msci usa value factor",       "IUVF.L"),
    ("wisdomtree issuer icav strategic metals",         "WREE.L"),
    ("spdr s&p us industrials select sector",           "SXLI.L"),
    ("spdr msci usa small cap value weighted",          "USSC.L"),
    ("wisdomtree artificial intelligence",              "INTL.L"),
    ("vanguard funds plc ftse developed asia pacific",  "VDPG.L"),
    ("ishares v plc s&p 500 consumer staples",          "ICSU.L"),
    ("wisdomtree core physical gold",                   "GLDW.L"),
    # Legal & General International Index Trust is a unit trust, not an ETF;
    # no yfinance ticker known → stays in the untracked panel.
]


def _resolve_fund_by_name(name: str) -> str:
    """Return an LSE yfinance ticker for a manual fund row, or '' if no match."""
    nlow = (name or "").lower()
    for pat, tkr in FUND_NAME_TICKERS:
        if pat in nlow:
            return tkr
    return ""


def _resolve_yf_ticker(raw_ticker: str, isin: str) -> str:
    """Map a Trading-212-style (ticker, ISIN) to the yfinance ticker.

    Strategy: use the ISIN country prefix to decide what exchange suffix
    to attach. Hand-maintained exceptions handle Irish operating companies
    (NYSE-listed) and any other surprises we encounter.
    """
    t = (raw_ticker or "").strip().upper()
    isin = (isin or "").strip().upper()
    if not t:
        return ""
    country = isin[:2] if len(isin) >= 2 else "US"
    if (t, country) in TICKER_OVERRIDES:
        return TICKER_OVERRIDES[(t, country)]
    if country == "IE":
        if t in IE_US_LISTED:
            return t
        return f"{t}.L"
    suffix = ISIN_SUFFIX_MAP.get(country, "")
    return f"{t}{suffix}" if suffix else t


def load_transactions_from_log() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load the real transaction log (log.xlsx, Sheet2).

    Schema: Action ("Market buy" / "Market sell" / "Stop sell"), Time,
    ISIN, Ticker, Name. Each row is treated as 1 unit since the broker
    export does not surface fractional-share quantities.

    Returns (transactions, untracked) where:
      - transactions has columns ticker, date, action, shares (=1),
        compatible with the rest of the pipeline. Tickers are resolved
        to yfinance form via ISIN-prefix → exchange-suffix mapping.
      - untracked holds rows missing both ISIN and ticker (manual funds
        with embedded "@ price" in the name); rendered separately for
        user awareness.
    """
    df = pd.read_excel(LOG_XLSX, sheet_name="Sheet2")
    df.columns = [c.strip().lower() for c in df.columns]
    for col in ("action", "time", "isin", "ticker", "name"):
        if col not in df.columns:
            raise ValueError(f"log.xlsx missing required column '{col}'")

    df["isin"] = df["isin"].fillna("").astype(str).str.strip()
    df["ticker"] = df["ticker"].fillna("").astype(str).str.strip().str.upper()
    df["name"] = df["name"].fillna("").astype(str).str.strip()

    # Pass 1: try to recover a ticker by NAME for rows missing both ticker
    # and ISIN (manual fund entries). Anything resolved gets promoted into
    # the tracked stream below; anything left in (no ticker, no ISIN) stays
    # in the untracked panel.
    needs_resolve = (df["isin"] == "") & (df["ticker"] == "")
    for idx in df[needs_resolve].index:
        resolved = _resolve_fund_by_name(df.at[idx, "name"])
        if resolved:
            df.at[idx, "ticker"] = resolved  # already in `.L`/yfinance form
            # Leave isin blank so the downstream resolver knows not to apply
            # the suffix rule a second time.

    # Untracked = still no ISIN AND no ticker after name resolution.
    untracked_mask = (df["isin"] == "") & (df["ticker"] == "")
    untracked = df[untracked_mask].copy()
    untracked["date"] = pd.to_datetime(untracked["time"], errors="coerce", dayfirst=True)
    untracked["action"] = untracked["action"].astype(str).str.strip()

    tracked = df[~untracked_mask].copy()

    # Parse dates — the tracked section uses ISO timestamps ("2024-10-24
    # 13:30:02"); the name-resolved fund rows use dd/mm/YYYY. Try ISO first
    # (no warning), then fall back to dayfirst for whatever is left.
    tracked["date"] = pd.to_datetime(tracked["time"], errors="coerce")
    missing_dates = tracked["date"].isna()
    if missing_dates.any():
        tracked.loc[missing_dates, "date"] = pd.to_datetime(
            tracked.loc[missing_dates, "time"], errors="coerce", dayfirst=True
        )

    # Normalize action: "Market buy" / "Limit buy" → BUY; "Market sell" /
    # "Stop sell" / "Limit sell" → SELL.
    action_lower = tracked["action"].astype(str).str.strip().str.lower()
    tracked["action"] = action_lower.map(
        lambda s: "BUY" if "buy" in s else ("SELL" if "sell" in s else "")
    )

    # Resolve to yfinance tickers via ISIN heuristic — but only for rows that
    # arrived with an ISIN. Name-resolved fund rows already have the suffix.
    def _resolve_row(t: str, i: str) -> str:
        return _resolve_yf_ticker(t, i) if i else t
    tracked["ticker"] = [
        _resolve_row(t, i) for t, i in zip(tracked["ticker"], tracked["isin"])
    ]

    # Filter to clean rows
    tracked = tracked[tracked["action"].isin(["BUY", "SELL"])]
    tracked = tracked.dropna(subset=["date"])
    tracked = tracked[tracked["ticker"] != ""]

    # Each broker entry = 1 unit by default (fractional-share platforms hide the
    # qty). v2.4: if the export carries a real quantity column, read it so the
    # opt-in value-weight mode has real sizes; equal mode ignores it anyway.
    _qty_col = next((c for c in ("no. of shares", "no of shares", "shares",
                                 "quantity", "qty", "units") if c in df.columns), None)
    if _qty_col:
        tracked["shares"] = pd.to_numeric(tracked[_qty_col], errors="coerce").fillna(1.0)
        tracked.loc[tracked["shares"] <= 0, "shares"] = 1.0
    else:
        tracked["shares"] = 1.0

    out = tracked[["ticker", "date", "action", "shares"]].copy()
    out = out.sort_values(["ticker", "date"]).reset_index(drop=True)
    return out, untracked


def _snap_to_trading(d: pd.Timestamp, trading_dates: pd.DatetimeIndex) -> pd.Timestamp | None:
    """Snap a calendar date to the next trading day >= d (or last if past end)."""
    if d in trading_dates:
        return d
    idx = trading_dates.searchsorted(d, side="left")
    if idx >= len(trading_dates):
        return None
    return trading_dates[idx]


def _txn_price(txn_date: pd.Timestamp, ticker_prices: pd.Series) -> float:
    """Look up the price for a transaction date — uses the close on that date,
    or the nearest prior close if the date isn't a trading day."""
    sub = ticker_prices.loc[:txn_date].dropna()
    if sub.empty:
        return float("nan")
    return float(sub.iloc[-1])


def _txn_prices(dates: pd.Series, ticker_prices: pd.Series) -> pd.Series:
    """Vectorized _txn_price across many transaction dates (C2): for each date the
    close at-or-before it (nearest prior trading day), NaN before the series
    starts. Byte-identical to ``dates.apply(lambda d: _txn_price(d, ticker_prices))``
    but a single sorted-index lookup instead of an O(txns) per-date prefix slice.
    Handles duplicate same-day rows; result is aligned to ``dates.index``."""
    s = ticker_prices.dropna()
    if s.empty:
        return pd.Series(np.nan, index=dates.index)
    s = s.sort_index()
    target = pd.DatetimeIndex(pd.to_datetime(dates.to_numpy()))
    pos = s.index.get_indexer(target, method="ffill")   # -1 where date precedes series
    vals = np.where(pos >= 0, s.to_numpy()[pos], np.nan)
    return pd.Series(vals, index=dates.index)


_OHLCV_FIELDS = ["Open", "High", "Low", "Close", "Volume"]


def download_ohlcv(tickers: list[str]) -> tuple[pd.DataFrame, set[str], int]:
    """Single yfinance batch returning full OHLCV in NATIVE currency.

    Output: wide DataFrame with MultiIndex columns ``(ticker, field)`` where
    ``field`` is one of Open/High/Low/Close/Volume. Used both as the source for
    close-only consumers (via :func:`download_prices`) and as the input for the
    quant-metric layer (ATR + volume ratio) that needs the High/Low/Volume
    columns yfinance returns alongside Close.

    Returns a 3-tuple: (frame, failed_after_retry, retries_recovered) so the
    build-health footer can surface which tickers ended up missing and how
    many were rescued by the per-ticker retry path.
    """
    end = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
    data = yf.download(
        tickers, start=START_DATE, end=end,
        auto_adjust=True, progress=False, group_by="ticker", threads=True,
    )
    frames: dict[str, pd.DataFrame] = {}
    failed_initial: list[str] = []
    for t in tickers:
        try:
            sub = data[t][_OHLCV_FIELDS].copy()
            if sub["Close"].notna().any():
                frames[t] = sub
            else:
                failed_initial.append(t)
        except (KeyError, ValueError):
            failed_initial.append(t)

    retries_recovered = 0
    for t in failed_initial:
        try:
            df1 = yf.Ticker(t).history(start=START_DATE, end=end, auto_adjust=True)
            df1 = df1[_OHLCV_FIELDS]
            if df1["Close"].notna().any():
                if df1.index.tz is not None:
                    df1.index = df1.index.tz_localize(None)
                frames[t] = df1
                retries_recovered += 1
                print(f"RETRY OK: {t}", file=sys.stderr)
        except Exception as e:
            print(f"WARN retry failed for {t}: {e}", file=sys.stderr)

    still_failed = {t for t in failed_initial if t not in frames}
    if not frames:
        return pd.DataFrame(), still_failed, retries_recovered
    out = pd.concat(frames, axis=1)  # MultiIndex columns: (ticker, field)
    if out.index.tz is not None:
        out.index = out.index.tz_localize(None)
    return out.sort_index(), still_failed, retries_recovered


def download_prices(tickers: list[str]) -> pd.DataFrame:
    """Close-only wide DataFrame (date × ticker). Thin wrapper over
    :func:`download_ohlcv` so the legacy contract used by every downstream
    consumer stays unchanged."""
    ohlcv, _failed, _retries = download_ohlcv(tickers)
    if ohlcv.empty:
        return pd.DataFrame()
    return ohlcv.xs("Close", axis=1, level=1).copy()


def _benchmark_close_from_df(df: pd.DataFrame) -> pd.Series:
    """Extract the Close series from a yfinance benchmark frame, robust to BOTH
    MultiIndex column orderings ((field, ticker) AND (ticker, field)) and to a
    plain single-level frame. Returns an empty Series when no Close column exists,
    so a yfinance column-shape change degrades visibly instead of raising KeyError
    (which the caller's blanket try/except would have turned into a silent empty
    SPY — disabling alpha + vs-SPY everywhere)."""
    if df is None or df.empty:
        return pd.Series(dtype=float)
    cols = df.columns
    if isinstance(cols, pd.MultiIndex):
        for lvl in (0, 1):
            if "Close" in cols.get_level_values(lvl):
                s = df.xs("Close", axis=1, level=lvl)
                return s.iloc[:, 0] if isinstance(s, pd.DataFrame) else s
        return pd.Series(dtype=float)
    if "Close" not in cols:
        return pd.Series(dtype=float)
    s = df["Close"]
    return s.iloc[:, 0] if getattr(s, "ndim", 1) > 1 else s


def download_benchmark(ticker: str = BENCHMARK) -> pd.Series:
    end = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        df = yf.download(ticker, start=START_DATE, end=end,
                         auto_adjust=True, progress=False, threads=False)
        s = _benchmark_close_from_df(df)
        if s.empty:
            print(f"WARN benchmark {ticker}: no usable Close in returned frame "
                  f"(columns={list(df.columns)[:4]}) — overlay/alpha unavailable "
                  f"this build", file=sys.stderr)
            return pd.Series(dtype=float)
        if s.index.tz is not None:
            s.index = s.index.tz_localize(None)
        s.name = ticker
        return s
    except Exception as e:
        print(f"WARN benchmark {ticker} download failed: {e}", file=sys.stderr)
        return pd.Series(dtype=float)


def load_watchlist() -> pd.DataFrame:
    """Read watchlist.csv (columns: ticker, note). Missing file -> empty frame.

    Tickers here are *additional* to whatever is in the broker log: they get
    priced and FX-converted alongside, but never enter the basket math.
    """
    if not WATCHLIST_CSV.exists():
        return pd.DataFrame(columns=["ticker", "note"])
    df = pd.read_csv(WATCHLIST_CSV, dtype=str).fillna("")
    if "ticker" not in df.columns:
        return pd.DataFrame(columns=["ticker", "note"])
    df["ticker"] = df["ticker"].str.strip().str.upper()
    df = df[df["ticker"] != ""].drop_duplicates(subset=["ticker"]).reset_index(drop=True)
    if "note" not in df.columns:
        df["note"] = ""
    return df[["ticker", "note"]]


def _cap_tier(market_cap) -> str:
    """Bucket a market cap in USD into a tier string."""
    if market_cap is None or pd.isna(market_cap) or market_cap <= 0:
        return ""
    mc = float(market_cap)
    if mc >= 200e9: return "MEGA"
    if mc >= 10e9:  return "LARGE"
    if mc >= 2e9:   return "MID"
    return "SMALL"


def load_universe() -> list[str]:
    """Read universe.csv (single column: ticker). Empty/missing → []."""
    if not UNIVERSE_CSV.exists():
        return []
    df = pd.read_csv(UNIVERSE_CSV, dtype=str).fillna("")
    if "ticker" not in df.columns:
        return []
    return (df["ticker"].str.strip().str.upper()
            .replace("", pd.NA).dropna().drop_duplicates().tolist())


def _pred_num(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return float("nan")


def load_prediction_themes() -> list[dict]:
    """Read predictions.csv -> [{theme, source, key}]. Empty/missing -> []."""
    if not PREDICTIONS_CSV.exists():
        return []
    df = pd.read_csv(PREDICTIONS_CSV, dtype=str).fillna("")
    need = {"theme_label", "source", "series_or_tag"}
    if not need.issubset(df.columns):
        return []
    out = []
    for _, r in df.iterrows():
        theme = str(r["theme_label"]).strip()
        source = str(r["source"]).strip().lower()
        key = str(r["series_or_tag"]).strip()
        if theme and source and key:
            out.append({"theme": theme, "source": source, "key": key})
    return out


def _pred_http_get_json(url: str, timeout: int = 12):
    """Stdlib GET -> parsed JSON, or None on any error (best-effort)."""
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "stocks-dashboard/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"WARN predictions GET failed ({url}): {e}", file=sys.stderr)
        return None


def _parse_kalshi_market(m: dict, theme: str, series_ticker: str | None = None) -> dict | None:
    yb = _pred_num(m.get("yes_bid_dollars"))
    ya = _pred_num(m.get("yes_ask_dollars"))
    if yb == yb and ya == ya and (yb > 0 or ya > 0):
        prob = (yb + ya) / 2 * 100
    elif yb == yb and yb > 0:
        prob = yb * 100
    else:
        return None
    # Link to the stable series landing page (e.g. /markets/kxfed) rather than the
    # raw event-ticker path, which Kalshi's slug router 404s on. Derive the series
    # from the explicit key, else from the event-ticker prefix.
    ev = str(m.get("event_ticker") or "").strip()
    series = (series_ticker or ev.split("-")[0] or "").strip().lower()
    return {
        "theme": theme,
        "question": str(m.get("title") or "").strip(),
        "source": "kalshi",
        "probability": prob,
        "volume": _pred_num(m.get("volume_fp")) if m.get("volume_fp") is not None
                  else (_pred_num(m.get("volume")) or 0.0),
        "end_date": str(m.get("expiration_time") or ""),
        "url": f"https://kalshi.com/markets/{series}" if series else None,
    }


def _kalshi_pick_active(markets: list[dict], theme: str,
                        series_ticker: str | None = None) -> dict | None:
    """Among a series' markets, pick the soonest market whose expiry is in the
    future (the currently-relevant one), parse it, return the record."""
    now = pd.Timestamp.now(tz="UTC")
    best, best_ts = None, None
    for m in markets:
        ts = pd.to_datetime(m.get("expiration_time"), utc=True, errors="coerce")
        if ts is None or ts != ts or ts < now:
            continue
        if best_ts is None or ts < best_ts:
            best, best_ts = m, ts
    if best is None:
        return None
    return _parse_kalshi_market(best, theme, series_ticker)


def fetch_kalshi(themes: list[dict]) -> list[dict]:
    """themes: [{theme, key(series_ticker)}]. One record per theme (active
    market). Best-effort: a failed theme is skipped."""
    out = []
    for t in themes:
        url = f"{KALSHI_API}/markets?series_ticker={t['key']}&status=open&limit=200"
        data = _pred_http_get_json(url)
        markets = (data or {}).get("markets") if isinstance(data, dict) else None
        if not markets:
            continue
        rec = _kalshi_pick_active(markets, t["theme"], t["key"])
        if rec:
            out.append(rec)
    return out


def _parse_polymarket_market(m: dict, theme: str) -> dict | None:
    prices = m.get("outcomePrices")
    if isinstance(prices, str):
        try:
            prices = json.loads(prices)
        except (TypeError, ValueError):
            prices = None
    if not isinstance(prices, list) or not prices:
        return None
    p = _pred_num(prices[0])
    if p != p:
        return None
    slug = str(m.get("slug") or "").strip()
    return {
        "theme": theme,
        "question": str(m.get("question") or "").strip(),
        "source": "polymarket",
        "probability": p * 100,
        "volume": _pred_num(m.get("volume")) or 0.0,
        "end_date": str(m.get("endDate") or ""),
        "url": f"https://polymarket.com/event/{slug}" if slug else None,
    }


def fetch_polymarket(themes: list[dict]) -> list[dict]:
    """themes: [{theme, key(slug)}]. One record per theme. Best-effort."""
    out = []
    for t in themes:
        url = f"{POLYMARKET_API}/markets?slug={t['key']}&closed=false"
        data = _pred_http_get_json(url)
        markets = data if isinstance(data, list) else (
            data.get("data") if isinstance(data, dict) else None)
        if not markets:
            continue
        rec = _parse_polymarket_market(markets[0], t["theme"])
        if rec:
            out.append(rec)
    return out


def fetch_predictions(themes: list[dict]) -> list[dict]:
    """Route themes by source, fetch each, apply the volume floor, return
    records sorted by probability desc (the final sort is by |delta| at render
    time)."""
    kal = [t for t in themes if t["source"] == "kalshi"]
    pol = [t for t in themes if t["source"] == "polymarket"]
    records = []
    if kal:
        records += fetch_kalshi(kal)
    if pol:
        records += fetch_polymarket(pol)
    records = [
        r for r in records
        if _pred_num(r.get("volume"))
        >= PRED_VOLUME_FLOORS.get(str(r.get("source") or "").lower(), PRED_VOLUME_FLOOR)
    ]
    records.sort(key=lambda r: -r["probability"])
    records = records[:PRED_MAX_ROWS]
    # Stamp the ACTUAL fetch date so the "as of" reflects prediction freshness,
    # not the price-data date (the two differ on weekends, and on demo/CI the
    # page renders from a committed cache that carries its own real fetch date).
    asof = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d")
    for r in records:
        r["fetched_at"] = asof
    return records


def save_predictions_cache(records: list[dict]) -> None:
    try:
        PREDICTIONS_CACHE.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(records).to_parquet(PREDICTIONS_CACHE)
    except Exception as e:
        print(f"WARN couldn't write predictions cache: {e}", file=sys.stderr)


def load_predictions_cache(path=None) -> list[dict]:
    path = path or PREDICTIONS_CACHE
    if not path.exists():
        return []
    try:
        return pd.read_parquet(path).to_dict("records")
    except Exception:
        return []


def snapshot_prior_predictions() -> None:
    """Copy current predictions cache to prior before the live fetch overwrites
    it. Skipped silently if absent (first build)."""
    if not PREDICTIONS_CACHE.exists():
        return
    try:
        PRIOR_PREDICTIONS_CACHE.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PREDICTIONS_CACHE, PRIOR_PREDICTIONS_CACHE)
    except Exception as e:
        print(f"WARN couldn't snapshot prior predictions: {e}", file=sys.stderr)


def compute_prediction_moves(prior: list[dict], current: list[dict]) -> list[dict]:
    """Attach delta_pp (current-prior probability) per theme. None when the
    theme had no prior snapshot."""
    prior_by = {r["theme"]: _pred_num(r.get("probability")) for r in (prior or [])}
    rows = []
    for r in current:
        p = prior_by.get(r["theme"])
        delta = (r["probability"] - p) if (p is not None and p == p) else None
        rows.append({**r, "delta_pp": delta})
    return rows


def _within_horizon(end_date, now, max_days: int) -> bool:
    """v3.0 #6: keep a prediction market only if it resolves soon. A missing or
    unparseable `end_date` is kept (can't judge -> don't silently drop). A real
    date is kept iff it resolves no more than `max_days` ahead of `now`, so a
    far-dated market (e.g. a December election in June) is filtered out."""
    if not end_date:
        return True
    ts = pd.to_datetime(end_date, utc=True, errors="coerce")
    if ts is None or ts != ts:        # NaT / unparseable
        return True
    return (ts - now).days <= max_days


def filter_predictions_horizon(rows: list[dict], max_days: int = PRED_HORIZON_DAYS,
                               now=None) -> list[dict]:
    """Drop markets resolving further than `max_days` out (evaluated at render
    time, so the window stays current). Keeps undated rows + all other fields."""
    now = now if now is not None else pd.Timestamp.now(tz="UTC")
    return [r for r in rows if _within_horizon(r.get("end_date"), now, max_days)]


def _universe_cache_date(path: "Path | None" = None):
    """The date the universe cache was last refreshed, read from an embedded
    `cache_date` column — NOT file mtime, which CI's git checkout resets to "now"
    every run (so an mtime TTL never expires under auto-publish and the cache
    freezes forever; this is the v2.8 analyst-history lesson applied here).
    Returns a normalized Timestamp, or None if absent / pre-`cache_date` format.
    (Resolves UNIVERSE_CACHE at call time, not via a def-time default.)"""
    path = path or UNIVERSE_CACHE
    if not path.exists():
        return None
    try:
        df = pd.read_parquet(path)
    except Exception:
        return None
    if "cache_date" not in df.columns or df.empty:
        return None
    try:
        return pd.to_datetime(df["cache_date"].iloc[0]).normalize()
    except Exception:
        return None


def _universe_cache_age_days(path: "Path | None" = None):
    cd = _universe_cache_date(path)
    if cd is None:
        return None
    return (pd.Timestamp(datetime.now(timezone.utc).date()).normalize() - cd).days


def _universe_cache_is_fresh(ttl_days: int = UNIVERSE_TTL_DAYS) -> bool:
    age = _universe_cache_age_days()
    # No cache, or an old pre-`cache_date` parquet → treat as stale so the next
    # build refetches and rewrites it in the dated format (one-time migration).
    return age is not None and age < ttl_days


def fetch_universe_outlook(universe: list[str], meta_cache: pd.DataFrame,
                           ttl_days: int = UNIVERSE_TTL_DAYS) -> pd.DataFrame:
    """Return per-ticker precomputed industry outlook data (ret_12m, target,
    upside, rec, n_an), backed by a parquet cache with file-mtime TTL.

    Daily builds reuse the cache; ~once a month the cache expires and we
    refetch 12-month prices + analyst targets for the entire universe.
    Self-contained: doesn't touch the main prices/analyst caches so portfolio
    behaviour stays unaffected.
    """
    if not universe:
        return pd.DataFrame()
    if _universe_cache_is_fresh(ttl_days):
        try:
            df = pd.read_parquet(UNIVERSE_CACHE)
            age = _universe_cache_age_days()
            print(f"Universe outlook: cache hit ({len(df)} tickers, "
                  f"{age if age is not None else '?'}d old)")
            return df
        except Exception as e:
            print(f"WARN universe cache read failed: {e}; refetching", file=sys.stderr)

    print(f"Universe outlook: refreshing {len(universe)} tickers (this is a ~monthly fetch)...")
    end = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
    start = (datetime.now(timezone.utc) - timedelta(days=400)).strftime("%Y-%m-%d")

    # 13-month price history — enough for a clean 12mo return without edge effects
    try:
        raw = yf.download(universe, start=start, end=end, auto_adjust=True,
                          progress=False, group_by="ticker", threads=True)
    except Exception as e:
        print(f"WARN universe price fetch failed: {e}", file=sys.stderr)
        return pd.DataFrame()

    cutoff = pd.Timestamp.now() - pd.DateOffset(months=12)
    rows: list[dict] = []
    for tkr in universe:
        try:
            s = raw[tkr]["Close"].dropna()
        except (KeyError, ValueError):
            continue
        if s.index.tz is not None:
            s.index = s.index.tz_localize(None)
        s = s[s.index >= cutoff]
        if len(s) < 30:
            continue
        ret_12m = (float(s.iloc[-1]) / float(s.iloc[0]) - 1) * 100 if s.iloc[0] else 0.0
        # v2.6 Value screen: where today's price sits in the 52-week range
        # (0 = at the low, 100 = at the high) — the "near 52w low" trigger.
        lo, hi = float(s.min()), float(s.max())
        range52w = ((float(s.iloc[-1]) - lo) / (hi - lo) * 100) if hi > lo else float("nan")
        rows.append({"ticker": tkr, "ret_12m": ret_12m, "range52w_pct": range52w})

    if not rows:
        print("WARN universe outlook: no usable price series", file=sys.stderr)
        return pd.DataFrame()

    df = pd.DataFrame(rows).set_index("ticker")

    # Industry classification — reuse meta cache, fetch only what's missing.
    # Cached forever in data/meta.csv; subsequent universe refreshes are free.
    missing_meta = [t for t in df.index if t not in meta_cache.index]
    if missing_meta:
        print(f"Universe outlook: fetching industry meta for {len(missing_meta)} new tickers")
        meta_cache, _ = fetch_meta(missing_meta, meta_cache)
    df["industry"] = [
        str(meta_cache.loc[t, "industry"] or meta_cache.loc[t, "sector"] or "")
        if t in meta_cache.index else ""
        for t in df.index
    ]
    # v2.6 Value screen needs the broad sector (for sector-median P/E) + a name.
    df["sector"] = [
        str(meta_cache.loc[t, "sector"] or "") if t in meta_cache.index else ""
        for t in df.index
    ]
    df["name"] = [
        str(meta_cache.loc[t, "name"] or t) if t in meta_cache.index else t
        for t in df.index
    ]

    # Analyst data — parallel fetch with the same fetch_analyst_data helper.
    # We pass a fresh empty cache so all tickers are fetched in one batch;
    # results are stored in the universe parquet (not the portfolio analyst
    # cache) to keep the two pools cleanly separated.
    print(f"Universe outlook: fetching analyst data for {len(df)} tickers...")
    a, _ = fetch_analyst_data(df.index.tolist(),
                              pd.DataFrame(columns=["target_mean","target_high","target_low",
                                                    "num_analysts","recommendation","rec_mean",
                                                    "current_price","fetched_at"]).rename_axis("ticker"))
    df["target_mean"]   = a.reindex(df.index)["target_mean"]
    df["current_price"] = a.reindex(df.index)["current_price"]
    df["recommendation"]= a.reindex(df.index)["recommendation"].fillna("")
    df["num_analysts"]  = a.reindex(df.index)["num_analysts"]
    df["market_cap"]    = a.reindex(df.index)["market_cap"]
    # v2.6 Value screen fundamentals (reindexed from the analyst fetch)
    for _c in ("pe", "pb", "roe", "rev_growth", "fcf", "debt_to_equity"):
        df[_c] = a.reindex(df.index)[_c] if _c in a.columns else float("nan")
    # Upside %: (target / current - 1) * 100, when both are defined
    df["upside"] = df.apply(
        lambda r: ((r["target_mean"] / r["current_price"] - 1) * 100
                   if pd.notna(r["target_mean"]) and pd.notna(r["current_price"])
                   and r["target_mean"] > 0 and r["current_price"] > 0 else float("nan")),
        axis=1,
    )
    # Market-cap tier — Mega >$200B, Large $10-200B, Mid $2-10B, Small <$2B.
    df["cap_tier"] = df["market_cap"].apply(_cap_tier)
    # Embed the refresh date so freshness survives CI (mtime would be reset by
    # git checkout every run). Read back by _universe_cache_date().
    df["cache_date"] = pd.Timestamp(datetime.now(timezone.utc).date()).strftime("%Y-%m-%d")

    UNIVERSE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    try:
        df.to_parquet(UNIVERSE_CACHE)
        print(f"Universe outlook: cached {len(df)} tickers to {UNIVERSE_CACHE.name}")
    except Exception as e:
        print(f"WARN couldn't cache universe outlook: {e}", file=sys.stderr)
    return df


def fetch_news(max_items: int = NEWS_MAX_ITEMS) -> list[dict]:
    """Pull recent headlines from a couple of free finance RSS feeds.

    Returns a list of {title, link, source, published (ISO), published_pretty}.
    Build-time only; if feedparser or the network is unavailable, returns [].
    """
    try:
        import feedparser
    except ImportError:
        print("WARN feedparser not installed; skipping news panel", file=sys.stderr)
        return []
    seen_links = set()
    items: list[dict] = []
    for source, url in NEWS_FEEDS:
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            print(f"WARN news fetch failed for {source}: {e}", file=sys.stderr)
            continue
        for entry in feed.entries:
            link = (entry.get("link") or "").strip()
            title = (entry.get("title") or "").strip()
            if not link or not title or link in seen_links:
                continue
            seen_links.add(link)
            pub_struct = entry.get("published_parsed") or entry.get("updated_parsed")
            if pub_struct:
                pub_dt = datetime(*pub_struct[:6], tzinfo=timezone.utc)
            else:
                pub_dt = datetime.now(timezone.utc)
            items.append({
                "title": title, "link": link, "source": source,
                "published": pub_dt.isoformat(), "published_dt": pub_dt,
            })
    items.sort(key=lambda x: x["published_dt"], reverse=True)
    out = []
    for it in items[:max_items]:
        out.append({
            "title": it["title"], "link": it["link"], "source": it["source"],
            "published": it["published"],
            "published_pretty": _relative_time(it["published_dt"]),
        })
    print(f"News: fetched {len(out)} headlines from {len(NEWS_FEEDS)} feeds")
    return out


def _relative_time(dt: datetime) -> str:
    delta = datetime.now(timezone.utc) - dt
    secs = int(delta.total_seconds())
    if secs < 60:    return "just now"
    if secs < 3600:  return f"{secs // 60}m ago"
    if secs < 86400: return f"{secs // 3600}h ago"
    days = secs // 86400
    if days < 7:     return f"{days}d ago"
    return dt.strftime("%d %b")


def load_meta_cache() -> pd.DataFrame:
    if META_CSV.exists():
        df = pd.read_csv(META_CSV, index_col="ticker")
        for col in ("sector", "industry", "name", "currency"):
            if col not in df.columns:
                df[col] = ""
        return df[["sector", "industry", "name", "currency"]].fillna("")
    return pd.DataFrame(columns=["sector", "industry", "name", "currency"]).rename_axis("ticker")


def fetch_meta(tickers: list[str], cache: pd.DataFrame) -> tuple[pd.DataFrame, set[str]]:
    # A ticker needs re-fetching if it's not in cache OR if currency is missing
    # (the latter handles upgrading old caches that pre-date the currency column)
    missing = []
    for t in tickers:
        if t not in cache.index:
            missing.append(t)
        elif not str(cache.loc[t, "currency"] or "").strip():
            missing.append(t)
    if not missing:
        return cache, set()
    print(f"Fetching metadata for {len(missing)} ticker(s) (parallel x4)...", flush=True)

    def one(t: str):
        try:
            info = yf.Ticker(t).info or {}
            return t, {
                "sector": (info.get("sector") or "").strip(),
                "industry": (info.get("industry") or "").strip(),
                "name": (info.get("shortName") or info.get("longName") or t).strip(),
                "currency": (info.get("currency") or "USD").strip(),
            }, None
        except Exception as e:
            print(f"  meta fail {t}: {e}", file=sys.stderr)
            # Leave currency BLANK (not a "USD" default) on failure: a blank
            # currency re-triggers the refetch condition next build, so a transient
            # .info failure can't permanently mask a non-USD ticker as USD (wrong
            # FX). Blank normalizes to USD at use-time, so nothing breaks meanwhile.
            return t, {"sector": "", "industry": "", "name": t, "currency": ""}, str(e)

    rows = []
    failed: set[str] = set()
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(one, t): t for t in missing}
        for i, fut in enumerate(as_completed(futures), 1):
            t, meta, err = fut.result()
            rows.append({"ticker": t, **meta})
            if err is not None:
                failed.add(t)
            if i % 10 == 0 or i == len(missing):
                print(f"  meta: {i}/{len(missing)}", flush=True)

    new_df = pd.DataFrame(rows).set_index("ticker")
    combined = pd.concat([cache.drop(index=[t for t in missing if t in cache.index]),
                           new_df]) if not cache.empty else new_df
    combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    META_CSV.parent.mkdir(parents=True, exist_ok=True)
    combined.to_csv(META_CSV)
    print(f"  cached metadata to {META_CSV}", flush=True)
    return combined, failed


def load_analyst_cache() -> pd.DataFrame:
    cols = ["target_mean", "target_high", "target_low", "num_analysts",
            "recommendation", "rec_mean", "current_price", "market_cap",
            "pe", "pb", "roe", "rev_growth", "fcf", "debt_to_equity", "fetched_at"]
    if not ANALYST_CACHE.exists():
        return pd.DataFrame(columns=cols).rename_axis("ticker")
    df = pd.read_parquet(ANALYST_CACHE)
    # Backward-compat: older caches lack market_cap + the v2.6 value-screen
    # fundamentals. Pre-fill so downstream code can rely on the columns existing.
    for _c in ("market_cap", "pe", "pb", "roe", "rev_growth", "fcf", "debt_to_equity"):
        if _c not in df.columns:
            df[_c] = float("nan")
    if "fetched_at" in df.columns:
        df["fetched_at"] = pd.to_datetime(df["fetched_at"], utc=True)
    return df


def fetch_analyst_data(tickers: list[str], cache: pd.DataFrame,
                       ttl_days: int = ANALYST_TTL_DAYS) -> tuple[pd.DataFrame, set[str]]:
    """For each ticker, return Wall-Street consensus (target prices, rating,
    analyst count). Cache to parquet with a per-row fetched_at; refetch only
    when missing or older than ttl_days. yfinance .info is slow + flaky, so
    failures degrade to a NaN row rather than blowing up the build."""
    now = pd.Timestamp.now(tz="UTC")
    stale_cutoff = now - pd.Timedelta(days=ttl_days)
    to_fetch: list[str] = []
    for t in tickers:
        if t not in cache.index:
            to_fetch.append(t)
        elif pd.isna(cache.loc[t, "fetched_at"]) or cache.loc[t, "fetched_at"] < stale_cutoff:
            to_fetch.append(t)
    if not to_fetch:
        return cache, set()
    print(f"Fetching analyst data for {len(to_fetch)} ticker(s) (parallel x4, TTL {ttl_days}d)...", flush=True)

    def one(t: str):
        try:
            info = yf.Ticker(t).info or {}
            return t, {
                "target_mean":  info.get("targetMeanPrice"),
                "target_high":  info.get("targetHighPrice"),
                "target_low":   info.get("targetLowPrice"),
                "num_analysts": info.get("numberOfAnalystOpinions"),
                "recommendation": (info.get("recommendationKey") or "").lower(),
                "rec_mean":     info.get("recommendationMean"),
                "current_price": info.get("currentPrice"),
                "market_cap":   info.get("marketCap"),
                # v2.6 Value screen fundamentals (yfinance .info)
                "pe":             info.get("trailingPE"),
                "pb":             info.get("priceToBook"),
                "roe":            info.get("returnOnEquity"),
                "rev_growth":     info.get("revenueGrowth"),
                "fcf":            info.get("freeCashflow"),
                "debt_to_equity": info.get("debtToEquity"),
                "fetched_at":   now,
            }, None
        except Exception as e:
            print(f"  analyst fail {t}: {e}", file=sys.stderr)
            # fetched_at = NaT (not `now`) so a failed row reads as stale and is
            # retried next build, instead of being treated as fresh for the full
            # TTL (which would suppress retries for a week on a transient blip).
            return t, {
                "target_mean": None, "target_high": None, "target_low": None,
                "num_analysts": None, "recommendation": "", "rec_mean": None,
                "current_price": None, "market_cap": None,
                "pe": None, "pb": None, "roe": None, "rev_growth": None,
                "fcf": None, "debt_to_equity": None, "fetched_at": pd.NaT,
            }, str(e)

    rows = []
    failed: set[str] = set()
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(one, t): t for t in to_fetch}
        for i, fut in enumerate(as_completed(futures), 1):
            t, data, err = fut.result()
            rows.append({"ticker": t, **data})
            if err is not None:
                failed.add(t)
            if i % 20 == 0 or i == len(to_fetch):
                print(f"  analyst: {i}/{len(to_fetch)}", flush=True)

    new_df = pd.DataFrame(rows).set_index("ticker")
    combined = (pd.concat([cache.drop(index=[t for t in to_fetch if t in cache.index]), new_df])
                if not cache.empty else new_df)
    combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    ANALYST_CACHE.parent.mkdir(parents=True, exist_ok=True)
    try:
        combined.to_parquet(ANALYST_CACHE)
        print(f"  cached analyst data to {ANALYST_CACHE}", flush=True)
    except Exception as e:
        print(f"WARN couldn't cache analyst data: {e}", file=sys.stderr)
    return combined, failed


def _analyst_snapshot_due(prior_path, now_ts: float, max_age_days: float = 6.0) -> bool:
    """True when the prior snapshot is missing or older than max_age_days.
    Keeps a ~weekly-stable baseline so rating-moves measure against a
    meaningful reference instead of resetting to 'current' every build."""
    if not prior_path.exists():
        return True
    age_days = (now_ts - prior_path.stat().st_mtime) / 86400
    return age_days >= max_age_days


def snapshot_prior_analyst() -> None:
    """Copy the current ANALYST_CACHE to PRIOR_ANALYST_CACHE as the rating-moves
    baseline -- but only when the prior is missing or >~6 days old. Overwriting
    it every build (the old behaviour) reset the baseline to 'current' each run,
    so moves only ever flashed for a single build. A stable ~weekly baseline
    lets target/rec changes actually accumulate and show. compute_rating_moves
    handles the missing-prior (first build) case."""
    if not ANALYST_CACHE.exists():
        return
    if not _analyst_snapshot_due(PRIOR_ANALYST_CACHE, datetime.now(timezone.utc).timestamp()):
        return   # keep the stable baseline
    try:
        PRIOR_ANALYST_CACHE.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ANALYST_CACHE, PRIOR_ANALYST_CACHE)
    except Exception as e:
        print(f"WARN couldn't snapshot prior analyst cache: {e}", file=sys.stderr)


def append_analyst_history(analyst: pd.DataFrame, today,
                           history_path: Path = ANALYST_HISTORY,
                           keep_days: int = RATING_HISTORY_KEEP_DAYS) -> pd.DataFrame:
    """Append today's analyst snapshot (target_mean + recommendation per ticker)
    to the rolling history parquet, replacing any existing same-day rows and
    pruning anything older than keep_days. The history is committed so the
    rolling rating-moves baseline survives stateless CI runs. Returns the updated
    long-form frame: [snapshot_date, ticker, target_mean, recommendation]."""
    today = pd.Timestamp(today).normalize()
    cols = ["target_mean", "recommendation"]
    out_cols = ["snapshot_date", "ticker"] + cols
    if analyst is not None and not analyst.empty:
        cur = analyst.reset_index()
        cur = cur.rename(columns={cur.columns[0]: "ticker"})
        for c in cols:
            if c not in cur.columns:
                cur[c] = pd.NA
        cur = cur[["ticker"] + cols].copy()
        cur.insert(0, "snapshot_date", today)
    else:
        cur = pd.DataFrame(columns=out_cols)
    if history_path.exists():
        try:
            hist = pd.read_parquet(history_path)
        except Exception:
            hist = pd.DataFrame(columns=out_cols)
    else:
        hist = pd.DataFrame(columns=out_cols)
    if not hist.empty:
        hist["snapshot_date"] = pd.to_datetime(hist["snapshot_date"]).dt.normalize()
        hist = hist[hist["snapshot_date"] != today]   # idempotent same-day replace
    combined = pd.concat([hist, cur], ignore_index=True)
    if not combined.empty:
        combined["snapshot_date"] = pd.to_datetime(combined["snapshot_date"]).dt.normalize()
        cutoff = today - pd.Timedelta(days=keep_days)
        combined = combined[combined["snapshot_date"] >= cutoff].reset_index(drop=True)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    combined.to_parquet(history_path)
    return combined


def select_rating_baseline(history: pd.DataFrame, today,
                           window_days: int = RATING_WINDOW_DAYS):
    """Pick the rolling baseline ~window_days ago: the most recent snapshot on or
    before today-window_days, else the oldest available (ramp-up during the first
    ~2 weeks of history). Returns (baseline_date, baseline_df) with baseline_df
    ticker-indexed (target_mean + recommendation). Returns (None, empty) when the
    only snapshot is today's -- no usable baseline yet."""
    if history is None or history.empty:
        return None, pd.DataFrame()
    h = history.copy()
    h["snapshot_date"] = pd.to_datetime(h["snapshot_date"]).dt.normalize()
    today = pd.Timestamp(today).normalize()
    dates = sorted(pd.to_datetime(h["snapshot_date"].unique()))
    if not dates:
        return None, pd.DataFrame()
    cutoff = today - pd.Timedelta(days=window_days)
    eligible = [d for d in dates if d <= cutoff]
    baseline_date = max(eligible) if eligible else min(dates)
    if pd.Timestamp(baseline_date).normalize() == today:
        return None, pd.DataFrame()
    base = h[h["snapshot_date"] == baseline_date].drop(columns=["snapshot_date"])
    base = base.set_index("ticker")
    return pd.Timestamp(baseline_date), base


def seed_rating_history(prior_path: Path, today,
                        history_path: Path = ANALYST_HISTORY,
                        window_days: int = RATING_WINDOW_DAYS) -> bool:
    """One-time cold-start backfill: if there is no rolling history yet but a
    legacy prior-analyst baseline exists, seed it as a snapshot dated ~window_days
    ago so rating moves render immediately instead of staying blank for two weeks
    while the history accrues. Returns True if it seeded."""
    if history_path.exists() or not prior_path.exists():
        return False
    try:
        prior = pd.read_parquet(prior_path)
    except Exception:
        return False
    if prior.empty:
        return False
    seed_date = pd.Timestamp(today).normalize() - pd.Timedelta(days=window_days)
    append_analyst_history(prior, seed_date, history_path)
    # Record the seed date so the UI can flag this baseline as provisional (it's a
    # backdated copy of the legacy prior, not a real measurement taken on that day).
    try:
        RATING_SEED_META.parent.mkdir(parents=True, exist_ok=True)
        RATING_SEED_META.write_text(
            json.dumps({"seed_date": seed_date.strftime("%Y-%m-%d")}), encoding="utf-8")
    except Exception as e:
        print(f"WARN couldn't write rating seed meta: {e}", file=sys.stderr)
    return True


def rating_baseline_is_seeded(baseline_date, meta_path: Path = RATING_SEED_META) -> bool:
    """True when the selected rating baseline IS the cold-start seed snapshot (a
    backdated copy of the legacy prior, not a real measurement on that date). Lets
    render_rating_moves label it provisional instead of implying a real 2-week read.
    Once real daily snapshots age past the window, the baseline date no longer
    matches the seed and this returns False."""
    if baseline_date is None or not meta_path.exists():
        return False
    try:
        seed = json.loads(meta_path.read_text(encoding="utf-8")).get("seed_date")
    except Exception:
        return False
    return bool(seed) and pd.Timestamp(baseline_date).strftime("%Y-%m-%d") == seed


# Rating strength order (lower = more bullish). Used for direction + sorting.
_REC_RANK = {"strong_buy": 0, "buy": 1, "outperform": 1, "hold": 2,
             "neutral": 2, "underperform": 3, "sell": 3, "strong_sell": 4}

# No-coverage = weakest state (consistent with _rec_direction); used only for the
# magnitude math so a none<->X move has a sane rank distance.
_REC_RANK_NONE = 5
# Each _REC_RANK step ~= this many % for the cross-kind "biggest moves" sort.
_REC_STEP_PCT = 5.0

# Free-text / cross-source recommendation synonyms -> a key present in _REC_RANK.
# yfinance emits underscore keys, but other sources and analyst free-text use
# spaces, hyphens, or synonyms; mapping them here stops an unrecognized label
# from masquerading as a no-coverage ("none") transition (M9).
_REC_ALIASES = {
    "strong_buy": "strong_buy", "conviction_buy": "strong_buy", "top_pick": "strong_buy",
    "buy": "buy", "outperform": "outperform", "overweight": "buy", "accumulate": "buy",
    "moderate_buy": "buy", "add": "buy", "positive": "buy",
    "hold": "hold", "neutral": "neutral", "market_perform": "hold",
    "sector_perform": "hold", "peer_perform": "hold", "equal_weight": "hold",
    "in_line": "hold", "perform": "hold",
    "underperform": "underperform", "underweight": "sell", "moderate_sell": "sell",
    "reduce": "sell", "sell": "sell", "negative": "sell",
    "strong_sell": "strong_sell", "conviction_sell": "strong_sell",
}
_REC_NONE = {"", "none", "n/a", "na", "-", "—", "nan"}
_REC_UNKNOWN_SEEN: set = set()


def _norm_rec(r) -> str:
    """Normalize a recommendation to a canonical _REC_RANK key; the no-coverage
    sentinel -> ''. Handles spaced/hyphenated/synonym strings (M9) so an
    unrecognized label can't masquerade as a 'none' transition. Unknown strings
    are returned normalized (underscored) and logged once."""
    raw = str(r or "").strip().lower()
    if raw in _REC_NONE:
        return ""
    key = re.sub(r"[\s\-]+", "_", raw)
    if key in _REC_ALIASES:
        return _REC_ALIASES[key]
    # Substring fallback for free text ("strong buy rating", "sector outperform"):
    # longest alias first so "outperform" wins over "perform", etc.
    for alias in sorted(_REC_ALIASES, key=len, reverse=True):
        if alias in key:
            return _REC_ALIASES[alias]
    if key not in _REC_UNKNOWN_SEEN:
        _REC_UNKNOWN_SEEN.add(key)
        print(f"WARN unrecognized recommendation '{raw}' (treated as-is)", file=sys.stderr)
    return key


def _rec_rank_mag(r) -> int:
    """Rank for magnitude math: no-coverage = weakest (past strong_sell)."""
    rk = _REC_RANK.get(_norm_rec(r))
    return _REC_RANK_NONE if rk is None else rk


def _rec_move_magnitude(before, after) -> float:
    """Severity of a rec move as an abs-% equivalent, from _REC_RANK distance, so
    the global 'biggest moves first' sort can rank a strong_sell->strong_buy above
    a hold->buy nudge instead of tying every rec move at a constant (H7)."""
    return abs(_rec_rank_mag(after) - _rec_rank_mag(before)) * _REC_STEP_PCT


def _rec_direction(before, after) -> "str | None":
    """v2.7 #4: 'up' for an upgrade/initiation, 'down' for a downgrade/coverage
    drop, None for a lateral move (same rank, e.g. buy<->outperform).
    No-coverage ('' / 'none') is the weakest state: none->X reads as an upgrade,
    X->none as a downgrade."""
    b, a = _norm_rec(before), _norm_rec(after)
    if b == a:
        return None
    rb = _REC_RANK.get(b)   # None when no-coverage
    ra = _REC_RANK.get(a)
    if ra is None:          # coverage dropped
        return "down"
    if rb is None:          # initiation from no-coverage
        return "up"
    if ra < rb:
        return "up"         # rank decreased -> more bullish
    if ra > rb:
        return "down"
    return None             # same rank, different label -> lateral


def compute_rating_moves(prior_path: "Path | None",
                         current: pd.DataFrame,
                         min_target_pct: float = 5.0,
                         max_results: int = 200,
                         prior_df: "pd.DataFrame | None" = None) -> list[dict]:
    """T9: diff prior vs current analyst cache, surface material moves.

    A "move" is one of:
      - target_price: abs % change in target_mean >= min_target_pct
      - recommendation: recommendation string changed (e.g. hold -> buy)

    Each raw row: {ticker, kind, before, after, pct_change, abs_pct} plus the
    ticker's CURRENT context {cur_rec, cur_target} (v2.7 #4) so the display can
    show the recommendation alongside a target move and vice-versa. Big Brain
    consumes these raw per-kind rows (kind=="target"); render_rating_moves splits
    them into the Price-targets and Recommendations groups.
    A baseline may be supplied directly as ``prior_df`` (the v2.8 rolling-window
    path); otherwise it is read from ``prior_path``. Returns [] when there is no
    usable baseline (first build / empty history)."""
    if current is None or current.empty:
        return []
    if prior_df is not None:
        prior = prior_df
    elif prior_path is None or not prior_path.exists():
        return []
    else:
        try:
            prior = pd.read_parquet(prior_path)
        except Exception as e:
            print(f"WARN couldn't read prior analyst cache: {e}", file=sys.stderr)
            return []
    if prior is None or prior.empty:
        return []
    common = prior.index.intersection(current.index)
    moves: list[dict] = []
    for tkr in common:
        p_row, c_row = prior.loc[tkr], current.loc[tkr]
        p_tgt = p_row.get("target_mean")
        c_tgt = c_row.get("target_mean")
        cur_target = float(c_tgt) if (c_tgt is not None and pd.notna(c_tgt)) else None
        cur_rec = _norm_rec(c_row.get("recommendation"))
        # Target-price move
        if (p_tgt is not None and c_tgt is not None
                and pd.notna(p_tgt) and pd.notna(c_tgt)
                and float(p_tgt) > 0):
            pct = (float(c_tgt) / float(p_tgt) - 1) * 100
            if abs(pct) >= min_target_pct:
                moves.append({
                    "ticker": tkr, "kind": "target",
                    "before": float(p_tgt), "after": float(c_tgt),
                    "pct_change": pct, "abs_pct": abs(pct),
                    "cur_rec": cur_rec, "cur_target": cur_target,
                })
        # Recommendation move (no-coverage transitions included)
        p_rec, c_rec = _norm_rec(p_row.get("recommendation")), cur_rec
        if p_rec != c_rec and (p_rec or c_rec):
            moves.append({
                "ticker": tkr, "kind": "recommendation",
                "before": p_rec, "after": c_rec,
                "pct_change": 0.0, "abs_pct": _rec_move_magnitude(p_rec, c_rec),
                "cur_rec": cur_rec, "cur_target": cur_target,
            })
    moves.sort(key=lambda m: (-m["abs_pct"], m["ticker"]))
    return moves[:max_results]


# --------------------------------------------------------------------------
# Per-ticker news cache (modal "Recent news" section)
# --------------------------------------------------------------------------
# yfinance exposes per-ticker news via `Ticker(t).news`. Same 7-day TTL as
# the analyst cache so most builds reuse the parquet and only ~1 build/week
# does a fresh fetch across all tracked tickers.


def load_ticker_news_cache() -> pd.DataFrame:
    cols = ["items_json", "fetched_at"]
    if not TICKER_NEWS_CACHE.exists():
        return pd.DataFrame(columns=cols).rename_axis("ticker")
    df = pd.read_parquet(TICKER_NEWS_CACHE)
    if "fetched_at" in df.columns:
        df["fetched_at"] = pd.to_datetime(df["fetched_at"], utc=True)
    return df


def _parse_yf_news_item(raw) -> dict | None:
    """Normalize a single yfinance news item into ``{title, link, publisher,
    published}``. yfinance changed its news shape between releases:

    - **Old (flat):** ``{title, publisher, link, providerPublishTime}``
    - **New (nested under "content"):** ``{content: {title, provider:
      {displayName}, canonicalUrl: {url}, pubDate}}``

    Returns ``None`` if the item lacks a title or link.
    """
    if not isinstance(raw, dict):
        return None
    content = raw.get("content")
    if isinstance(content, dict):
        title = content.get("title")
        publisher = ((content.get("provider") or {}).get("displayName") or "").strip()
        link = None
        canonical = content.get("canonicalUrl")
        if isinstance(canonical, dict):
            link = canonical.get("url")
        if not link:
            click = content.get("clickThroughUrl")
            if isinstance(click, dict):
                link = click.get("url")
        published_raw = content.get("pubDate") or content.get("displayTime")
    else:
        title = raw.get("title")
        publisher = (raw.get("publisher") or "").strip()
        link = raw.get("link")
        ts = raw.get("providerPublishTime")
        if isinstance(ts, (int, float)):
            try:
                published_raw = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
            except (ValueError, OSError, OverflowError):
                published_raw = None
        else:
            published_raw = ts
    if not title or not link:
        return None
    return {
        "title":     str(title).strip(),
        "link":      str(link),
        "publisher": publisher,
        "published": str(published_raw) if published_raw else "",
    }


def fetch_ticker_news(tickers: list[str], cache: pd.DataFrame,
                      ttl_days: int = TICKER_NEWS_TTL_DAYS,
                      top_n: int = TICKER_NEWS_TOP_N) -> tuple[pd.DataFrame, set[str]]:
    """Per-ticker yfinance news with the same parquet-cache + per-row
    ``fetched_at`` TTL pattern as the analyst cache. Returns (cache, failed)."""
    now = pd.Timestamp.now(tz="UTC")
    stale_cutoff = now - pd.Timedelta(days=ttl_days)
    to_fetch: list[str] = []
    for t in tickers:
        if t not in cache.index:
            to_fetch.append(t)
        elif pd.isna(cache.loc[t, "fetched_at"]) or cache.loc[t, "fetched_at"] < stale_cutoff:
            to_fetch.append(t)
    if not to_fetch:
        return cache, set()
    print(f"Fetching ticker news for {len(to_fetch)} ticker(s) "
          f"(parallel x4, TTL {ttl_days}d, top {top_n}/ticker)...", flush=True)

    def one(t: str):
        try:
            raw_items = yf.Ticker(t).news or []
            parsed: list[dict] = []
            # Parse a few extra in case some items lack title/link; stop at top_n.
            for it in raw_items[: top_n * 2]:
                p = _parse_yf_news_item(it)
                if p:
                    parsed.append(p)
                if len(parsed) >= top_n:
                    break
            return t, {
                "items_json": json.dumps(parsed, separators=(",", ":"), ensure_ascii=False),
                "fetched_at": now,
            }, None
        except Exception as e:
            print(f"  news fail {t}: {e}", file=sys.stderr)
            return t, {"items_json": "[]", "fetched_at": now}, str(e)

    rows = []
    failed: set[str] = set()
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(one, t): t for t in to_fetch}
        for i, fut in enumerate(as_completed(futures), 1):
            t, data, err = fut.result()
            rows.append({"ticker": t, **data})
            if err is not None:
                failed.add(t)
            if i % 30 == 0 or i == len(to_fetch):
                print(f"  news: {i}/{len(to_fetch)}", flush=True)

    new_df = pd.DataFrame(rows).set_index("ticker")
    combined = (pd.concat([cache.drop(index=[t for t in to_fetch if t in cache.index]), new_df])
                if not cache.empty else new_df)
    combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    TICKER_NEWS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    try:
        combined.to_parquet(TICKER_NEWS_CACHE)
        print(f"  cached ticker news to {TICKER_NEWS_CACHE}", flush=True)
    except Exception as e:
        print(f"WARN couldn't cache ticker news: {e}", file=sys.stderr)
    return combined, failed


def normalize_currency(raw_ccy: str) -> tuple[str, float]:
    """yfinance sometimes returns pence/cents (GBp, GBX). Map to the major currency
    and a divisor (100 for pence, 1 otherwise)."""
    raw_ccy = (raw_ccy or "USD").strip()
    if raw_ccy in PENCE_CODES:
        return PENCE_CODES[raw_ccy], 100.0
    return raw_ccy, 1.0


def download_fx(pairs: list[str]) -> pd.DataFrame:
    """Download daily close for FX pair tickers like 'USDGBP=X', 'EURGBP=X'."""
    if not pairs:
        return pd.DataFrame()
    end = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        data = yf.download(pairs, start=START_DATE, end=end,
                           auto_adjust=True, progress=False,
                           group_by="ticker", threads=True)
    except Exception as e:
        print(f"WARN FX download failed: {e}", file=sys.stderr)
        return pd.DataFrame()
    closes: dict[str, pd.Series] = {}
    for p in pairs:
        try:
            s = data[p]["Close"] if len(pairs) > 1 else data["Close"]
            if hasattr(s, "ndim") and s.ndim > 1:
                s = s.iloc[:, 0]
            if s.notna().any():
                closes[p] = s
            else:
                print(f"WARN no FX data for {p}", file=sys.stderr)
        except (KeyError, ValueError) as e:
            print(f"WARN FX miss for {p}: {e}", file=sys.stderr)
    df = pd.DataFrame(closes).sort_index()
    if not df.empty and df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    return df


FX_FILL_LIMIT_DAYS = 7   # M4: max leading gap (rows) to back-fill an FX rate across


def convert_to_base(prices: pd.DataFrame, meta: pd.DataFrame,
                    fx: pd.DataFrame, base: str = BASE_CCY) -> pd.DataFrame:
    """Convert native-currency prices to base currency using daily FX rates.
    Returns a new DataFrame with the same shape but base-currency values.

    FX edge-fill (M4): ffill carries a rate forward over interior/trailing gaps
    (non-trading days); a BOUNDED bfill covers only a short start-of-series
    calendar misalignment. An *unbounded* bfill would fabricate a flat rate across
    a long pre-series gap -> biased early baseline. But NaN-ing those dates would
    drop an early-bought non-base ticker (build_positions consumes base prices),
    so for a genuine long leading gap we fall back to the earliest known rate WITH
    a warning -- visible and bounded, never silent, never dropped. A missing pair
    is likewise left in native units (its % return stays correct) rather than
    NaN'd out of the basket."""
    out = prices.copy()
    for tkr in prices.columns:
        raw_ccy = str(meta.loc[tkr, "currency"]) if tkr in meta.index else "USD"
        ccy, divisor = normalize_currency(raw_ccy)
        if divisor != 1.0:
            out[tkr] = out[tkr] / divisor
        if ccy == base:
            continue
        fx_key = f"{ccy}{base}=X"
        if fx_key not in fx.columns:
            print(f"WARN no FX series {fx_key}, leaving {tkr} as {ccy}", file=sys.stderr)
            continue
        raw_rate = fx[fx_key].reindex(out.index)
        rate = raw_rate.ffill().bfill(limit=FX_FILL_LIMIT_DAYS)
        # Any rate still NaN where a price exists = a leading gap longer than the
        # bound. Don't drop the position: fall back to the earliest known rate and
        # warn so the partial coverage (approximate early baseline) is visible.
        gap = rate.isna() & out[tkr].notna()
        if gap.any():
            earliest = raw_rate.dropna()
            if not earliest.empty:
                print(f"WARN {fx_key}: {int(gap.sum())} date(s) for {tkr} before FX "
                      f"coverage; using earliest rate (early baseline approximate)",
                      file=sys.stderr)
                rate = rate.fillna(float(earliest.iloc[0]))
        out[tkr] = out[tkr] * rate
    return out


def ticker_currency(meta: pd.DataFrame, tkr: str) -> str:
    """Return the *normalized* currency (pence-codes mapped to major)."""
    raw = str(meta.loc[tkr, "currency"]) if tkr in meta.index else "USD"
    ccy, _ = normalize_currency(raw)
    return ccy


# --------------------------------------------------------------------------
# Returns / basket math
# --------------------------------------------------------------------------
def _baseline_price(series: pd.Series, baseline_date: pd.Timestamp) -> float | None:
    """Price on baseline_date, or nearest prior trading day."""
    s = series.dropna()
    sub = s.loc[:baseline_date]
    if sub.empty:
        return None
    return float(sub.iloc[-1])


def _synthesize_watchlist_transactions(watchlist: pd.DataFrame,
                                       prices: pd.DataFrame) -> pd.DataFrame:
    """Watchlist-only mode: treat each watched ticker as a single equal unit
    'held' from the start of its price window, so the normal positions pipeline
    renders per-ticker analytics and an equal-weight watchlist performance line —
    no real trades required."""
    if watchlist is None or watchlist.empty:
        return pd.DataFrame(columns=["ticker", "date", "action", "shares"])
    rows = []
    for tkr in watchlist.ticker.tolist():
        if tkr not in prices.columns:
            continue
        s = prices[tkr].dropna()
        if s.empty:
            continue
        rows.append({"ticker": tkr, "date": s.index[0], "action": "BUY", "shares": 1.0})
    return pd.DataFrame(rows, columns=["ticker", "date", "action", "shares"])


def _active_cycle_basis(txns: pd.DataFrame) -> "dict | None":
    """Average-cost basis for the ACTIVE trade cycle of one ticker — the current
    open lot, or the last closed cycle — splitting history at every FULL exit
    (running net units -> 0 after a SELL). SHARED by build_positions (table/modal)
    and compute_basket_mtm_series (hero chart) so both agree on a re-entered name's
    entry price + date (fixes the v2.9.4 table-vs-chart divergence). `txns` must be
    sorted by date and carry a numeric `price` column.

    Returns {avg_buy, avg_sell, first_buy_date, last_sell_date, closed}, or None
    when there are no usable buys.

    LIMITATION (forker note): cycle boundaries rely on SELL `shares` reflecting the
    real exit size so a full exit nets to 0. The author's snapshot guarantees this
    (full-exit SELLs carry the cycle's unit count). On a hand-rolled transactions.csv
    that logs 1 unit per row, a full exit recorded as a single SELL after several BUY
    rows won't net to 0, so the basis won't reset — log the exit with shares = units
    held, or use value mode with real quantities.
    """
    buys_all = txns[txns.action == "BUY"]
    if buys_all.empty:
        return None
    cycles: list[dict] = []
    cur_b: list[tuple] = []
    cur_s: list[tuple] = []
    net = 0.0
    for _r in txns.itertuples(index=False):
        _sh, _px = float(_r.shares), float(_r.price)
        if str(_r.action).upper() == "BUY":
            cur_b.append((_r.date, _sh, _px)); net += _sh
        else:
            cur_s.append((_r.date, _sh, _px)); net -= _sh
            if net <= 1e-6 and cur_b:            # position fully closed -> cycle boundary
                cycles.append({"buys": cur_b, "sells": cur_s})
                cur_b, cur_s, net = [], [], 0.0
    if cur_b or cur_s:
        cycles.append({"buys": cur_b, "sells": cur_s})
    active = cycles[-1] if cycles else {"buys": [], "sells": []}

    def _wavg(rws: list) -> float:
        tot = sum(sh for _, sh, _p in rws)
        return float(sum(sh * p for _, sh, p in rws) / tot) if tot > 0 else float("nan")

    raw_bought = float(buys_all.shares.sum())
    sells_all = txns[txns.action == "SELL"]
    raw_sold = float(sells_all.shares.sum())
    avg_buy = (_wavg(active["buys"]) if active["buys"]
               else float((buys_all.shares * buys_all.price).sum() / raw_bought))
    avg_sell = (_wavg(active["sells"]) if active["sells"]
                else (float((sells_all.shares * sells_all.price).sum() / raw_sold)
                      if raw_sold > 0 else float("nan")))
    first_buy_date = (pd.Timestamp(active["buys"][0][0]) if active["buys"]
                      else pd.Timestamp(buys_all.date.min()))
    last_sell_date = (pd.Timestamp(active["sells"][-1][0]) if active["sells"] else None)
    closed = str(txns.iloc[-1].action).upper() == "SELL"
    return {"avg_buy": avg_buy, "avg_sell": avg_sell,
            "first_buy_date": first_buy_date, "last_sell_date": last_sell_date,
            "closed": closed}


def build_positions(transactions: pd.DataFrame, prices: pd.DataFrame,
                    mode: str | None = None) -> pd.DataFrame:
    """Aggregate transactions into positions using average-cost accounting.

    Output column names mirror the old returns shape (baseline, baseline_date,
    latest, total_pct, ...) so the render layer keeps working; new columns
    (shares_held, status, total_invested, ...) extend the schema.

    ``mode`` (default = WEIGHT_MODE): "equal" collapses each position to one unit
    (privacy-driven, no monetary scale); "value" keeps real share quantities and
    capital-weights by shares x price.
    """
    if transactions.empty:
        return pd.DataFrame()
    mode = (mode or WEIGHT_MODE)
    latest_date = prices.index[-1]
    rows: list[dict] = []
    for tkr, txns in transactions.groupby("ticker"):
        if tkr not in prices.columns:
            print(f"WARN no price data for {tkr}, skipping", file=sys.stderr)
            continue
        ticker_series = prices[tkr].ffill().dropna()
        if ticker_series.empty:
            continue

        txns = txns.sort_values("date").copy()
        txns["price"] = _txn_prices(txns["date"], ticker_series)
        txns = txns.dropna(subset=["price"])
        if txns.empty:
            continue

        buys = txns[txns.action == "BUY"]
        sells = txns[txns.action == "SELL"]
        n_buys = int(len(buys))
        n_sells = int(len(sells))
        raw_bought = float(buys.shares.sum())    # raw row count (each row = 1 unit)
        raw_sold = float(sells.shares.sum())
        if raw_bought <= 0:
            continue

        # Cost basis from the ACTIVE trade cycle (current open lot or last closed
        # cycle), split at full exits — see _active_cycle_basis. Shared with the
        # hero-chart MTM so a re-entered name's entry can't disagree between the
        # table and the chart. Baseline/"held since" date = active cycle's first
        # buy, so the modal chart starts at the current lot.
        basis = _active_cycle_basis(txns)
        avg_buy_price = basis["avg_buy"]
        avg_sell_price = basis["avg_sell"]
        first_buy_date = basis["first_buy_date"]
        # Earliest buy across ALL cycles (independent of the active-cycle
        # baseline_date). The modal chart slices from here to draw the full
        # holding journey, while baseline/% stay anchored at the active cycle.
        first_acquired_date = pd.Timestamp(buys.date.min())
        last_action_date = pd.Timestamp(txns.date.max())
        latest = float(ticker_series.iloc[-1])

        # Transactional-recency rule: a position is "closed" when the most
        # recent trade row is a SELL, "open" otherwise. This is robust to
        # partial exits and to multiple open/close cycles on the same
        # ticker, where share-net math (e.g. shares_held > 0) would either
        # misclassify or require real per-row share quantities we don't have.
        last_action = str(txns.iloc[-1].action).upper()
        status = "closed" if last_action == "SELL" else "open"

        if mode == "value":
            # Capital weighting from real share quantities (shares x price). Only
            # meaningful when the source carries real sizes; on a 1-unit-per-row
            # source this degenerates to row-count x price (the KLAC artifact).
            total_bought = raw_bought
            total_sold = raw_sold
            shares_held = raw_bought - raw_sold
            total_invested = float((buys.shares * buys.price).sum())
            total_received = float((sells.shares * sells.price).sum()) if raw_sold > 0 else 0.0
            weight = total_invested if status == "open" else 0.0
        else:
            # v2.4 equal weight (default): collapse every position to ONE unit,
            # regardless of how many broker rows it carries. A stock split or
            # heavy scale-in (e.g. KLAC logged as 10 share-grants) must not
            # inflate this name's weight or, via cumulative-invested math, drag
            # the whole basket return down. Open = 1 unit held; closed = that
            # single unit sold. Raw counts (n_buys/n_sells) are display-only.
            total_bought = 1.0
            total_sold = 1.0 if status == "closed" else 0.0
            shares_held = 1.0 if status == "open" else 0.0
            total_invested = avg_buy_price            # per-unit cost basis (native ccy)
            total_received = avg_sell_price if status == "closed" else 0.0
            weight = 1.0 if status == "open" else 0.0

        realized_pnl = (avg_sell_price - avg_buy_price) * total_sold if total_sold > 0 else 0.0
        current_value = shares_held * latest
        current_cost = shares_held * avg_buy_price
        unrealized_pnl = current_value - current_cost

        if status == "open":
            total_pct = (latest / avg_buy_price - 1) * 100
        else:
            total_pct = (avg_sell_price / avg_buy_price - 1) * 100 if avg_buy_price > 0 else 0.0

        # Post-exit move (only meaningful for closed positions): how has the
        # stock moved since you sold? Positive = you missed gains (regret).
        # Negative = it tanked after you sold (a lucky escape).
        if status == "closed" and avg_sell_price > 0:
            post_exit_pct = (latest / avg_sell_price - 1) * 100
        else:
            post_exit_pct = float("nan")

        def pct_back(days: int) -> float:
            if status != "open":
                return float("nan")
            cutoff = latest_date - pd.Timedelta(days=days)
            sub = ticker_series.loc[:cutoff]
            if sub.empty:
                return float("nan")
            return (latest / float(sub.iloc[-1]) - 1) * 100

        ytd_cutoff = pd.Timestamp(f"{latest_date.year}-01-01")
        ytd_sub = ticker_series.loc[:ytd_cutoff]
        # Fall back to NaN (not avg_buy_price) when no prior-year data exists, so the
        # YTD column renders as `—` instead of a silently-wrong "return since buy".
        # Triggers only for tickers with <1y of price history (e.g. recent IPOs).
        ytd_ref = float(ytd_sub.iloc[-1]) if not ytd_sub.empty else float("nan")
        ytd_pct = (latest / ytd_ref - 1) * 100 if status == "open" and pd.notna(ytd_ref) and ytd_ref > 0 else float("nan")

        txn_list = [
            {
                "date": pd.Timestamp(r.date).strftime("%Y-%m-%d"),
                "action": str(r.action),
                "shares": float(r.shares),
                "price": float(r.price),
            }
            for r in txns.itertuples(index=False)
        ]

        rows.append({
            "ticker": tkr,
            "baseline": avg_buy_price,
            "baseline_date": first_buy_date,
            "latest": latest,
            "total_pct": total_pct,
            "1w_pct": pct_back(7),
            "1m_pct": pct_back(30),
            "3m_pct": pct_back(90),
            "ytd_pct": ytd_pct,
            "weight": weight,
            "shares_held": shares_held,
            "total_bought": total_bought,
            "total_sold": total_sold,
            "n_buys": n_buys,
            "n_sells": n_sells,
            "total_invested": total_invested,
            "total_received": total_received,
            "avg_buy_price": avg_buy_price,
            "avg_sell_price": avg_sell_price,
            "current_value": current_value,
            "current_cost": current_cost,
            "unrealized_pnl": unrealized_pnl,
            "realized_pnl": realized_pnl,
            "post_exit_pct": post_exit_pct,
            "status": status,
            "first_buy_date": first_buy_date,
            "first_acquired_date": first_acquired_date,
            "last_action_date": last_action_date,
            "n_transactions": len(txns),
            "transactions": txn_list,
        })
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).set_index("ticker")


def _basket_mtm_capital_weighted(transactions: pd.DataFrame, p: pd.DataFrame,
                                 dates: pd.DatetimeIndex) -> pd.Series:
    """value-mode basket: % return on cumulative capital deployed, marking held
    shares to market and counting realized sell cash so closing a winner doesn't
    drop the line. Needs real share quantities to be meaningful."""
    cum_invested = pd.Series(0.0, index=dates)
    cum_value = pd.Series(0.0, index=dates)
    for tkr, txns in transactions.groupby("ticker"):
        if tkr not in p.columns:
            continue
        s = p[tkr]
        txns = txns.sort_values("date").copy()
        txns["price"] = _txn_prices(txns["date"], s)
        txns = txns.dropna(subset=["price"])
        snapped = []
        for _, row in txns.iterrows():
            snap = _snap_to_trading(pd.Timestamp(row["date"]), dates)
            if snap is None:
                continue
            snapped.append({"date": snap, "action": row["action"],
                            "shares": row["shares"], "price": row["price"]})
        if not snapped:
            continue
        snap_df = pd.DataFrame(snapped)
        sign = snap_df.action.map({"BUY": 1, "SELL": -1})
        active_shares = (sign * snap_df.shares).groupby(snap_df.date).sum() \
            .reindex(dates).fillna(0).cumsum()
        buys = snap_df[snap_df.action == "BUY"]
        cum_buy = ((buys.shares * buys.price).groupby(buys.date).sum()
                   .reindex(dates).fillna(0).cumsum()
                   if not buys.empty else pd.Series(0.0, index=dates))
        sells = snap_df[snap_df.action == "SELL"]
        cum_sell = ((sells.shares * sells.price).groupby(sells.date).sum()
                    .reindex(dates).fillna(0).cumsum()
                    if not sells.empty else pd.Series(0.0, index=dates))
        cum_invested = cum_invested.add(cum_buy, fill_value=0)
        cum_value = cum_value.add((active_shares * s).fillna(0) + cum_sell, fill_value=0)
    return ((cum_value / cum_invested.replace(0, np.nan)) - 1).fillna(0) * 100


def compute_basket_mtm_series(transactions: pd.DataFrame, prices: pd.DataFrame,
                              mode: str | None = None) -> pd.Series:
    """Daily basket return (%).

    Default (``mode="equal"``, v2.4): every position counts once, no matter how
    many transaction rows or how much capital it carries. Each ticker's return
    series is rebased to its average buy price and frozen at the realized return
    once closed (so closing a winner doesn't drop the line); the basket at date t
    is the mean across positions active by t — a "how have my picks done, on
    average" line no split / scale-in can distort.

    ``mode="value"`` instead capital-weights by real share quantities (see
    :func:`_basket_mtm_capital_weighted`).
    """
    if transactions.empty:
        return pd.Series(dtype=float)
    mode = (mode or WEIGHT_MODE)
    p = prices.ffill()
    dates = p.index
    if mode == "value":
        return _basket_mtm_capital_weighted(transactions, p, dates)
    contribs: list[pd.Series] = []   # per-position return %, NaN before entry

    for tkr, txns in transactions.groupby("ticker"):
        if tkr not in p.columns:
            continue
        s = p[tkr]
        ser = s.dropna()
        if ser.empty:
            continue
        txns = txns.sort_values("date").copy()
        txns["price"] = _txn_prices(txns["date"], ser)
        txns = txns.dropna(subset=["price"])
        # v2.9.4: use the SAME active-cycle basis as build_positions so a re-entered
        # name's entry price + date match the holdings table + modal exactly. (This
        # previously used an all-time unweighted mean of every buy + the all-time
        # first-buy date -> the chart and table disagreed for rebought names.)
        basis = _active_cycle_basis(txns)
        if basis is None or not (basis["avg_buy"] > 0):
            continue
        avg_buy = basis["avg_buy"]
        entry = _snap_to_trading(basis["first_buy_date"], dates)
        if entry is None:
            continue

        # Mark-to-market return relative to this position's average buy price.
        r = (s / avg_buy - 1.0) * 100.0
        if basis["closed"] and basis["last_sell_date"] is not None:
            exit_date = _snap_to_trading(basis["last_sell_date"], dates)
            if exit_date is not None:
                realized = (basis["avg_sell"] / avg_buy - 1.0) * 100.0
                r.loc[r.index >= exit_date] = realized   # freeze at realized return
        r.loc[r.index < entry] = np.nan                  # not in the basket before entry
        contribs.append(r)

    if not contribs:
        return pd.Series(0.0, index=dates)
    # Equal-weight: mean across the positions that are active on each date.
    basket = pd.concat(contribs, axis=1).mean(axis=1, skipna=True)
    return basket.fillna(0.0)


def compute_benchmark_series(bench: pd.Series, start_date: pd.Timestamp) -> pd.Series:
    if bench.empty:
        return pd.Series(dtype=float)
    s = bench.ffill()
    sub = s.loc[:start_date]
    base = float(sub.iloc[-1]) if not sub.empty else float(s.iloc[0])
    rebased = (s / base - 1) * 100
    return rebased.loc[start_date:]


def compute_rolling_alpha(basket: pd.Series, bench: pd.Series,
                          window_days: int = 30) -> pd.Series:
    """Trailing-window excess return of basket over benchmark, in percentage
    points. Both inputs are cumulative-% series indexed by date. For each
    date t, returns (basket[t] - basket[t - W]) - (bench[t] - bench[t - W]),
    aligned via outer-merge then forward-filled so the two series share a
    common daily index even when one is missing trading days the other has."""
    if basket.empty or bench.empty:
        return pd.Series(dtype=float)
    df = pd.concat([basket.rename("b"), bench.rename("s")], axis=1).sort_index()
    df = df.ffill().dropna()
    if len(df) < window_days + 1:
        return pd.Series(dtype=float)
    b_window = df["b"] - df["b"].shift(window_days)
    s_window = df["s"] - df["s"].shift(window_days)
    return (b_window - s_window).dropna()


def compute_drawdown_series(basket: pd.Series) -> pd.Series:
    """v1.8 T5: peak-to-trough drawdown at each date, in percent (<= 0).

    Converts the cumulative-% basket series into a growth multiplier, tracks the
    running peak, and returns (mult / running_peak - 1) * 100. A value of -5.2
    means "at this date, the basket was 5.2% below its prior peak". Series is
    indexed by the same dates as the input basket.
    """
    if basket.empty:
        return pd.Series(dtype=float)
    mult = 1 + basket / 100.0
    running_peak = mult.cummax()
    return (mult / running_peak - 1) * 100


def _date_fraction(d, start, end) -> float:
    """v2.7 #6: fraction in [0,1] of date `d` across the [start, end] domain,
    clamped. Returns 0.0 for a zero/negative span. Used so the alpha + drawdown
    sparklines map x by *calendar date* over the main chart's full date span
    (basket start -> today) — a date then lands at the same x in all three,
    instead of each series stretching its own length across the full width."""
    span = end - start
    span_s = span.total_seconds() if hasattr(span, "total_seconds") else float(span)
    if span_s <= 0:
        return 0.0
    cur = d - start
    cur_s = cur.total_seconds() if hasattr(cur, "total_seconds") else float(cur)
    f = cur_s / span_s
    return 0.0 if f < 0 else (1.0 if f > 1 else f)


def compute_signals(prices: pd.DataFrame) -> pd.DataFrame:
    """Compute a compact technical-analysis signal per ticker.

    Uses 12M high/low proximity, 50/200-day moving-average position, and
    1M/3M momentum to classify each name into a single label + detail line.
    """
    rows = []
    for tkr in prices.columns:
        s = prices[tkr].ffill().dropna()
        if s.empty or len(s) < 30:
            rows.append({"ticker": tkr, "signal": "—", "tone": "neutral",
                         "detail": "", "m12_high": float("nan"), "m12_low": float("nan")})
            continue
        latest = float(s.iloc[-1])
        m12 = s.tail(252)  # ~12 months trading days
        m12_high = float(m12.max())
        m12_low = float(m12.min())
        pct_from_high = (latest / m12_high - 1) * 100  # <= 0
        pct_from_low = (latest / m12_low - 1) * 100    # >= 0
        ma50 = float(s.tail(50).mean()) if len(s) >= 50 else latest
        ma200 = float(s.tail(200).mean()) if len(s) >= 200 else latest
        s3m = s.tail(63)
        s1m = s.tail(21)
        mom_3m = (latest / float(s3m.iloc[0]) - 1) * 100 if len(s3m) >= 2 else 0.0
        mom_1m = (latest / float(s1m.iloc[0]) - 1) * 100 if len(s1m) >= 2 else 0.0

        # Priority cascade — most informative first
        if pct_from_high >= -2.5:
            signal, tone = "Near 12M high", "pos"
            detail = f"{pct_from_high:.0f}% off high"
        elif pct_from_low <= 2.5:
            signal, tone = "Near 12M low", "neg"
            detail = f"+{pct_from_low:.0f}% off low"
        elif latest > ma50 > ma200 and mom_3m > 8:
            signal, tone = "Strong uptrend", "pos"
            detail = f"3M {mom_3m:+.0f}% · above 50/200 DMA"
        elif latest < ma50 < ma200 and mom_3m < -8:
            signal, tone = "Strong downtrend", "neg"
            detail = f"3M {mom_3m:+.0f}% · below 50/200 DMA"
        elif mom_1m > 6 and mom_3m < 0:
            signal, tone = "Bouncing", "pos"
            detail = f"1M {mom_1m:+.0f}% · 3M {mom_3m:+.0f}%"
        elif mom_1m < -6 and mom_3m > 0:
            signal, tone = "Pullback", "neg"
            detail = f"1M {mom_1m:+.0f}% · 3M {mom_3m:+.0f}%"
        elif latest > ma200 and mom_3m > 0:
            signal, tone = "Trending up", "pos"
            detail = f"3M {mom_3m:+.0f}% · above 200 DMA"
        elif latest < ma200 and mom_3m < 0:
            signal, tone = "Trending down", "neg"
            detail = f"3M {mom_3m:+.0f}% · below 200 DMA"
        elif abs(mom_3m) < 4:
            signal, tone = "Consolidating", "neutral"
            detail = f"3M {mom_3m:+.0f}% · range-bound"
        else:
            signal, tone = "Mixed", "neutral"
            detail = f"3M {mom_3m:+.0f}% · 1M {mom_1m:+.0f}%"

        rows.append({"ticker": tkr, "signal": signal, "tone": tone,
                     "detail": detail, "m12_high": m12_high, "m12_low": m12_low})
    return pd.DataFrame(rows).set_index("ticker")


# --------------------------------------------------------------------------
# Quant signals (modal "Quant signals" sub-row)
# --------------------------------------------------------------------------
# Surface five per-stock metrics derived from full OHLCV history:
#   - sma200_dist_pct : distance from 200-day SMA (long-term trend health)
#   - atr14_*         : 14-day Wilder ATR (stop-loss sizing)
#   - rsi14           : 14-day Wilder RSI (overbought / oversold)
#   - range52w_pct    : where today's price sits in the 52-week range
#   - vol_ratio       : today's volume vs 63-day average (move confirmation)
# All NaN-safe — partial history (e.g. brand-new IPOs without 200 days) yields
# NaN and renders as a dim em-dash in the modal.


def _wilder_atr_last(high: pd.Series, low: pd.Series,
                     close: pd.Series, period: int = 14) -> float:
    """Last value of the 14-day Wilder ATR. NaN if insufficient history."""
    df = pd.concat({"h": high, "l": low, "c": close}, axis=1).dropna()
    if len(df) < period + 1:
        return float("nan")
    prev_close = df["c"].shift(1)
    tr = pd.concat([
        (df["h"] - df["l"]).abs(),
        (df["h"] - prev_close).abs(),
        (df["l"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    # Wilder smoothing == EMA with alpha = 1/period.
    atr = tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    return float(atr.iloc[-1]) if not atr.empty else float("nan")


def _wilder_rsi_last(close: pd.Series, period: int = 14) -> float:
    """Last value of the 14-day Wilder RSI (0-100). NaN if insufficient history."""
    c = close.dropna()
    if len(c) < period + 1:
        return float("nan")
    delta = c.diff()
    gains = delta.clip(lower=0)
    losses = (-delta).clip(lower=0)
    avg_gain = gains.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = losses.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    rsi = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1]) if not rsi.empty and pd.notna(rsi.iloc[-1]) else float("nan")


def compute_quant_metrics(ohlcv_native: pd.DataFrame, fx: pd.DataFrame,
                          meta: pd.DataFrame) -> pd.DataFrame:
    """Per-ticker quant metrics derived from native-currency OHLCV.

    Returns a DataFrame indexed by ticker with columns:
        sma200_dist_pct, atr14_native, atr14_gbp, atr14_pct,
        rsi14, range52w_pct, vol_ratio
    NaN where the underlying window can't be filled (e.g. <200 days history,
    or no FX rate available to convert ATR into base currency).
    """
    if ohlcv_native is None or ohlcv_native.empty:
        return pd.DataFrame()

    # MultiIndex columns: gather every unique ticker.
    tickers = sorted({t for (t, _) in ohlcv_native.columns})

    # Latest FX rate per currency-pair series, e.g. 'USDGBP=X' -> 0.78.
    fx_last: dict[str, float] = {}
    if fx is not None and not fx.empty:
        for col in fx.columns:
            s = fx[col].dropna()
            if not s.empty:
                fx_last[col] = float(s.iloc[-1])

    rows = []
    for t in tickers:
        try:
            close = ohlcv_native[(t, "Close")].dropna()
            high = ohlcv_native[(t, "High")]
            low = ohlcv_native[(t, "Low")]
            volume = ohlcv_native[(t, "Volume")].dropna()
        except KeyError:
            continue
        if close.empty:
            continue
        last_close = float(close.iloc[-1])

        # --- 200-day SMA distance ---------------------------------------
        sma200_dist = float("nan")
        if len(close) >= 200 and last_close == last_close:
            sma200 = float(close.tail(200).mean())
            if sma200 > 0:
                sma200_dist = (last_close / sma200 - 1) * 100

        # --- 52-week range position -------------------------------------
        range52w = float("nan")
        if len(close) >= 60:  # at least a few months of data
            window = close.tail(252)
            hi = float(window.max())
            lo = float(window.min())
            if hi > lo:
                range52w = (last_close - lo) / (hi - lo) * 100

        # --- ATR (Wilder, 14d) ------------------------------------------
        atr_native = _wilder_atr_last(high, low, close, period=14)
        atr_pct = float("nan")
        atr_gbp = float("nan")
        if atr_native == atr_native and last_close > 0:
            atr_pct = (atr_native / last_close) * 100
            raw_ccy = str(meta.loc[t, "currency"]) if t in meta.index else "USD"
            ccy, divisor = normalize_currency(raw_ccy)
            atr_in_major = atr_native / divisor if divisor else atr_native
            if ccy == BASE_CCY:
                atr_gbp = atr_in_major
            else:
                fx_key = f"{ccy}{BASE_CCY}=X"
                if fx_key in fx_last:
                    atr_gbp = atr_in_major * fx_last[fx_key]

        # --- RSI (Wilder, 14d) ------------------------------------------
        rsi14 = _wilder_rsi_last(close, period=14)

        # --- Volume ratio (today vs 63-day mean) ------------------------
        vol_ratio = float("nan")
        if len(volume) >= 5:
            last_vol = float(volume.iloc[-1])
            avg_vol = float(volume.tail(63).mean())
            if avg_vol > 0:
                vol_ratio = last_vol / avg_vol

        rows.append({
            "ticker": t,
            "sma200_dist_pct": sma200_dist,
            "atr14_native": atr_native,
            "atr14_gbp": atr_gbp,
            "atr14_pct": atr_pct,
            "rsi14": rsi14,
            "range52w_pct": range52w,
            "vol_ratio": vol_ratio,
        })

    return pd.DataFrame(rows).set_index("ticker") if rows else pd.DataFrame()


def compute_contributors(returns_df: pd.DataFrame) -> pd.DataFrame:
    """Contribution in percentage points to the equal-weight basket return."""
    in_basket = returns_df[returns_df.weight > 0].copy()
    total_w = in_basket.weight.sum()
    if total_w == 0:
        in_basket["contribution_pp"] = 0.0
    else:
        in_basket["contribution_pp"] = (in_basket.weight * in_basket.total_pct) / total_w
    return in_basket.sort_values("contribution_pp", ascending=False)


# --------------------------------------------------------------------------
# Basket diversification (pairwise correlations of open positions)
# --------------------------------------------------------------------------
# A portfolio-level lens nothing else in the dashboard covers: are the open
# positions independent bets or expressions of the same trade? Lower average
# pairwise correlation = more diversified; higher = redundant exposure.
#
# Correlations are computed on **native-currency daily returns** (FX would
# add a spurious common factor across all non-GBP names). 6-month lookback
# is the equity-research default — long enough to be stable, short enough
# to reflect the current regime.

_DIV_HIST_EDGES = [-1.0, -0.75, -0.5, -0.25, 0.0, 0.25, 0.5, 0.75, 1.0]


def compute_basket_correlation(returns: pd.DataFrame, prices_native: pd.DataFrame,
                               lookback_days: int = 126,
                               min_periods: int = 30) -> dict | None:
    """Pairwise correlation summary across currently-open positions.

    Returns ``None`` when fewer than two open positions have usable history.
    Otherwise a dict containing:
        ``n_positions``, ``n_pairs``, ``lookback_days``,
        ``avg_corr`` (mean of upper-triangle pairwise correlations),
        ``most_correlated``  (list of {a, b, corr}: top 3 pairs by correlation),
        ``best_diversifiers`` (list of {ticker, avg_corr}: lowest mean
                               correlation with every other open position),
        ``histogram`` (list of {min, max, count} buckets).
    """
    if returns.empty or prices_native.empty:
        return None
    open_tickers = sorted(returns[returns.status == "open"].index.tolist())
    available = [t for t in open_tickers if t in prices_native.columns]
    if len(available) < 2:
        return None
    sub = prices_native[available].tail(lookback_days)
    daily_ret = sub.pct_change().dropna(how="all")
    if daily_ret.empty:
        return None
    # Pandas .corr() does pairwise NaN handling for free; min_periods guards
    # against tiny-overlap noise (e.g. a position only ~10 days old).
    corr = daily_ret.corr(min_periods=min_periods)

    # Upper triangle: every unique unordered pair, no self-correlations.
    pairs: list[tuple[str, str, float]] = []
    cols = corr.columns.tolist()
    for i in range(len(cols)):
        for j in range(i + 1, len(cols)):
            v = corr.iat[i, j]
            if pd.notna(v):
                pairs.append((cols[i], cols[j], float(v)))
    if not pairs:
        return None

    avg_corr = sum(p[2] for p in pairs) / len(pairs)
    most_correlated = sorted(pairs, key=lambda p: p[2], reverse=True)[:3]

    # Best diversifiers: per-ticker mean of its (non-self) correlations.
    div_scores: list[tuple[str, float]] = []
    for t in corr.index:
        row = corr.loc[t].drop(t).dropna()
        if not row.empty:
            div_scores.append((t, float(row.mean())))
    best_diversifiers = sorted(div_scores, key=lambda x: x[1])[:3]

    # Histogram (8 buckets of width 0.25, spanning [-1, 1]).
    vals = np.array([p[2] for p in pairs], dtype=float)
    counts, _ = np.histogram(vals, bins=_DIV_HIST_EDGES)
    histogram = [
        {"min": _DIV_HIST_EDGES[i], "max": _DIV_HIST_EDGES[i + 1], "count": int(counts[i])}
        for i in range(len(counts))
    ]
    return {
        "n_positions": len(available),
        "n_pairs": len(pairs),
        "lookback_days": lookback_days,
        "avg_corr": float(avg_corr),
        "most_correlated": [{"a": a, "b": b, "corr": c} for (a, b, c) in most_correlated],
        "best_diversifiers": [{"ticker": t, "avg_corr": v} for (t, v) in best_diversifiers],
        "histogram": histogram,
        # T14: full pair list so JS can filter by correlation bucket when the
        # user clicks a histogram bar. Sorted by abs(corr) desc -- most
        # diagnostic pairs first within each bucket.
        "all_pairs": sorted(
            [{"a": a, "b": b, "corr": c} for (a, b, c) in pairs],
            key=lambda p: abs(p["corr"]),
            reverse=True,
        ),
    }


# --------------------------------------------------------------------------
# SVG sparkline helper
# --------------------------------------------------------------------------
def sparkline_svg(values: list[float], width: int, height: int,
                  *, color_class: str, gradient_id: str | None = None,
                  show_zero: bool = False) -> str:
    if not values or len(values) < 2:
        return f'<svg viewBox="0 0 {width} {height}" class="sparkline {color_class}"></svg>'
    n = len(values)
    vmin, vmax = min(values), max(values)
    span = vmax - vmin or 1
    pad = 2.0
    pts = []
    for i, v in enumerate(values):
        x = (i / (n - 1)) * (width - 2 * pad) + pad
        y = height - pad - ((v - vmin) / span) * (height - 2 * pad)
        pts.append((x, y))
    pl = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)

    fill_layer = ""
    if gradient_id:
        area_d = (
            f"M {pts[0][0]:.1f},{height - pad:.1f} "
            + " ".join(f"L {x:.1f},{y:.1f}" for x, y in pts)
            + f" L {pts[-1][0]:.1f},{height - pad:.1f} Z"
        )
        fill_layer = f'<path d="{area_d}" fill="url(#{gradient_id})"/>'

    zero_layer = ""
    if show_zero and vmin <= 0 <= vmax:
        zero_y = height - pad - ((0 - vmin) / span) * (height - 2 * pad)
        zero_layer = (
            f'<line x1="{pad:.1f}" y1="{zero_y:.1f}" x2="{width - pad:.1f}" y2="{zero_y:.1f}" '
            f'stroke="rgba(255,255,255,0.12)" stroke-width="0.5" stroke-dasharray="2 3"/>'
        )

    return (
        f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" class="sparkline {color_class}">'
        f'{fill_layer}{zero_layer}'
        f'<polyline points="{pl}" fill="none" stroke="currentColor" stroke-width="1.5" '
        f'stroke-linejoin="round" stroke-linecap="round"/>'
        f'</svg>'
    )


# --------------------------------------------------------------------------
# Render fragments
# --------------------------------------------------------------------------
def _cls(v: float) -> str:
    return "pos" if (v is not None and v >= 0) else "neg"


def _pct_cell(v: float, dim_mobile: bool = False) -> str:
    """Format a percentage cell with NaN safety (NaN -> em-dash)."""
    cls_mobile = " dim-mobile" if dim_mobile else ""
    if v is None or (isinstance(v, float) and v != v):  # NaN check
        return f'<td class="num dim{cls_mobile}" data-v="0">&mdash;</td>'
    return f'<td class="num {_cls(v)}{cls_mobile}" data-v="{v:.4f}">{v:+.2f}%</td>'


def _esc(s: str) -> str:
    # Escapes &, <, > AND quotes — so the result is safe in BOTH text and
    # attribute context (title="...", href="..."). & must be replaced first.
    return ((str(s) if s is not None else "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


def safe_url(u) -> str:
    """HTML-attribute-safe href, but ONLY for http(s) URLs — anything else
    (javascript:, data:, relative junk) collapses to '#'. News/cite links come
    from untrusted RSS feeds + the Cloudflare Worker, where entity-escaping alone
    leaves a `javascript:` scheme clickable (it has no <>&" to escape)."""
    s = (str(u) if u is not None else "").strip()
    low = s.lower()
    if low.startswith("http://") or low.startswith("https://"):
        return _esc(s)
    return "#"


def _json_for_script(obj, **kw) -> str:
    """json.dumps hardened for embedding INSIDE a <script> tag: escapes '</' so a
    string value containing the literal '</script>' can't terminate the element
    early (standard JSON-in-HTML hardening that json.dumps does NOT do). Use for
    every inline payload; the fetched sidecar payload.json doesn't need it."""
    return json.dumps(obj, **kw).replace("</", "<\\/")


def render_svg_defs() -> str:
    return """<svg width="0" height="0" style="position:absolute" aria-hidden="true">
  <defs>
    <linearGradient id="grad-up" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#34d399" stop-opacity="0.38"/>
      <stop offset="100%" stop-color="#34d399" stop-opacity="0.02"/>
    </linearGradient>
    <linearGradient id="grad-down" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#f87171" stop-opacity="0.38"/>
      <stop offset="100%" stop-color="#f87171" stop-opacity="0.02"/>
    </linearGradient>
    <linearGradient id="grad-up-lg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#34d399" stop-opacity="0.32"/>
      <stop offset="100%" stop-color="#34d399" stop-opacity="0.01"/>
    </linearGradient>
    <linearGradient id="grad-down-lg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#f87171" stop-opacity="0.32"/>
      <stop offset="100%" stop-color="#f87171" stop-opacity="0.01"/>
    </linearGradient>
    <linearGradient id="grad-amber-lg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#f59e0b" stop-opacity="0.32"/>
      <stop offset="100%" stop-color="#f59e0b" stop-opacity="0.01"/>
    </linearGradient>
  </defs>
</svg>"""


def _industry_label(meta: pd.DataFrame, tkr: str) -> str:
    if tkr not in meta.index:
        return ""
    industry = str(meta.loc[tkr, "industry"] or "")
    if not industry:
        industry = str(meta.loc[tkr, "sector"] or "")
    return industry


def render_table(returns: pd.DataFrame, weekly: pd.DataFrame, meta: pd.DataFrame,
                 signals: pd.DataFrame, analyst: pd.DataFrame | None = None) -> str:
    rows = []
    for tkr, r in returns.iterrows():
        if tkr in weekly.columns:
            s = weekly[tkr].dropna()
            rebased = ((s / s.iloc[0] - 1) * 100).tolist() if not s.empty else []
        else:
            rebased = []
        color_class = "up" if r.total_pct >= 0 else "down"
        spark = sparkline_svg(rebased, 90, 28, color_class=color_class, gradient_id=f"grad-{color_class}")
        industry = _esc(_industry_label(meta, tkr))
        ccy = ticker_currency(meta, tkr)
        ccy_badge = (
            f'<span class="badge-ccy" title="Listed in {ccy}">{ccy}</span>'
            if ccy != BASE_CCY else ""
        )
        weight_badge = (
            f'<span class="badge-weight" title="1 equal-weight unit '
            f'&middot; {r.n_buys} buys, {r.n_sells} sells">1 u</span>'
            if r.status == "open" else
            '<span class="badge-closed" title="Position closed">CLOSED</span>'
        )
        # Purchase columns — show actual avg buy price + first buy date for
        # every position (open and closed alike — closed positions DID buy
        # at some price, even if they no longer hold shares).
        if pd.notna(r.baseline) and r.baseline > 0:
            cost_cell = (f'<td class="num dim-mobile" data-v="{r.baseline:.4f}">'
                         f'{BASE_SYMBOL}{r.baseline:,.2f}</td>')
            date_cell = (f'<td class="num purchased dim-mobile" data-v="{r.baseline_date.value}">'
                         f'{r.baseline_date.strftime("%d %b %y")}</td>')
        else:
            cost_cell = '<td class="num dim dim-mobile" data-v="0">&mdash;</td>'
            date_cell = '<td class="num dim purchased dim-mobile" data-v="0">&mdash;</td>'

        # Signal cell
        if tkr in signals.index:
            sig = signals.loc[tkr]
            sig_cell = (f'<td class="t-signal sig-{sig.tone}" data-v="{_esc(sig.signal)}">'
                        f'<div class="sig-main">{_esc(sig.signal)}</div>'
                        f'<div class="sig-detail">{_esc(sig.detail)}</div>'
                        f'</td>')
        else:
            sig_cell = '<td class="t-signal dim" data-v="">&mdash;</td>'

        # Post-exit cell: only populated for closed positions. Shows how the
        # stock moved since you sold (regret if up, lucky escape if down).
        post_exit_val = r.get("post_exit_pct", float("nan")) if hasattr(r, "get") else \
                        (r["post_exit_pct"] if "post_exit_pct" in r.index else float("nan"))
        if r.status == "closed" and pd.notna(post_exit_val):
            cls = "pos" if post_exit_val >= 0 else "neg"
            post_exit_cell = (
                f'<td class="num post-exit {cls} dim-mobile" data-v="{post_exit_val:.4f}">'
                f'{post_exit_val:+.2f}%</td>'
            )
        else:
            post_exit_cell = '<td class="num post-exit dim dim-mobile" data-v="0">&mdash;</td>'

        # Analyst cells — target, upside %, recommendation + analyst count.
        # All three are em-dashed when the ticker has no analyst coverage
        # (small caps, recent IPOs, some non-US tickers).
        a_target_cell = '<td class="num t-target dim dim-mobile" data-v="0">&mdash;</td>'
        a_upside_cell = '<td class="num t-upside dim dim-mobile" data-v="0">&mdash;</td>'
        a_rec_cell    = '<td class="t-rec dim" data-v="">&mdash;</td>'
        if analyst is not None and not analyst.empty and tkr in analyst.index:
            a = analyst.loc[tkr]
            target_native = a.get("target_mean")
            cur_native = a.get("current_price")
            if (target_native is not None and pd.notna(target_native) and target_native > 0
                and cur_native is not None and pd.notna(cur_native) and cur_native > 0):
                # Apply the same pence-to-major divisor used elsewhere so the
                # display matches the rest of the row (also native ccy).
                raw_ccy = str(meta.loc[tkr, "currency"]) if tkr in meta.index else "USD"
                _, divisor = normalize_currency(raw_ccy)
                t_major = float(target_native) / divisor
                upside = (float(target_native) / float(cur_native) - 1) * 100
                ccy_sym = CCY_SYMBOLS.get(ticker_currency(meta, tkr), "")
                a_target_cell = (f'<td class="num t-target dim-mobile" data-v="{t_major:.4f}">'
                                 f'{ccy_sym}{t_major:,.2f}</td>')
                cls = "pos" if upside >= 0 else "neg"
                a_upside_cell = (f'<td class="num t-upside {cls} dim-mobile" data-v="{upside:.4f}">'
                                 f'{upside:+.1f}%</td>')
            rec_raw = str(a.get("recommendation") or "")
            n_an = int(a["num_analysts"]) if pd.notna(a.get("num_analysts")) else 0
            if rec_raw and n_an > 0:
                rec_label, rec_cls = _REC_LABELS.get(rec_raw, ("—", "an-rec-none"))
                a_rec_cell = (f'<td class="t-rec" data-v="{_esc(rec_raw)}">'
                              f'<span class="an-rec {rec_cls}">{rec_label}</span>'
                              f'<div class="t-rec-count">{n_an}</div></td>')

        sector = str(meta.loc[tkr, "sector"]).strip() if tkr in meta.index else ""
        rows.append(
            f'<tr data-ticker="{_esc(tkr)}" data-total="{r.total_pct:.4f}" data-weight="{r.weight:.4f}"'
            f' data-sector="{_esc(sector)}">'
            f'<td class="t-ticker">'
            f'<div class="tkr-main">{_esc(tkr)}{ccy_badge}{weight_badge}</div>'
            f'<div class="tkr-sub">{industry}</div>'
            f'</td>'
            f'{a_target_cell}'
            f'{a_upside_cell}'
            f'{a_rec_cell}'
            f'{sig_cell}'
            f'<td class="t-spark">{spark}</td>'
            f'<td class="num" data-v="{r.latest:.4f}">{BASE_SYMBOL}{r.latest:,.2f}</td>'
            f'{cost_cell}'
            f'{date_cell}'
            f'{_pct_cell(r.total_pct)}'
            f'{_pct_cell(r["1w_pct"], dim_mobile=True)}'
            f'{_pct_cell(r["1m_pct"], dim_mobile=True)}'
            f'{_pct_cell(r["3m_pct"], dim_mobile=True)}'
            f'{_pct_cell(r.ytd_pct)}'
            f'{post_exit_cell}'
            "</tr>"
        )
    body = "\n".join(rows)
    return f"""<div class="table-scroll"><table id="ret-table">
  <thead>
    <tr>
      <th data-col="0" data-num="0">Ticker</th>
      <th data-col="1" data-num="1" class="num dim-mobile" title="Wall Street mean target price (native currency)">Target</th>
      <th data-col="2" data-num="1" class="num dim-mobile" title="Implied upside from current price to mean target">Upside</th>
      <th data-col="3" data-num="0" title="Analyst recommendation / number of analysts covering">Analyst</th>
      <th data-col="4" data-num="0">Signal</th>
      <th>Trend</th>
      <th data-col="6" data-num="1" class="num">Last</th>
      <th data-col="7" data-num="1" class="num dim-mobile" title="Average buy price, estimated from the trade-date closing price (a proxy &mdash; not your actual fill). Per-name returns inherit this small gap.">Cost</th>
      <th data-col="8" data-num="1" class="num dim-mobile">Purchased</th>
      <th data-col="9" data-num="1" class="num" title="Return since the (trade-date-close) cost basis &mdash; a proxy, not your actual fill price.">Since baseline</th>
      <th data-col="10" data-num="1" class="num dim-mobile">1W</th>
      <th data-col="11" data-num="1" class="num dim-mobile">1M</th>
      <th data-col="12" data-num="1" class="num dim-mobile">3M</th>
      <th data-col="13" data-num="1" class="num">YTD</th>
      <th data-col="14" data-num="1" class="num dim-mobile" title="For closed positions: how the stock has moved since you sold. Positive = regret, negative = lucky escape.">Post-exit</th>
    </tr>
  </thead>
  <tbody>{body}</tbody>
</table></div>"""


def render_chart_grid(returns: pd.DataFrame, weekly: pd.DataFrame, meta: pd.DataFrame) -> str:
    cards = []
    for tkr, r in returns.iterrows():
        if tkr not in weekly.columns:
            continue
        s = weekly[tkr].dropna()
        if s.empty:
            continue
        rebased = ((s / s.iloc[0] - 1) * 100).tolist()
        color_class = "up" if r.total_pct >= 0 else "down"
        spark = sparkline_svg(rebased, 220, 70, color_class=color_class,
                              gradient_id=f"grad-{color_class}", show_zero=True)
        industry = _esc(_industry_label(meta, tkr))
        ccy = ticker_currency(meta, tkr)
        ccy_badge = (
            f'<span class="badge-ccy" title="Listed in {ccy}">{ccy}</span>'
            if ccy != BASE_CCY else ""
        )
        weight_badge = (
            f'<span class="badge-weight" title="1 equal-weight unit '
            f'&middot; {r.n_buys} buys, {r.n_sells} sells">1 u</span>'
            if r.status == "open" else
            '<span class="badge-closed" title="Position closed">CLOSED</span>'
        )
        cards.append(
            f'<div class="card" data-ticker="{_esc(tkr)}" data-total="{r.total_pct:.4f}" data-weight="{r.weight:.4f}">'
            f'<div class="card-head">'
            f'<div class="card-head-left">'
            f'<span class="card-tkr">{_esc(tkr)}{ccy_badge}{weight_badge}</span>'
            f'<span class="card-industry">{industry}</span>'
            f'</div>'
            f'<span class="card-pct {_cls(r.total_pct)}">{r.total_pct:+.1f}%</span>'
            f'</div>'
            f'<div class="card-chart">{spark}</div>'
            f'<div class="card-foot">'
            f'<span class="card-meta">{BASE_SYMBOL}{r.latest:,.2f}</span>'
            f'<span class="card-meta-small">was {BASE_SYMBOL}{r.baseline:,.2f}</span>'
            f'</div>'
            f'</div>'
        )
    return '<div class="chart-grid">' + "".join(cards) + "</div>"


def render_contributors(contrib: pd.DataFrame, meta: pd.DataFrame, n: int = 5) -> str:
    top = contrib.head(n)
    bot = contrib.tail(n).iloc[::-1]

    def _row(tkr, r):
        ind = _esc(_industry_label(meta, tkr))
        # "Cost basis" column shows the per-unit avg buy price (each position is
        # one equal-weight unit as of v2.4); contribution below is equal-weight.
        cost_str = f"{BASE_SYMBOL}{r.total_invested:,.0f}"
        return (
            f'<tr data-ticker="{_esc(tkr)}">'
            f'<td class="ct-tkr">{_esc(tkr)}<div class="ct-ind">{ind}</div></td>'
            f'<td class="num ct-wt">{cost_str}</td>'
            f'<td class="num {_cls(r.total_pct)} ct-ret">{r.total_pct:+.1f}%</td>'
            f'<td class="num {_cls(r.contribution_pp)} ct-contrib">{r.contribution_pp:+.2f} pp</td>'
            f'</tr>'
        )

    top_rows = "".join(_row(t, r) for t, r in top.iterrows())
    bot_rows = "".join(_row(t, r) for t, r in bot.iterrows())
    return f"""<section class="contributors">
  <div class="contrib-col">
    <h3>Top contributors</h3>
    <table class="contrib-table">
      <thead><tr><th>Ticker</th><th class="num">Cost basis</th><th class="num">Return</th><th class="num">Contrib</th></tr></thead>
      <tbody>{top_rows}</tbody>
    </table>
  </div>
  <div class="contrib-col">
    <h3>Top detractors</h3>
    <table class="contrib-table">
      <thead><tr><th>Ticker</th><th class="num">Cost basis</th><th class="num">Return</th><th class="num">Contrib</th></tr></thead>
      <tbody>{bot_rows}</tbody>
    </table>
  </div>
</section>"""


def _exit_action(signal_tone: str, analyst_rec: str) -> tuple[str, str]:
    """3x3 heuristic — joins technical signal tone with analyst rec to suggest
    a course of action. Returns (label, tone_class).

    This is build-time analytics, not personalized advice; the goal is to
    highlight tickers where two independent signals agree (high conviction)
    or disagree (worth investigating). The caller is responsible for caveat.
    """
    rec = (analyst_rec or "").strip().lower()
    if rec in ("strong_buy", "buy", "outperform"):
        rec_bucket = "buy"
    elif rec in ("strong_sell", "sell", "underperform"):
        rec_bucket = "sell"
    elif rec in ("hold", "neutral"):
        rec_bucket = "hold"
    else:
        rec_bucket = "none"
    tone = (signal_tone or "neutral").strip().lower()
    # No analyst data — fall back to technical signal only.
    if rec_bucket == "none":
        if tone == "pos":    return ("HOLD",    "pos")
        if tone == "neg":    return ("REVIEW",  "neg")
        return ("MONITOR", "neutral")
    # Two-axis lookup. Order: rows = tone (pos/neutral/neg), cols = rec bucket.
    matrix = {
        ("pos",     "buy"):  ("HOLD",          "pos"),
        ("pos",     "hold"): ("TRIM",          "neutral"),
        ("pos",     "sell"): ("EXIT",          "neg"),
        ("neutral", "buy"):  ("HOLD",          "pos"),
        ("neutral", "hold"): ("MONITOR",       "neutral"),
        ("neutral", "sell"): ("EXIT",          "neg"),
        ("neg",     "buy"):  ("REVIEW THESIS", "neutral"),
        ("neg",     "hold"): ("TRIM",          "neutral"),
        ("neg",     "sell"): ("CUT LOSS",      "neg"),
    }
    return matrix.get((tone, rec_bucket), ("MONITOR", "neutral"))


def render_detractors_strategy(contrib: pd.DataFrame, returns: pd.DataFrame,
                               signals: pd.DataFrame, analyst: pd.DataFrame,
                               meta: pd.DataFrame, n: int = 8,
                               quant_metrics: pd.DataFrame | None = None,
                               prices: pd.DataFrame | None = None,
                               prices_native: pd.DataFrame | None = None) -> str:
    """Full-width 'Top detractors' panel with technical signal + analyst rec +
    suggested action. Only open positions — you can't exit a closed one.

    When ``quant_metrics`` is provided, each row also gets a concrete
    **2× ATR suggested stop** sub-line beneath the action pill — in base
    currency, with the broker-aligned native value in parentheses for
    non-GBP names."""
    if contrib.empty or returns.empty:
        return ""
    # Bottom by contribution (most negative first). Filter to open: closed
    # positions have weight 0 → contribution 0, so they cluster at the bottom
    # of the sort even though they're not actionable.
    open_tickers = set(returns[returns.status == "open"].index.tolist())
    open_contrib = contrib[contrib.index.isin(open_tickers)]
    if open_contrib.empty:
        return ""
    bot = open_contrib.sort_values("contribution_pp", ascending=True).head(n)
    rows = []
    for tkr, r in bot.iterrows():
        ind = _esc(_industry_label(meta, tkr))
        cost_str = f"{BASE_SYMBOL}{r.total_invested:,.0f}"   # per-unit cost basis
        # Technical signal
        if tkr in signals.index:
            sig = signals.loc[tkr]
            sig_label, sig_tone, sig_detail = str(sig.signal), str(sig.tone), str(sig.detail)
        else:
            sig_label, sig_tone, sig_detail = "—", "neutral", ""
        # Analyst rec
        rec_raw = ""
        upside = None
        if not analyst.empty and tkr in analyst.index:
            a = analyst.loc[tkr]
            rec_raw = str(a.get("recommendation") or "")
            target = a.get("target_mean")
            if target is not None and pd.notna(target) and target > 0 and tkr in returns.index:
                # Upside in the same units as the returns table (native ccy)
                latest_native = a.get("current_price")
                if latest_native is not None and pd.notna(latest_native) and latest_native > 0:
                    upside = (float(target) / float(latest_native) - 1) * 100
        rec_label, rec_cls = _REC_LABELS.get(rec_raw, ("—", "an-rec-none"))
        upside_str = f"{upside:+.0f}%" if upside is not None else "—"
        # Suggested action
        action_label, action_tone = _exit_action(sig_tone, rec_raw)
        # Suggested 2× ATR stop (base currency + native parenthetical for non-GBP).
        # NaN-safe: any missing component → no stop line rendered for that row.
        stop_html = ""
        if quant_metrics is not None and tkr in quant_metrics.index \
                and prices is not None and tkr in prices.columns:
            q = quant_metrics.loc[tkr]
            atr_gbp = q.get("atr14_gbp", float("nan"))
            atr_native = q.get("atr14_native", float("nan"))
            close_gbp_series = prices[tkr].dropna()
            if pd.notna(atr_gbp) and not close_gbp_series.empty:
                last_gbp = float(close_gbp_series.iloc[-1])
                stop_gbp = last_gbp - 2.0 * float(atr_gbp)
                stop_gbp_str = f"{BASE_SYMBOL}{stop_gbp:,.2f}"
                native_str = ""
                ccy = ticker_currency(meta, tkr)
                if ccy != BASE_CCY and pd.notna(atr_native) \
                        and prices_native is not None and tkr in prices_native.columns:
                    close_native_series = prices_native[tkr].dropna()
                    if not close_native_series.empty:
                        last_native = float(close_native_series.iloc[-1])
                        stop_native = last_native - 2.0 * float(atr_native)
                        sym = CCY_SYMBOLS.get(ccy, ccy + " ")
                        native_str = f' <span class="dt-action-stop-native">({sym}{stop_native:,.2f})</span>'
                stop_html = (
                    f'<div class="dt-action-stop">'
                    f'<span class="dt-action-stop-label">Stop</span> '
                    f'{stop_gbp_str}{native_str} '
                    f'<span class="dt-action-stop-meta">&minus;2&times; ATR</span>'
                    f'</div>'
                )
        rows.append(
            f'<tr data-ticker="{_esc(tkr)}">'
            f'<td class="dt-tkr">{_esc(tkr)}<div class="dt-ind">{ind}</div></td>'
            f'<td class="num dt-cost">{cost_str}</td>'
            f'<td class="num neg dt-ret">{r.total_pct:+.1f}%</td>'
            f'<td class="num neg dt-contrib">{r.contribution_pp:+.2f} pp</td>'
            f'<td class="dt-sig sig-{sig_tone}">'
            f'<div class="sig-main">{_esc(sig_label)}</div>'
            f'<div class="sig-detail">{_esc(sig_detail)}</div>'
            f'</td>'
            f'<td class="dt-rec"><span class="an-rec {rec_cls}">{rec_label}</span>'
            f'<div class="dt-upside">{upside_str} target</div></td>'
            f'<td class="dt-action"><span class="dt-action-pill dt-action-{action_tone}">{action_label}</span>{stop_html}</td>'
            f'</tr>'
        )
    body = "".join(rows)
    # T17: also build a 3-card stack for narrow viewports. The desktop table
    # has 7 numeric columns that are unusable on phones; the cards distill
    # each row to the essentials: ticker + tone-tagged signal + analyst rec
    # + suggested action. Numeric details are still reachable by tapping the
    # ticker (opens the modal).
    mobile_cards: list[str] = []
    for tkr, r in bot.head(3).iterrows():
        ind = _esc(_industry_label(meta, tkr))
        if tkr in signals.index:
            sig = signals.loc[tkr]
            sig_label, sig_tone = str(sig.signal), str(sig.tone)
        else:
            sig_label, sig_tone = "—", "neutral"
        rec_raw = ""
        if not analyst.empty and tkr in analyst.index:
            rec_raw = str(analyst.loc[tkr].get("recommendation") or "")
        rec_label, rec_cls = _REC_LABELS.get(rec_raw, ("—", "an-rec-none"))
        action_label, action_tone = _exit_action(sig_tone, rec_raw)
        mobile_cards.append(
            f'<div class="dt-mobile-card ticker-clickable" data-ticker="{_esc(tkr)}">'
            f'  <div class="dt-mobile-head">'
            f'    <span class="dt-mobile-tkr">{_esc(tkr)}</span>'
            f'    <span class="dt-mobile-ret neg">{r.total_pct:+.1f}%</span>'
            f'  </div>'
            f'  <div class="dt-mobile-ind">{ind}</div>'
            f'  <div class="dt-mobile-pills">'
            f'    <span class="dt-mobile-pill sig-{sig_tone}" title="Technical signal">{_esc(sig_label)}</span>'
            f'    <span class="dt-mobile-pill an-rec {rec_cls}" title="Analyst recommendation">{rec_label}</span>'
            f'    <span class="dt-mobile-pill dt-action-pill dt-action-{action_tone}" title="Suggested action">{action_label}</span>'
            f'  </div>'
            f'</div>'
        )
    mobile_html = "".join(mobile_cards)
    return f"""<section class="detractors-section">
  <div class="dt-head-row">
    <h3>Top detractors &mdash; exit strategy <span class="muted">({len(bot)})</span></h3>
    <p class="muted">Heaviest drags on your basket return. Suggested action joins
    the technical signal with Wall Street consensus &mdash; agreement = high conviction,
    divergence = review thesis. <strong>Not financial advice</strong>; build-time analytics only.</p>
  </div>
  <div class="dt-scroll">
    <table class="dt-table">
      <thead><tr>
        <th>Ticker</th><th class="num">Cost basis</th><th class="num">Return</th>
        <th class="num">Contrib</th><th>Technical signal</th>
        <th>Analyst</th><th>Suggested action</th>
      </tr></thead>
      <tbody>{body}</tbody>
    </table>
  </div>
  <div class="dt-mobile-cards" aria-label="Top 3 detractors (mobile view)">{mobile_html}</div>
</section>"""


def render_regret_tracker(returns: pd.DataFrame, meta: pd.DataFrame, n: int = 5) -> str:
    """Top regrets and lucky escapes among CLOSED positions.

    For each closed position, post_exit_pct measures (current_price /
    avg_sell_price - 1). A large positive number = you missed gains by
    selling too early (regret). A large negative = the stock tanked after
    you sold (lucky escape).

    Only closed positions with a defined post_exit_pct contribute. Open
    positions are skipped because there's nothing to compare against.
    """
    if returns.empty or "post_exit_pct" not in returns.columns:
        return ""
    closed = returns[returns.status == "closed"].copy()
    closed = closed.dropna(subset=["post_exit_pct"])
    if closed.empty:
        return ""
    closed = closed.sort_values("post_exit_pct", ascending=False)
    regrets = closed.head(n)  # biggest positive moves since exit
    escapes = closed.tail(n).iloc[::-1]  # biggest negative moves, worst first

    def _row(tkr, r):
        ind = _esc(_industry_label(meta, tkr))
        sell_str = f"{BASE_SYMBOL}{r.avg_sell_price:,.2f}" if pd.notna(r.avg_sell_price) else "—"
        last_str = f"{BASE_SYMBOL}{r.latest:,.2f}"
        cls = "pos" if r.post_exit_pct >= 0 else "neg"
        return (
            f'<tr data-ticker="{_esc(tkr)}">'
            f'<td class="rg-tkr">{_esc(tkr)}<div class="rg-ind">{ind}</div></td>'
            f'<td class="num rg-sell">{sell_str}</td>'
            f'<td class="num rg-last">{last_str}</td>'
            f'<td class="num {cls} rg-delta">{r.post_exit_pct:+.1f}%</td>'
            f'</tr>'
        )

    regret_rows = "".join(_row(t, r) for t, r in regrets.iterrows())
    escape_rows = "".join(_row(t, r) for t, r in escapes.iterrows())
    return f"""<section class="regret">
  <div class="regret-col">
    <h3>Biggest regrets <span class="muted">— sold too early</span></h3>
    <table class="regret-table">
      <thead><tr><th>Ticker</th><th class="num">Sold @</th><th class="num">Now</th><th class="num">Post-exit</th></tr></thead>
      <tbody>{regret_rows}</tbody>
    </table>
  </div>
  <div class="regret-col">
    <h3>Lucky escapes <span class="muted">— sold before a drop</span></h3>
    <table class="regret-table">
      <thead><tr><th>Ticker</th><th class="num">Sold @</th><th class="num">Now</th><th class="num">Post-exit</th></tr></thead>
      <tbody>{escape_rows}</tbody>
    </table>
  </div>
</section>"""


def render_untracked(untracked: pd.DataFrame) -> str:
    """Render the 'manual fund' rows that have no ticker/ISIN.

    These can't be priced by yfinance but are surfaced so the user knows
    what's been excluded from the basket math. Embedded "@ <price>" in the
    name is parsed back out into its own column for readability.
    """
    if untracked is None or untracked.empty:
        return ""

    import re
    rows = []
    for _, r in untracked.iterrows():
        name = str(r.get("name", ""))
        # The name field looks like "Fund description here  @  1234.5678" —
        # split into a friendly name + execution price.
        m = re.match(r"^(.*?)\s*@\s*([\d.,]+)\s*$", name)
        if m:
            fund_name, exec_price = m.group(1).strip(), m.group(2).strip()
        else:
            fund_name, exec_price = name, ""
        dt = r.get("date")
        date_str = pd.Timestamp(dt).strftime("%d %b %y") if pd.notna(dt) else "?"
        action = str(r.get("action", "")).strip()
        action_tone = "neg" if "sell" in action.lower() else "pos"
        rows.append(
            f'<tr>'
            f'<td class="ut-date">{date_str}</td>'
            f'<td class="ut-action ut-{action_tone}">{_esc(action)}</td>'
            f'<td class="ut-name">{_esc(fund_name)}</td>'
            f'<td class="num ut-price">{_esc(exec_price)}</td>'
            f'</tr>'
        )
    n = len(untracked)
    return f"""<section class="untracked">
  <div class="untracked-head">
    <h3>Untracked entries <span class="muted">({n})</span></h3>
    <p class="muted">These fund rows have no ticker/ISIN in <code>log.xlsx</code>, so they
    are excluded from the basket math above. Provide LSE/yfinance tickers to fold them in.</p>
  </div>
  <div class="table-scroll">
    <table class="untracked-table">
      <thead><tr>
        <th>Date</th><th>Action</th><th>Fund</th><th class="num">Exec price</th>
      </tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
  </div>
</section>"""


def two_signal_tickers(value_rows: "list[dict] | None") -> list[str]:
    """Tickers flagged by BOTH the value screen and Big Brain (is_bb_idea), in
    value_rows order (the value screen's strength sort)."""
    if not value_rows:
        return []
    return [r["ticker"] for r in value_rows if r.get("is_bb_idea")]


def select_auto_watchlist(value_rows: "list[dict] | None", manual_tickers,
                          max_n: "int | None" = None) -> list[str]:
    """Up to max_n 2-signal tickers NOT already in the manual watchlist. Held
    names are already excluded by build_value_screen (discovery-only)."""
    cap = AUTO_WATCH_MAX if max_n is None else max_n
    manual = set(manual_tickers or ())
    return [t for t in two_signal_tickers(value_rows) if t not in manual][:cap]


def build_combined_watchlist(manual_df: "pd.DataFrame | None",
                             auto_tickers: "list[str]",
                             two_signal_set: "set[str]") -> pd.DataFrame:
    """One watchlist frame: auto picks first (wl_kind='auto'), then manual rows
    (wl_kind='manual_validated' if also 2-signal, else 'manual'). Auto and manual
    are kept disjoint (a manual ticker that is also auto appears once, as auto).
    Columns: ticker, note, wl_kind."""
    auto_set = set(auto_tickers or [])
    two = set(two_signal_set or ())
    rows = [{"ticker": t, "note": "", "wl_kind": "auto"} for t in (auto_tickers or [])]
    if manual_df is not None and not manual_df.empty:
        for _, r in manual_df.iterrows():
            tkr = r["ticker"]
            if tkr in auto_set:
                continue                              # already an auto card
            rows.append({"ticker": tkr, "note": r.get("note", ""),
                         "wl_kind": "manual_validated" if tkr in two else "manual"})
    return pd.DataFrame(rows, columns=["ticker", "note", "wl_kind"])


def build_watchlist_payload(watchlist: pd.DataFrame, prices: pd.DataFrame,
                            prices_native: pd.DataFrame, meta: pd.DataFrame) -> dict:
    """For each watchlist ticker, produce a payload mirroring build_data_payload
    so the existing modal opens cleanly. Baseline = price 12 months ago (in
    BASE_CCY). Tickers with no price data are skipped (logged once)."""
    if watchlist.empty:
        return {}
    cutoff = pd.Timestamp.now(tz="UTC").normalize().tz_localize(None) - pd.DateOffset(months=12)
    payload: dict = {}
    for _, row in watchlist.iterrows():
        tkr = row["ticker"]
        if tkr not in prices.columns:
            print(f"  watchlist: no price data for {tkr}, skipping", file=sys.stderr)
            continue
        s_base = prices[tkr].dropna()
        if s_base.empty:
            print(f"  watchlist: empty series for {tkr}, skipping", file=sys.stderr)
            continue
        s_base = s_base[s_base.index >= cutoff]
        if len(s_base) < 2:
            print(f"  watchlist: <2 points after 12m cutoff for {tkr}, skipping", file=sys.stderr)
            continue
        baseline = float(s_base.iloc[0])
        latest = float(s_base.iloc[-1])
        total_pct = (latest / baseline - 1) * 100 if baseline else 0.0
        weekly = s_base.resample("W-FRI").last().ffill().dropna()
        if weekly.empty:
            weekly = s_base.tail(1)

        ccy = ticker_currency(meta, tkr)
        ccy_symbol = CCY_SYMBOLS.get(ccy, ccy + " ")

        native_baseline = native_latest = native_total = None
        if tkr in prices_native.columns:
            sn = prices_native[tkr].dropna()
            sn = sn[sn.index >= cutoff]
            if len(sn) >= 2:
                native_baseline = float(sn.iloc[0])
                native_latest = float(sn.iloc[-1])
                native_total = (native_latest / native_baseline - 1) * 100 if native_baseline else 0.0

        payload[tkr] = {
            "name": str(meta.loc[tkr, "name"]) if tkr in meta.index else tkr,
            "sector": str(meta.loc[tkr, "sector"]) if tkr in meta.index else "",
            "industry": str(meta.loc[tkr, "industry"]) if tkr in meta.index else "",
            "currency": ccy, "ccy_symbol": ccy_symbol,
            "baseline": baseline, "baseline_date": s_base.index[0].strftime("%Y-%m-%d"),
            "latest": latest, "total": total_pct,
            "w1": None, "m1": None, "m3": None, "ytd": None,
            "weight": 0.0, "contribution": None,
            "signal": "", "signal_tone": "neutral", "signal_detail": "",
            "native_total": native_total, "native_baseline": native_baseline,
            "native_latest": native_latest, "fx_change": None,
            "status": "watch", "shares_held": 0.0,
            "total_invested": 0.0, "total_received": 0.0,
            "realized_pnl": 0.0, "unrealized_pnl": 0.0,
            "post_exit": None, "transactions": [], "note": row.get("note", ""),
            "dates": [d.strftime("%Y-%m-%d") for d in weekly.index],
            "prices": [round(float(p), 4) for p in weekly.tolist()],
        }
        payload[tkr]["wl_kind"] = row.get("wl_kind", "manual")
        # T1: precompute the modal chart polyline (same path as portfolio tickers).
        if baseline:
            _rebased = [(float(p) / baseline - 1) * 100 for p in weekly.tolist()]
            payload[tkr]["chart"] = _modal_polyline_d(_rebased)
    return payload


def render_watchlist(watchlist_payload: dict, meta: pd.DataFrame) -> str:
    if not watchlist_payload:
        return ""
    cards = []
    for tkr, d in watchlist_payload.items():
        ind = _esc(_industry_label(meta, tkr))
        name = _esc(d["name"])
        note = _esc(d.get("note") or "")
        kind = d.get("wl_kind", "manual")
        card_cls = "wl-card wl-auto" if kind == "auto" else "wl-card"
        auto_badge = ('<span class="wl-auto-tag" title="Flagged by both the Value '
                      'screen and Big Brain">Value + BB</span>'
                      if kind in ("auto", "manual_validated") else "")
        total = d["total"]
        cls = "pos" if total >= 0 else "neg"
        ccy_sym = d["ccy_symbol"]
        latest_native = d["native_latest"] if d["native_latest"] is not None else d["latest"]
        latest_str = f"{ccy_sym}{latest_native:,.2f}"
        latest_gbp = f"{BASE_SYMBOL}{d['latest']:,.2f}"
        gbp_line = "" if d["currency"] == BASE_CCY else f'<div class="wl-gbp">≈ {latest_gbp}</div>'
        sparkline = sparkline_svg(
            d["prices"], 180, 48,
            color_class=cls,
            gradient_id="grad-up" if total >= 0 else "grad-down",
        )
        note_html = f'<div class="wl-note">{note}</div>' if note else ""
        # v2.7 entry-signal layer (read with .get so a non-enriched payload still renders).
        verdict = d.get("verdict")
        verdict_html = ""
        if verdict:
            verdict_html = (f'<div class="wl-verdict wl-v-{verdict["tone"]}">'
                            f'{_esc(verdict["label"])}</div>')
        chips = d.get("triggers") or []
        chips_html = ""
        if chips:
            chips_html = '<div class="wl-chips">' + "".join(
                f'<span class="wl-chip wl-c-{c["tone"]}">{_esc(c["label"])}</span>'
                for c in chips) + "</div>"
        cite = d.get("news_cite")
        cite_html = ""
        if cite and cite.get("title"):
            src = _esc(cite.get("publisher") or "")
            title = _esc(cite["title"])
            link = cite.get("link") or ""
            inner = (f'<a href="{safe_url(link)}" target="_blank" rel="noopener">{title}</a>'
                     if link else title)
            cite_html = (f'<div class="wl-cite">{inner}'
                         + (f' <span class="wl-cite-src">&mdash; {src}</span>' if src else "")
                         + '</div>')
        cards.append(
            f'<div class="{card_cls}" data-ticker="{_esc(tkr)}">'
            f'  <div class="wl-head">'
            f'    <div class="wl-tkr">{_esc(tkr)}{auto_badge}<div class="wl-ind">{ind}</div></div>'
            f'    <div class="wl-pct {cls}">{total:+.1f}%</div>'
            f'  </div>'
            f'  <div class="wl-name">{name}</div>'
            f'  <div class="wl-price"><span class="wl-latest">{latest_str}</span>{gbp_line}</div>'
            f'  <div class="wl-spark {cls}">{sparkline}</div>'
            f'  {verdict_html}{chips_html}{cite_html}'
            f'  <div class="wl-foot"><span class="wl-period">12-month</span>{note_html}</div>'
            f'</div>'
        )
    # v3.0 #4: arrow-paged so 6+ names don't spill into a 2nd row. Page size =
    # however many cards fill the row (measured in JS, 6 desktop / 3 mobile), so
    # page 1 fills before page 2. Nav rendered whenever paging could be needed
    # (n above the mobile column count); JS hides it when one row holds all.
    n = len(watchlist_payload)
    nav, section_attr = "", ""
    if n > WATCH_COLS_MOBILE:
        section_attr = ' data-wl-pageable="1"'
        nav = ('<div class="wl-nav">'
               '<button type="button" class="wl-arrow wl-prev" aria-label="Previous set">&#8249;</button>'
               '<span class="wl-page-ind"><span class="wl-page-cur">1</span>'
               '&#8202;/&#8202;<span class="wl-page-total">1</span></span>'
               '<button type="button" class="wl-arrow wl-next" aria-label="Next set">&#8250;</button>'
               '</div>')
    return f"""<section class="watchlist-section"{section_attr}>
  <div class="wl-head-row">
    <div class="wl-head-top">
      <h3>Watchlist <span class="muted">({n})</span></h3>
      {nav}
    </div>
    <p class="muted">Names you're tracking but don't (yet) hold &mdash; each with an
    entry read (near-low / oversold / unusual volume / Street upside). Cards badged
    <span class="wl-auto-tag">Value + BB</span> were flagged by both the Value screen
    and Big Brain; shaded ones were auto-surfaced (not in your <code>watchlist.csv</code>).
    Click a card for the full chart.</p>
  </div>
  <div class="wl-grid">{''.join(cards)}</div>
</section>"""


def _analyst_empty() -> pd.DataFrame:
    """Empty analyst frame with the columns build_watchlist_signals reads
    (keeps tests terse; production passes the real analyst cache)."""
    return pd.DataFrame(columns=["target_mean", "recommendation", "num_analysts"])


def build_watchlist_signals(
    watchlist_payload: dict,
    quant_metrics: "pd.DataFrame | None",
    analyst: "pd.DataFrame | None",
    ticker_news: "pd.DataFrame | None",
    meta: pd.DataFrame,
) -> dict:
    """Enrich each watchlist payload entry with an entry-signal layer:
    triggers[] (chips), a verdict, and a news cite. Pure + total: any missing
    source is skipped, never raised. Thresholds mirror Big Brain."""
    if not watchlist_payload:
        return watchlist_payload
    now = pd.Timestamp.now(tz="UTC")
    has_q = quant_metrics is not None and not quant_metrics.empty
    has_a = analyst is not None and not analyst.empty

    def _news_cite(tkr):
        if ticker_news is None or tkr not in ticker_news.index:
            return None
        raw = ticker_news.loc[tkr, "items_json"]
        try:
            items = json.loads(raw) if isinstance(raw, str) and raw else []
        except (ValueError, TypeError):
            items = []
        return _bb_news_cite(items, now)

    for tkr, d in watchlist_payload.items():
        triggers: list[dict] = []
        # --- technical chips (from quant_metrics) ---------------------------
        if has_q and tkr in quant_metrics.index:
            row = quant_metrics.loc[tkr]
            r52 = row.get("range52w_pct")
            rsi = row.get("rsi14")
            vol = row.get("vol_ratio")
            sma = row.get("sma200_dist_pct")
            if pd.notna(r52) and float(r52) <= 10:
                triggers.append({"label": "Near low", "tone": "buy"})
            if pd.notna(rsi):
                if float(rsi) < 30:
                    triggers.append({"label": "Oversold", "tone": "buy"})
                elif float(rsi) > 70:
                    triggers.append({"label": "Overbought", "tone": "caution"})
            if pd.notna(vol) and float(vol) > 2.0:
                triggers.append({"label": f"Vol {float(vol):.1f}×", "tone": "neutral"})
            if pd.notna(sma):
                if float(sma) < 0:
                    triggers.append({"label": "Below 200-day", "tone": "buy"})
                elif float(sma) > 15:
                    triggers.append({"label": "Extended", "tone": "caution"})
        # --- analyst upside chip --------------------------------------------
        upside = None
        if has_a and tkr in analyst.index:
            a = analyst.loc[tkr]
            target = a.get("target_mean")
            # Prefer the analyst frame's own current_price (captured with the
            # target -> guaranteed same native unit, so the pence divisor cancels
            # exactly as in the main holdings table). Fall back to native_latest.
            cur = a.get("current_price")
            if cur is None or pd.isna(cur) or float(cur) <= 0:
                cur = d.get("native_latest") or d.get("latest")
            if target is not None and pd.notna(target) and float(target) > 0 and cur:
                upside = (float(target) / float(cur) - 1) * 100
                if upside > 0:
                    triggers.append({"label": f"+{upside:.0f}% to target", "tone": "buy"})
        # --- verdict (function of the chip stack) ---------------------------
        labels = {t["label"] for t in triggers}
        has_upside = upside is not None and upside > 0
        if "Near low" in labels and ("Oversold" in labels or has_upside):
            verdict = {"label": "Buy zone", "tone": "buy"}
        elif "Extended" in labels or "Overbought" in labels:
            verdict = {"label": "Cooling off", "tone": "caution"}
        else:
            verdict = {"label": "Watching", "tone": "neutral"}
        d["triggers"] = triggers
        d["verdict"] = verdict
        d["news_cite"] = _news_cite(tkr)
    return watchlist_payload


def build_analyst_payload(candidates: list[str], analyst: pd.DataFrame,
                          prices_native: pd.DataFrame, meta: pd.DataFrame,
                          signals: pd.DataFrame | None = None,
                          top_n: int = ANALYST_TOP_N,
                          quant_metrics: pd.DataFrame | None = None) -> list[dict]:
    """For each candidate ticker with a usable analyst target, compute upside
    using the latest native close, attach the technical signal (so the card can
    surface analyst/technical agreement or conflict), sort by upside desc, return top N."""
    if not candidates or analyst.empty:
        return []
    sig_df = signals if signals is not None else pd.DataFrame()
    rows: list[dict] = []
    for tkr in candidates:
        if tkr not in analyst.index:
            continue
        a = analyst.loc[tkr]
        target = a.get("target_mean")
        if target is None or pd.isna(target) or target <= 0:
            continue
        if tkr not in prices_native.columns:
            continue
        s = prices_native[tkr].ffill().dropna()
        if s.empty:
            continue
        current_native = float(s.iloc[-1])
        ccy = ticker_currency(meta, tkr)
        _, divisor = normalize_currency(str(meta.loc[tkr, "currency"]) if tkr in meta.index else "USD")
        # Yahoo returns prices in major units; LSE pence-quoted symbols come
        # back as pence here too, so apply the same divisor for parity with prices_native.
        target_major = float(target) / divisor
        upside_pct = (target_major / current_native - 1) * 100 if current_native else 0.0
        # Dispersion: a +40% mean off a [-10%, +90%] spread is far less actionable
        # than the same mean off a tight consensus. Surface high/low (already
        # fetched) so a wide range is visible rather than hidden behind the mean.
        def _norm_t(v):
            return (float(v) / divisor) if (v is not None and pd.notna(v) and float(v) > 0) else None
        target_high = _norm_t(a.get("target_high"))
        target_low = _norm_t(a.get("target_low"))
        spread_pct = (((target_high - target_low) / current_native * 100)
                      if (target_high is not None and target_low is not None and current_native)
                      else None)
        rec = (a.get("recommendation") or "").strip().lower()
        # Technical signal — empty/neutral when no data
        if not sig_df.empty and tkr in sig_df.index:
            sig_row = sig_df.loc[tkr]
            sig_label = str(sig_row.signal)
            sig_tone = str(sig_row.tone)
        else:
            sig_label, sig_tone = "—", "neutral"
        # Current RSI (re-entry context: "is this a good moment to buy back in?")
        rsi14 = None
        if quant_metrics is not None and tkr in quant_metrics.index:
            v = quant_metrics.loc[tkr, "rsi14"]
            if pd.notna(v):
                rsi14 = float(v)
        rows.append({
            "ticker": tkr,
            "name": str(meta.loc[tkr, "name"]) if tkr in meta.index else tkr,
            "industry": _industry_label(meta, tkr),
            "currency": ccy,
            "ccy_symbol": CCY_SYMBOLS.get(ccy, ccy + " "),
            "current": current_native,
            "target_mean": target_major,
            "target_high": target_high,
            "target_low": target_low,
            "spread_pct": spread_pct,
            "upside_pct": upside_pct,
            "num_analysts": int(a["num_analysts"]) if pd.notna(a.get("num_analysts")) else 0,
            "recommendation": rec,
            "signal": sig_label,
            "signal_tone": sig_tone,
            "rsi14": rsi14,
        })
    rows.sort(key=lambda x: x["upside_pct"], reverse=True)
    return rows[:top_n]


_REC_LABELS = {
    "strong_buy":  ("STRONG BUY", "an-rec-strong-buy"),
    "buy":         ("BUY",        "an-rec-buy"),
    "outperform":  ("BUY",        "an-rec-buy"),
    "hold":        ("HOLD",       "an-rec-hold"),
    "neutral":     ("HOLD",       "an-rec-hold"),
    "underperform":("SELL",       "an-rec-sell"),
    "sell":        ("SELL",       "an-rec-sell"),
    "strong_sell": ("STRONG SELL","an-rec-strong-sell"),
    "":            ("—",          "an-rec-none"),
}


def render_analyst_signals(rows: list[dict], candidate_pool_size: int) -> str:
    if not rows and candidate_pool_size == 0:
        return ""
    cards = []
    for d in rows:
        label, rec_cls = _REC_LABELS.get(d["recommendation"], ("—", "an-rec-none"))
        upside_cls = "pos" if d["upside_pct"] >= 0 else "neg"
        ccy_sym = d["ccy_symbol"]
        cur = f"{ccy_sym}{d['current']:,.2f}"
        sig_tone = d.get("signal_tone", "neutral")
        sig_label = d.get("signal", "—")
        # RSI pill — always shown when available. Color follows the same
        # convention as the modal: red ≥70 (overbought), green ≤30 (oversold),
        # dim otherwise. "—" when no RSI yet (typically <15 days of history).
        rsi = d.get("rsi14")
        if rsi is None:
            rsi_html = '<span class="an-rsi an-rsi-dim" title="No RSI">RSI &mdash;</span>'
        else:
            if rsi >= 70:
                rsi_cls = "an-rsi-hot"
                rsi_title = "Overbought (RSI ≥ 70) — pullback risk"
            elif rsi <= 30:
                rsi_cls = "an-rsi-cold"
                rsi_title = "Oversold (RSI ≤ 30) — potential bounce"
            else:
                rsi_cls = "an-rsi-neutral"
                rsi_title = "Neutral momentum (30 < RSI < 70)"
            rsi_html = f'<span class="an-rsi {rsi_cls}" title="{rsi_title}">RSI {rsi:.0f}</span>'
        # Target dispersion: show the analyst high–low range so a wide (low-
        # agreement) spread doesn't read like a tight consensus behind the mean.
        th, tl, spread = d.get("target_high"), d.get("target_low"), d.get("spread_pct")
        if th is not None and tl is not None:
            wide = spread is not None and spread >= 40
            range_html = (
                f' <span class="an-dot">·</span> '
                f'<span class="an-range{" an-range-wide" if wide else ""}" '
                f'title="Analyst target range {ccy_sym}{tl:,.2f}&ndash;{ccy_sym}{th:,.2f}'
                f'{f" ({spread:.0f}% wide &mdash; low agreement)" if spread is not None else ""}. '
                f'The +% upside is off the MEAN target.">'
                f'range {ccy_sym}{tl:,.0f}&ndash;{ccy_sym}{th:,.0f}</span>')
        else:
            range_html = ""
        cards.append(
            f'<div class="an-card" data-ticker="{_esc(d["ticker"])}">'
            f'  <div class="an-head">'
            f'    <div class="an-tkr">{_esc(d["ticker"])}<div class="an-ind">{_esc(d["industry"])}</div></div>'
            f'    <div class="an-rec {rec_cls}">{label}</div>'
            f'  </div>'
            f'  <div class="an-name">{_esc(d["name"])}</div>'
            f'  <div class="an-line"><span class="an-cur">{cur}</span>'
            f'    <span class="an-dot">·</span>'
            f'    <span class="an-upside {upside_cls}">{d["upside_pct"]:+.1f}% upside</span></div>'
            f'  <div class="an-signal sig-{sig_tone}">'
            f'    <span class="an-signal-dot"></span><span class="an-signal-label">{_esc(sig_label)}</span>'
            f'    {rsi_html}</div>'
            f'  <div class="an-foot">{d["num_analysts"]} analysts{range_html}</div>'
            f'</div>'
        )
    shown = len(rows)
    if shown >= candidate_pool_size:
        counter = f"{shown} candidate{'s' if shown != 1 else ''} ranked by upside"
    else:
        counter = f"Top {shown} of {candidate_pool_size} candidates by upside"
    body = f'<div class="an-grid">{"".join(cards)}</div>' if cards else (
        '<div class="an-empty">No closed positions have analyst targets — '
        'panel will populate once yfinance returns target prices for at least one.</div>'
    )
    return f"""<section class="analyst-section">
  <div class="an-head-row">
    <h3>Re-entry ideas <span class="muted">({shown})</span></h3>
    <p class="muted">{counter} &middot; Wall Street mean target vs latest close, for closed
    positions only (stocks you previously held). Cached {ANALYST_TTL_DAYS}d via yfinance.
    Click a card for the full chart.</p>
  </div>
  {body}
</section>"""


def build_industry_attribution(returns: pd.DataFrame, meta: pd.DataFrame,
                                top_n: int = 12) -> tuple[list[dict], float]:
    """Aggregate the user's OPEN positions by industry to show which industries
    drive the basket return up or down.

    Each row's "contribution to basket" is its equal-weight return share —
    (industry_n / basket_n) * industry_avg_return. Sum across all industries
    equals the basket return (modulo tiny rounding). Each position counts once
    (v2.4). Returns (rows_sorted_by_contribution_desc, basket_avg_return).
    """
    if returns.empty:
        return [], 0.0
    open_pos = returns[returns.status == "open"].copy()
    if open_pos.empty:
        return [], 0.0
    # Robustness (M10): mirror compute_currency_exposure's per-row guard. Under the
    # vectorized sums below a NaN weight would silently drop a name from its
    # industry average while still counting it (holdings-count vs avg mismatch),
    # and a NaN return would render a phantom 0.0% industry. Coerce weight to the
    # equal-weight default and drop names with no computed return (can't attribute
    # a return we don't have).
    open_pos["weight"] = [
        float(w) if pd.notna(w) and float(w) > 0 else 1.0
        for w in open_pos["weight"]
    ]
    open_pos = open_pos[pd.notna(open_pos["total_pct"])].copy()
    if open_pos.empty:
        return [], 0.0
    # Map ticker -> industry from meta. Tickers with no industry are bucketed
    # as "Other" so basket attribution still sums to the basket total.
    open_pos["industry"] = [
        (str(meta.loc[t, "industry"] or meta.loc[t, "sector"] or "").strip()
         if t in meta.index else "") or "Other"
        for t in open_pos.index
    ]
    total_w = float(open_pos["weight"].sum())   # equal-weight => count of open names
    if total_w <= 0:
        return [], 0.0
    # Basket-wide equal-weight average return — anchor for "vs basket avg"
    basket_avg = float((open_pos["weight"] * open_pos["total_pct"]).sum() / total_w)
    rows = []
    for industry, g in open_pos.groupby("industry"):
        ind_w = float(g["weight"].sum())
        if ind_w <= 0:
            continue
        ind_avg = float((g["weight"] * g["total_pct"]).sum() / ind_w)
        contrib = (ind_w / total_w) * ind_avg   # pp contribution to basket
        rows.append({
            "industry": industry,
            "n_holdings": int(len(g)),
            "cost_basis": float(g["total_invested"].sum()),  # per-unit £ cost basis (display)
            "avg_return": ind_avg,
            "contribution_pp": contrib,
            "vs_basket": ind_avg - basket_avg,
        })
    rows.sort(key=lambda r: r["contribution_pp"], reverse=True)
    return rows[:top_n], basket_avg


def render_industry_attribution(rows: list[dict], basket_avg: float) -> str:
    if not rows:
        return ""
    # Find max absolute contribution to scale the horizontal bars
    max_abs = max((abs(r["contribution_pp"]) for r in rows), default=1.0) or 1.0
    body = []
    for r in rows:
        ret_cls = "pos" if r["avg_return"] >= 0 else "neg"
        contrib_cls = "pos" if r["contribution_pp"] >= 0 else "neg"
        vs_cls = "pos" if r["vs_basket"] >= 0 else "neg"
        # Bar: width % proportional to |contribution| / max_abs.
        # Positive bars sit right of the zero axis (col 5); negative left.
        bar_pct = abs(r["contribution_pp"]) / max_abs * 50  # max half-width 50%
        # v2.1 #5: numeric label anchored to the axis line, ALWAYS on the side
        # OPPOSITE the bar tip. Positives -> label left-of-axis right-aligned;
        # negatives -> label right-of-axis left-aligned. Two payoffs:
        #   (a) all labels of the same sign form a tight vertical column at
        #       the axis line, so digit count (1.49 vs 29.88) is visible by
        #       horizontal extent at a glance
        #   (b) bars and labels never overlap, no inside/outside conditional,
        #       label visibility never depends on bar magnitude.
        # Sign-aware format: positives drop the redundant "+" (color = sign);
        # negatives keep the "-" because magnitude + sign in mono is clearer.
        if r["contribution_pp"] >= 0:
            contrib_label = f"{r['contribution_pp']:.2f}"
            bar_style = f'left:50%;width:{bar_pct:.1f}%;background:var(--up);'
            label_style = 'right:50%;text-align:right;padding-right:6px;'
        else:
            contrib_label = f"{r['contribution_pp']:.2f}"  # already includes "-"
            bar_style = f'left:{50 - bar_pct:.1f}%;width:{bar_pct:.1f}%;background:var(--down);'
            label_style = 'left:50%;text-align:left;padding-left:6px;'
        label_cls = f"ia-bar-label {contrib_cls}"
        body.append(
            f'<tr class="attribution-row-clickable" data-industry="{_esc(r["industry"])}" '
            f'title="Click to see every open position in {_esc(r["industry"])}">'
            f'<td class="ia-industry">{_esc(r["industry"])}</td>'
            f'<td class="num ia-n">{r["n_holdings"]}</td>'
            f'<td class="num ia-cost">{BASE_SYMBOL}{r["cost_basis"]:,.0f}</td>'
            f'<td class="num {ret_cls} ia-ret">{r["avg_return"]:+.1f}%</td>'
            f'<td class="num {contrib_cls} ia-contrib">{r["contribution_pp"]:+.2f} pp</td>'
            f'<td class="num {vs_cls} ia-vs">{r["vs_basket"]:+.1f} pp</td>'
            f'<td class="ia-bar"><div class="ia-bar-axis"></div>'
            f'<div class="ia-bar-fill" style="{bar_style}"></div>'
            f'<span class="{label_cls}" style="{label_style}">{contrib_label}</span></td>'
            f'</tr>'
        )
    return f"""<section class="attribution-section">
  <div class="ia-head-row">
    <h3>Industry attribution <span class="muted">({len(rows)})</span></h3>
    <p class="muted">Equal-weight contribution to your basket return, grouped by industry —
    open positions only. Basket equal-weight avg return: <strong>{basket_avg:+.2f}%</strong>.
    Bars show each industry's signed contribution to the basket; longer = bigger driver.</p>
  </div>
  <div class="ia-scroll">
    <table class="ia-table">
      <thead><tr>
        <th>Industry</th>
        <th class="num">Holdings</th>
        <th class="num">Cost basis</th>
        <th class="num">Avg return</th>
        <th class="num">Contrib</th>
        <th class="num">vs basket</th>
        <th>Contribution bar</th>
      </tr></thead>
      <tbody>{''.join(body)}</tbody>
    </table>
  </div>
</section>"""


def compute_currency_exposure(returns: pd.DataFrame, meta: pd.DataFrame) -> list[dict]:
    """v1.9 #3 / v2.4: equal-weight currency mix of open positions.

    Each open position counts once (one equal-weight unit) toward its native
    currency bucket, so the split reflects how many of your names sit in each
    currency rather than how much capital — consistent with the equal-weight
    basket. Returns dicts sorted by share desc:

        [{"ccy": "USD", "ccy_symbol": "$", "invested": 14.0,
          "share": 0.62, "n": 14}, ...]

    Empty list when there are no open positions.
    """
    if returns.empty:
        return []
    open_pos = returns[returns.status == "open"]
    if open_pos.empty:
        return []
    buckets: dict[str, dict] = {}
    for tkr, r in open_pos.iterrows():
        ccy = ticker_currency(meta, tkr) or BASE_CCY
        w = float(r.weight) if pd.notna(r.weight) and r.weight > 0 else 1.0
        b = buckets.setdefault(ccy, {"ccy": ccy, "invested": 0.0, "n": 0})
        b["invested"] += w            # equal mode: 1 each; value mode: cost basis
        b["n"] += 1
    total = sum(b["invested"] for b in buckets.values())
    if total <= 0:
        return []
    out = []
    for ccy, b in buckets.items():
        out.append({
            "ccy": ccy,
            "ccy_symbol": CCY_SYMBOLS.get(ccy, ccy + " "),
            "invested": b["invested"],
            "share": b["invested"] / total,
            "n": b["n"],
        })
    out.sort(key=lambda x: -x["share"])
    return out


def render_currency_exposure(rows: list[dict]) -> str:
    """v1.9 #3: render the currency exposure section -- a horizontal stacked
    bar plus a small legend. Highlights single-currency concentration (>= 80%)
    in the muted note line; a balanced split renders the same UI without the
    warning.
    """
    if not rows or len(rows) < 1:
        return ""
    # Distinct hue per currency, cycled deterministically. Major currencies get
    # stable colors for visual familiarity.
    HUES = {
        "GBP": "#34d399", "USD": "#f59e0b", "EUR": "#60a5fa",
        "JPY": "#a78bfa", "CHF": "#f472b6", "HKD": "#f87171",
        "CAD": "#34d399", "AUD": "#fbbf24",
    }
    FALLBACK = ["#94a3b8", "#cbd5e1", "#64748b", "#475569"]
    fb_iter = iter(FALLBACK)
    for r in rows:
        r["color"] = HUES.get(r["ccy"], next(fb_iter, "#94a3b8"))
    # Stacked bar
    segments = []
    for r in rows:
        pct = r["share"] * 100
        segments.append(
            f'<div class="ccy-seg" style="width:{pct:.2f}%;background:{r["color"]}" '
            f'title="{r["ccy"]}: {pct:.1f}% &middot; {r["n"]} position{"s" if r["n"] != 1 else ""}"></div>'
        )
    seg_html = "".join(segments)
    # Legend
    legend = []
    for r in rows:
        legend.append(
            f'<div class="ccy-legend-row">'
            f'<span class="ccy-legend-swatch" style="background:{r["color"]}"></span>'
            f'<span class="ccy-legend-ccy">{r["ccy"]}</span>'
            f'<span class="ccy-legend-share">{r["share"] * 100:.1f}%</span>'
            f'<span class="ccy-legend-n">{r["n"]} pos</span>'
            f'</div>'
        )
    legend_html = "".join(legend)
    # Concentration note
    top = rows[0]
    if top["share"] >= 0.80:
        note = (f'<span class="ccy-note">High concentration in {top["ccy"]} '
                f'({top["share"] * 100:.0f}% of open positions) &mdash; its FX moves '
                f'affect a large share of the basket.</span>')
    elif len(rows) == 1:
        note = (f'<span class="ccy-note">All open positions are in {top["ccy"]}.</span>')
    else:
        note = (f'<span class="ccy-note">{len(rows)} currencies in basket. '
                f'Top exposure: {top["ccy"]} at {top["share"] * 100:.0f}%.</span>')
    return f"""<section class="ccy-exposure-section">
  <div class="ccy-head-row">
    <h3>Currency exposure</h3>
    <p class="muted">{note}</p>
  </div>
  <div class="ccy-bar">{seg_html}</div>
  <div class="ccy-legend">{legend_html}</div>
</section>"""


# --------------------------------------------------------------------------
# v2.2 "Big Brain says" -- per-ticker signal-stacking engine
# --------------------------------------------------------------------------
# Ranks open positions by stacked, cross-panel signal strength and surfaces
# the top 3 with a confident-punchy narrative + best-effort news citation.
# Pure read-only over already-computed build artefacts -- no I/O, no extra
# metric computation. See big-brain-rethink-spec.md.

# v2.2 rethink: per-ticker signal-stacking engine. See big-brain-rethink-spec.md.
BB_SEVERITY_RANK = {"warn": 0, "watch": 1, "info": 2}
_BB_NEWS_FLURRY_DAYS = 7      # window for the news_flurry flag
_BB_NEWS_CITE_DAYS = 14       # window for the best-effort citation
_BB_TITLE_TAGS = {
    "exhausted_winner": "Running hot",
    "heavy_bleeder": "Big position bleeding",
    "divergence": "Tape vs Street",
    "crowded_strength": "Carrying the basket",
    "quiet_breakout": "Breaking out",
    "capitulation": "Washed out",
    "catalyst": "Catalyst in the tape",
    "ran_without_you": "Ran without you",
    "missing_idea": "Setup you're missing",
    "fallback": "Signals stacking",
}


def _bb_score(flags: list[dict]) -> tuple[float, float, int]:
    """(score, raw, n_domains). score = raw weight sum * diversity multiplier,
    where multiplier = 1 + 0.25*(distinct_domains-1), capped at 2.0."""
    if not flags:
        return 0.0, 0.0, 0
    raw = float(sum(f["weight"] for f in flags))
    n_domains = len({f["domain"] for f in flags})
    mult = min(2.0, 1.0 + 0.25 * (n_domains - 1))
    return raw * mult, raw, n_domains


def _bb_severity(flags: list[dict]) -> str:
    """Net weighted direction -> severity. Bearish on a big position is the
    only 'warn'; any net-negative or single strong-bear flag is 'watch'."""
    net = sum(f["weight"] * f["dir"] for f in flags)
    has_top_weight = any(f["id"] == "top_weight" for f in flags)
    strong_bear = any(f["dir"] == -1 and f["weight"] >= 2.0 for f in flags)
    if net <= -2 and has_top_weight:
        return "warn"
    if net < 0 or strong_bear:
        return "watch"
    return "good"


def _bb_match_archetype(ids: set[str]) -> str | None:
    """Most-specific-first; first match wins. Operates on the set of fired flag
    ids for one ticker. The richer current-tape archetypes are checked BEFORE the
    context fallbacks (post_exit -> ran_without_you, beats_your_sector ->
    missing_idea) so a sold or idea name that ALSO shows a strong pattern gets the
    more informative title instead of collapsing to the catch-all (H8)."""
    if {"near_high", "overbought"} <= ids and (
            "fading_volume" in ids or "extended" in ids):
        return "exhausted_winner"
    if ("downtrend" in ids or "near_low" in ids) and "top_weight" in ids and (
            "downgrade" in ids or "big_detractor" in ids):
        return "heavy_bleeder"
    if ("downgrade" in ids and "near_high" in ids) or (
            "upgrade" in ids and "near_low" in ids):
        return "divergence"
    if {"top_weight", "big_contributor", "extended"} <= ids:
        return "crowded_strength"
    if "downtrend" not in ids and "unusual_volume" in ids and (
            "upgrade" in ids or "big_upside" in ids):
        return "quiet_breakout"
    if {"oversold", "near_low", "big_detractor"} <= ids:
        return "capitulation"
    if {"unusual_volume", "big_move_1w", "news_flurry"} <= ids:
        return "catalyst"
    # Context fallbacks — only when nothing richer fired.
    if "post_exit" in ids:
        return "ran_without_you"
    if "beats_your_sector" in ids:
        return "missing_idea"
    return None


def _bb_num(x) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return float("nan")
    return v


def _bb_flags_for(tkr, row, q, signal, an, moves_for_tkr, news_items,
                  weight_rank, is_top_contrib, is_bottom_contrib, now,
                  beats_sector_gap=None, sector_name=""):
    """Return the list of fired flag dicts for one open ticker. All inputs are
    pre-sliced scalars/objects so this stays a pure, testable function.

    q / an may be None. moves_for_tkr is a list of rating-move dicts for this
    ticker. news_items is the parsed list for this ticker (possibly empty)."""
    flags: list[dict] = []

    def add(fid, domain, weight, direction, pill, frag):
        flags.append({"id": fid, "domain": domain, "weight": weight,
                      "dir": direction, "pill": pill, "frag": frag})

    # ---- sold (closed only) ----
    if str(row.get("status") or "").lower() == "closed":
        pe = _bb_num(row.get("post_exit_pct"))
        if pe == pe and pe >= 10:
            add("post_exit", "position", 1.5, 0, f"+{pe:.0f}% since sold",
                f"up {pe:.0f}% since you sold")
        elif pe == pe and pe <= -10:
            add("post_exit", "position", 1.0, 0, f"{pe:.0f}% since sold",
                f"down {abs(pe):.0f}% since you sold — dodged it")

    # ---- position ----
    if weight_rank is not None and weight_rank <= 5:
        w = 2.5 if weight_rank <= 3 else 2.0
        add("top_weight", "position", w, 0,
            f"#{int(weight_rank)} weight", f"your #{int(weight_rank)} weight")
    if is_top_contrib:
        add("big_contributor", "position", 1.5, 1,
            "top contributor", "one of your biggest contributors")
    if is_bottom_contrib:
        add("big_detractor", "position", 1.5, -1,
            "top detractor", "one of your biggest detractors")
    total_pct = _bb_num(row.get("total_pct"))
    bdate = row.get("baseline_date")
    if total_pct == total_pct and total_pct > 50 and bdate is not None:
        try:
            held_days = (now.tz_convert(None) - pd.Timestamp(bdate)).days
        except (TypeError, ValueError):
            held_days = 0
        if held_days > 365:
            add("long_winner", "position", 1.0, 1,
                f"+{total_pct:.0f}% / {held_days}d",
                f"up {total_pct:.0f}% over {held_days} days held")

    # ---- trend ----
    rsi = _bb_num(q.get("rsi14")) if q is not None else float("nan")
    ext = _bb_num(q.get("sma200_dist_pct")) if q is not None else float("nan")
    r52 = _bb_num(q.get("range52w_pct")) if q is not None else float("nan")
    if rsi == rsi and rsi >= 70:
        w = 2.0 if rsi >= 78 else 1.5
        add("overbought", "trend", w, -1, f"RSI {rsi:.0f}",
            f"overbought at RSI {rsi:.0f}")
    if rsi == rsi and rsi <= 30:
        add("oversold", "trend", 1.5, 1, f"RSI {rsi:.0f}",
            f"oversold at RSI {rsi:.0f}")
    if ext == ext and ext >= 50:
        w = 1.5 if ext >= 100 else 1.0
        add("extended", "trend", w, -1, f"{ext:.0f}% >200DMA",
            f"stretched {ext:.0f}% above its 200-day")
    if r52 == r52 and r52 >= 90:
        add("near_high", "trend", 1.0, 0, "near 52w high", "near its 52-week high")
    if r52 == r52 and r52 <= 10:
        add("near_low", "trend", 1.0, -1, "near 52w low", "near its 52-week low")
    sig = str(signal or "").strip().lower()
    if sig in ("strong downtrend", "trending down"):
        add("downtrend", "trend", 1.0, -1, "downtrend", "in a downtrend")

    # ---- flow ----
    vr = _bb_num(q.get("vol_ratio")) if q is not None else float("nan")
    if vr == vr and vr >= 2.0:
        add("unusual_volume", "flow", 1.5, 0, f"{vr:.1f}x vol",
            f"trading on {vr:.1f}x average volume")
    if vr == vr and vr <= 0.6 and rsi == rsi and rsi >= 65:
        add("fading_volume", "flow", 1.0, -1, f"{vr:.1f}x vol",
            "running on thinning volume")
    w1 = _bb_num(row.get("1w_pct"))
    if w1 == w1 and abs(w1) >= 10:
        add("big_move_1w", "flow", 1.0, 1 if w1 > 0 else -1,
            f"{w1:+.0f}% 1w", f"moved {w1:+.0f}% this week")
    m1 = _bb_num(row.get("1m_pct"))
    m3 = _bb_num(row.get("3m_pct"))
    if (m1 == m1 and m3 == m3 and abs(m1) >= 4 and abs(m3) >= 4
            and (m1 > 0) != (m3 > 0)):
        add("reversal", "flow", 1.0, 0, "reversal",
            f"1m {m1:+.0f}% vs 3m {m3:+.0f}% — turning")

    # ---- street ----
    # rating_moves carries recommendation changes too, but their direction is
    # not signed; we use only target-price moves (signed) for up/down flags.
    target_moves = [m for m in moves_for_tkr if m.get("kind") == "target"]
    cuts = [_bb_num(m.get("pct_change")) for m in target_moves
            if _bb_num(m.get("pct_change")) <= -5]
    raises = [_bb_num(m.get("pct_change")) for m in target_moves
              if _bb_num(m.get("pct_change")) >= 5]
    if cuts:
        cut = min(cuts)
        add("downgrade", "street", 2.0, -1, f"target {cut:+.0f}%",
            f"the Street cut its target {cut:+.0f}%")
    if raises:
        rai = max(raises)
        add("upgrade", "street", 1.5, 1, f"target {rai:+.0f}%",
            f"the Street raised its target {rai:+.0f}%")
    if an is not None:
        tgt = _bb_num(an.get("target_mean"))
        last = _bb_num(row.get("latest"))
        if tgt == tgt and last == last and last > 0:
            upside = (tgt / last - 1) * 100
            if upside >= 25:
                add("big_upside", "street", 1.0, 1, f"+{upside:.0f}% to target",
                    f"{upside:.0f}% below the average analyst target")

    # ---- relational (universe only) ----
    if beats_sector_gap is not None and beats_sector_gap >= 10:
        label = sector_name or "sector"
        add("beats_your_sector", "relative", 1.5, 1,
            f"+{beats_sector_gap:.0f}pp vs your {label}",
            f"outpacing every {label} you own by {beats_sector_gap:.0f}pp")

    return flags


def _bb_sector_avg_returns(returns: pd.DataFrame, meta: pd.DataFrame) -> dict:
    """Mean total_pct of OPEN holdings grouped by industry (fallback sector)."""
    if returns is None or returns.empty:
        return {}
    op = returns[returns.status == "open"]
    out: dict[str, list] = {}
    for tkr, row in op.iterrows():
        ind = ""
        if meta is not None and tkr in meta.index:
            ind = str(meta.loc[tkr, "industry"] or meta.loc[tkr, "sector"] or "").strip()
        if not ind:
            continue
        v = _bb_num(row.get("total_pct"))
        if v == v:
            out.setdefault(ind, []).append(v)
    return {k: sum(vs) / len(vs) for k, vs in out.items() if vs}


def _bb_build_universe_observations(shortlist, quant_metrics, signals, analyst,
                                    ticker_news, sector_avg, outlook):
    """Stack-score deepened universe shortlist names -> idea observation dicts."""
    now = pd.Timestamp.now(tz="UTC")
    out = []
    for tkr in shortlist:
        q = quant_metrics.loc[tkr] if (quant_metrics is not None
                                       and tkr in quant_metrics.index) else None
        sig = (signals.loc[tkr, "signal"] if (signals is not None
               and tkr in signals.index) else "")
        an = analyst.loc[tkr] if (analyst is not None
                                  and tkr in analyst.index) else None
        items = []
        if ticker_news is not None and tkr in ticker_news.index:
            raw = ticker_news.loc[tkr, "items_json"]
            try:
                items = json.loads(raw) if isinstance(raw, str) and raw else []
            except (TypeError, ValueError):
                items = []
        # relational gap vs the holder's sector
        gap = None
        sector_name = ""
        if outlook is not None and tkr in outlook.index:
            sector_name = str(outlook.loc[tkr].get("industry") or "").strip()
            ret = _bb_num(outlook.loc[tkr].get("ret_12m"))
            if sector_name in sector_avg and ret == ret:
                gap = ret - sector_avg[sector_name]
        row = pd.Series({"status": "universe", "latest": float("nan")})
        flags = _bb_flags_for(
            tkr, row, q, sig, an, [], items, weight_rank=None,
            is_top_contrib=False, is_bottom_contrib=False, now=now,
            beats_sector_gap=gap, sector_name=sector_name)
        if not flags:
            continue
        score, raw, _ = _bb_score(flags)
        archetype = _bb_match_archetype({f["id"] for f in flags})
        tag, body = _bb_narrative(tkr, flags, archetype, score=score)
        # Native price for the card (v2.5 #2). The universe is the S&P 500
        # (universe.csv), so current_price is USD.
        _px = _bb_num(outlook.loc[tkr].get("current_price")) \
            if (outlook is not None and tkr in outlook.index) else float("nan")
        out.append({
            "ticker": tkr, "ownership": "idea", "severity": _bb_severity(flags),
            # Neutral fallback tag: only an archetype (e.g. missing_idea from a
            # beats_your_sector match) earns the "Setup you're missing" claim.
            "title": tag if archetype else "On the radar", "body": body,
            "pills": [f["pill"] for f in sorted(flags, key=lambda f: -f["weight"])],
            "cite": _bb_news_cite(items, now),
            "price": (_px if _px == _px else None), "price_ccy": "$",
            "score": score, "raw": raw,
        })
    out.sort(key=lambda c: (-c["score"], -c["raw"], c["ticker"]))
    return out


def _bb_news_cite(news_items: list[dict], now: pd.Timestamp) -> dict | None:
    """Most-recent news item within _BB_NEWS_CITE_DAYS that has a title + link.
    Returns {title, link, publisher} or None."""
    best = None
    best_ts = None
    for it in news_items or []:
        title = str(it.get("title") or "").strip()
        link = str(it.get("link") or "").strip()
        if not title or not link:
            continue
        ts = pd.to_datetime(it.get("published"), utc=True, errors="coerce")
        if ts is None or ts != ts:
            continue
        if (now - ts).days > _BB_NEWS_CITE_DAYS:
            continue
        if best_ts is None or ts > best_ts:
            best, best_ts = it, ts
    if best is None:
        return None
    return {"title": str(best["title"]).strip(),
            "link": str(best["link"]).strip(),
            "publisher": str(best.get("publisher") or "").strip()}


def _bb_universe_shortlist(universe_outlook: "pd.DataFrame | None",
                           exclude: set[str], n: int = 40) -> list[str]:
    """Pre-rank the universe by a cheap 'can't-miss' composite using cached
    outlook fields only, excluding held/sold names. Returns top-n tickers."""
    if universe_outlook is None or universe_outlook.empty:
        return []
    df = universe_outlook[~universe_outlook.index.isin(exclude)].copy()
    if df.empty:
        return []

    def _norm(s):
        s = pd.to_numeric(s, errors="coerce")
        lo, hi = s.min(), s.max()
        if pd.isna(lo) or hi == lo:
            return s.fillna(0) * 0
        return ((s - lo) / (hi - lo)).fillna(0)

    upside = _norm(df.get("upside"))
    momentum = _norm(df.get("ret_12m"))
    coverage = _norm(df.get("num_analysts"))
    rec = df.get("recommendation").fillna("").str.lower() if "recommendation" in df else ""
    rec_bonus = rec.isin(["buy", "strong_buy"]).astype(float) * 0.2 if len(rec) else 0
    score = upside * (0.6 + 0.4 * coverage) + 0.6 * momentum + rec_bonus
    return list(score.sort_values(ascending=False).head(n).index)


_BB_THIN_SCORE = 3.0    # below this, a read rests on one weak/lonely signal
_BB_THIN_HEDGE = "Early read on a thin signal."


def _bb_narrative(tkr: str, flags: list[dict], archetype: str | None,
                  score: "float | None" = None) -> tuple[str, str]:
    """Return (title_tag, body). Archetype -> bespoke punchy sentence; else a
    fallback that lists the top fragments. Confident, specific, never advice.
    When ``score`` is below _BB_THIN_SCORE the body gets an honest hedge so a
    one-signal read doesn't sound as certain as a multi-domain stack (M-BB)."""
    by_id = {f["id"]: f for f in flags}

    def frag(fid, default=""):
        return by_id[fid]["frag"] if fid in by_id else default

    if archetype == "exhausted_winner":
        body = (f"{tkr} is {frag('overbought', 'overbought')} "
                f"{frag('near_high', 'near its highs')}. "
                f"Momentum trains don't run forever.")
    elif archetype == "heavy_bleeder":
        street = (f" and {frag('downgrade')}" if "downgrade" in by_id else "")
        body = (f"{tkr} is {frag('top_weight', 'a big position')} "
                f"{frag('downtrend', 'heading the wrong way')}{street}. "
                f"Thesis check, not hope.")
    elif archetype == "divergence":
        body = (f"{tkr}: the tape and the Street disagree. "
                f"One of them is early.")
    elif archetype == "crowded_strength":
        body = (f"{tkr} is {frag('big_contributor', 'carrying the basket')} and "
                f"{frag('extended', 'stretched above its 200-day')}. "
                f"Great until it reverts.")
    elif archetype == "quiet_breakout":
        body = (f"{tkr} is breaking out — {frag('unusual_volume', 'heavy volume')} "
                f"with the Street still onside. The market's noticing.")
    elif archetype == "capitulation":
        body = (f"{tkr} is {frag('oversold', 'washed out')}, "
                f"{frag('near_low', 'near its lows')}. "
                f"Value entry or falling knife.")
    elif archetype == "catalyst":
        body = (f"{tkr} {frag('big_move_1w', 'moved hard')} on "
                f"{frag('unusual_volume', 'heavy volume')} — "
                f"and the news backs it up.")
    elif archetype == "ran_without_you":
        body = (f"You sold {tkr} — {frag('post_exit', 'it moved after you left')}. "
                f"Worth a look at whether the exit was early.")
    elif archetype == "missing_idea":
        body = (f"You don't own {tkr}, but it's {frag('beats_your_sector', 'outpacing your sector')} "
                f"and {frag('unusual_volume', frag('overbought', 'stacking signals'))}. "
                f"The one you're missing.")
    else:
        top = sorted(flags, key=lambda f: -f["weight"])[:3]
        frags = [f["frag"] for f in top if f.get("frag")]
        joined = "; ".join(frags) if frags else "multiple signals"
        n = len(flags)
        body = f"{tkr}: {n} signal{'s' if n != 1 else ''} stacking — {joined}."

    if score is not None and score < _BB_THIN_SCORE:
        body = f"{body} {_BB_THIN_HEDGE}"
    tag = _BB_TITLE_TAGS.get(archetype or "fallback", _BB_TITLE_TAGS["fallback"])
    return tag, body


def _bb_owned_price(base_px, ccy: str, fx) -> "tuple[float | None, str]":
    """Owned-position price in its NATIVE currency, reconstructed from the
    base-currency price and the latest FX rate (so GBp/pence never surface).
    Falls back to base currency when no rate is available. (v2.5 #2b)"""
    if base_px is None or base_px != base_px:
        return None, BASE_SYMBOL
    if ccy == BASE_CCY:
        return float(base_px), BASE_SYMBOL
    key = f"{ccy}{BASE_CCY}=X"
    if fx is not None and hasattr(fx, "columns") and key in fx.columns:
        rate = fx[key].dropna()
        if len(rate) and float(rate.iloc[-1]) > 0:
            return float(base_px) / float(rate.iloc[-1]), CCY_SYMBOLS.get(ccy, ccy + " ")
    return float(base_px), BASE_SYMBOL


def compute_bigbrain_observations(
    returns: pd.DataFrame,
    meta: pd.DataFrame,
    contrib: "pd.DataFrame | None" = None,
    quant_metrics: "pd.DataFrame | None" = None,
    signals: "pd.DataFrame | None" = None,
    analyst: "pd.DataFrame | None" = None,
    rating_moves: "list[dict] | None" = None,
    ticker_news: "pd.DataFrame | None" = None,
    universe_observations: "list[dict] | None" = None,
    fx: "pd.DataFrame | None" = None,
) -> list[dict]:
    """Discovery board engine. Two lanes: universe ideas (passed in, Phase 2)
    and portfolio (open + sold). Returns up to 4: 2 per lane, backfilled."""
    if returns is None or returns.empty:
        portfolio: list[dict] = []
    else:
        now = pd.Timestamp.now(tz="UTC")
        pool = returns[returns.status.isin(["open", "closed"])]
        open_pos = returns[returns.status == "open"]
        weight_rank = open_pos["weight"].rank(ascending=False, method="min") \
            if not open_pos.empty else pd.Series(dtype=float)
        top_contrib: set[str] = set()
        bottom_contrib: set[str] = set()
        if contrib is not None and not contrib.empty and "contribution_pp" in contrib:
            c_open = contrib[contrib.index.isin(open_pos.index)]
            top_contrib = set(c_open["contribution_pp"].nlargest(3).index)
            bottom_contrib = set(c_open["contribution_pp"].nsmallest(3).index)
        moves_by_tkr: dict[str, list] = {}
        for m in (rating_moves or []):
            moves_by_tkr.setdefault(m["ticker"], []).append(m)

        def news_items(tkr):
            if ticker_news is None or tkr not in ticker_news.index:
                return []
            raw = ticker_news.loc[tkr, "items_json"]
            try:
                return json.loads(raw) if isinstance(raw, str) and raw else []
            except (TypeError, ValueError):
                return []

        portfolio = []
        for tkr, row in pool.iterrows():
            q = quant_metrics.loc[tkr] if (quant_metrics is not None
                                           and tkr in quant_metrics.index) else None
            sig = (signals.loc[tkr, "signal"] if (signals is not None
                   and tkr in signals.index) else "")
            an = analyst.loc[tkr] if (analyst is not None
                                      and tkr in analyst.index) else None
            items = news_items(tkr)
            wr = int(weight_rank.get(tkr, 0)) or None
            flags = _bb_flags_for(
                tkr, row, q, sig, an, moves_by_tkr.get(tkr, []), items,
                weight_rank=wr, is_top_contrib=tkr in top_contrib,
                is_bottom_contrib=tkr in bottom_contrib, now=now)
            if not flags:
                continue
            score, raw, _ = _bb_score(flags)
            archetype = _bb_match_archetype({f["id"] for f in flags})
            tag, body = _bb_narrative(tkr, flags, archetype, score=score)
            # v2.5 #2b: show the holding in its NATIVE currency (Engie -> EUR,
            # US names -> USD) so the whole section is local-currency consistent
            # with the universe ideas. Reconstructed from base price / FX rate.
            _ccy = ticker_currency(meta, tkr)
            _px, _px_ccy = _bb_owned_price(_bb_num(row.get("latest")), _ccy, fx)
            portfolio.append({
                "ticker": tkr,
                "ownership": "sold" if row.get("status") == "closed" else "held",
                "severity": _bb_severity(flags),
                "title": tag, "body": body,
                "pills": [f["pill"] for f in sorted(flags, key=lambda f: -f["weight"])],
                "cite": _bb_news_cite(items, now),
                "price": _px, "price_ccy": _px_ccy,
                "score": score, "raw": raw,
            })
        portfolio.sort(key=lambda c: (-c["score"], -c["raw"], c["ticker"]))

    universe = list(universe_observations or [])
    universe.sort(key=lambda c: (-c["score"], -c.get("raw", 0), c["ticker"]))
    # v2.5 #9: up to 8 cards (4 idea + 4 owned), rendered 2:2 per couple page.
    return _bb_merge_lanes(universe, portfolio, n=8, per_lane=4)


def _bb_merge_lanes(universe: list[dict], portfolio: list[dict],
                    n: int = 4, per_lane: int = 2) -> list[dict]:
    """Take per_lane from each, then backfill remaining slots from whichever
    lane still has candidates. Universe first (top row of the 2x2)."""
    picked = universe[:per_lane] + portfolio[:per_lane]
    if len(picked) < n:
        rest = universe[per_lane:] + portfolio[per_lane:]
        rest.sort(key=lambda c: (-c["score"], c["ticker"]))
        picked += rest[: n - len(picked)]
    return picked[:n]


def log_bigbrain_flags(observations: list[dict], date_str: str,
                       prices: dict, path=None) -> None:
    """Append today's surfaced Big Brain cards to the flag log (real build
    only). De-duped per (date, ticker). Columns: date, ticker, ownership,
    price, label."""
    path = path or BIGBRAIN_LOG_CSV
    if not observations:
        return
    cols = ["date", "ticker", "ownership", "price", "label"]
    try:
        existing = pd.read_csv(path, dtype=str) if path.exists() else pd.DataFrame(columns=cols)
    except Exception:
        existing = pd.DataFrame(columns=cols)
    seen = set(zip(existing.get("date", []), existing.get("ticker", [])))
    new_rows = []
    for o in observations:
        tkr = o.get("ticker")
        if not tkr or (date_str, tkr) in seen:
            continue
        pr = prices.get(tkr)
        new_rows.append({"date": date_str, "ticker": tkr,
                         "ownership": o.get("ownership", ""),
                         "price": "" if pr is None else f"{float(pr):.4f}",
                         "label": o.get("title", "")})
    if not new_rows:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        pd.concat([existing, pd.DataFrame(new_rows)], ignore_index=True).to_csv(path, index=False)
    except Exception as e:
        print(f"WARN couldn't write bigbrain log: {e}", file=sys.stderr)


def _humanize_age(days: int) -> str:
    if days >= 60:
        return f"{days // 30} months"
    if days >= 14:
        return f"{days // 7} weeks"
    if days >= 7:
        return "1 week"
    if days <= 1:
        return "yesterday" if days == 1 else "today"
    return f"{days} days"


def compute_bigbrain_memory(log_df, current_prices: dict, today_str: str) -> dict:
    """For names flagged today that ALSO have an earlier log entry, return
    {ticker: 'flagged <age> ago - <+/-%> since'} using current price vs the
    OLDEST prior flag's price. Skips when no current/prior price."""
    if log_df is None or len(log_df) == 0:
        return {}
    df = log_df.copy()
    df["_d"] = pd.to_datetime(df["date"], errors="coerce")
    today = pd.to_datetime(today_str, errors="coerce")
    out = {}
    for tkr, g in df.groupby("ticker"):
        if not (g["date"] == today_str).any():
            continue                      # not flagged today
        prior = g[g["_d"] < today].sort_values("_d")
        if prior.empty:
            continue
        oldest = prior.iloc[0]
        prior_price = _bb_num(oldest["price"])
        cur = current_prices.get(tkr)
        cur = float(cur) if cur is not None else float("nan")
        if prior_price != prior_price or prior_price <= 0 or cur != cur:
            continue
        pct = (cur / prior_price - 1) * 100
        age = _humanize_age(int((today - oldest["_d"]).days))
        out[tkr] = f"flagged {age} ago &mdash; {pct:+.0f}% since"
    return out


def _quadrant_signal_score(q, signal_label, row):
    """(strength 0..100, direction +/-1) for one holding, from RSI distance,
    trend label, 200-DMA side, and momentum. NaN-safe.

    M-SIG: strength and direction are derived from ONE signed composite (each
    component carries its own sign), so they can't disagree — a name can't read
    'bullish' on the axis while its dominant inputs are falling. strength is the
    composite's magnitude; direction its sign. Conflicting drivers cancel toward
    the neutral middle (low strength) instead of piling into a confident extreme."""
    sig = str(signal_label or "").strip().lower()
    bull = any(w in sig for w in ("uptrend", "near 12m high", "trending up", "bouncing"))
    bear = any(w in sig for w in ("downtrend", "near 12m low", "trending down", "pullback"))
    rsi = _bb_num(q.get("rsi14")) if q is not None else float("nan")
    sma = _bb_num(q.get("sma200_dist_pct")) if q is not None else float("nan")
    m1 = _bb_num(row.get("1m_pct")) if row is not None else float("nan")
    # Signed components, each in [-1, 1] (bullish positive, bearish negative).
    c_rsi = max(-1.0, min(1.0, (rsi - 50) / 50)) if rsi == rsi else 0.0
    c_trend = (1.0 if bull else 0.0) - (1.0 if bear else 0.0)
    c_mom = max(-1.0, min(1.0, m1 / 15)) if m1 == m1 else 0.0
    c_sma = max(-1.0, min(1.0, sma / 50)) if sma == sma else 0.0
    composite = 0.35 * c_rsi + 0.25 * c_trend + 0.2 * c_mom + 0.2 * c_sma  # [-1, 1]
    strength = abs(composite) * 100
    direction = 1 if composite >= 0 else -1
    return strength, direction


def build_signal_strip_data(returns, quant_metrics, signals) -> list[dict]:
    """Per OPEN position: {ticker, signal (-100..+100 bearish->bullish), ret (%)}.

    v2.4: equal weight removed the old size/conviction axis, so this feeds a
    one-dimensional beeswarm — each open name placed along a bearish<->bullish
    technical-signal axis, coloured by its return. `signal` = direction x
    strength from `_quadrant_signal_score`; `ret` is total return to date."""
    if returns is None or returns.empty:
        return []
    op = returns[returns.status == "open"]
    if op.empty:
        return []
    out = []
    for tkr, row in op.iterrows():
        q = quant_metrics.loc[tkr] if (quant_metrics is not None
                                       and tkr in quant_metrics.index) else None
        sig = (signals.loc[tkr, "signal"] if (signals is not None
               and tkr in signals.index) else "")
        strength, direction = _quadrant_signal_score(q, sig, row)
        ret = _bb_num(row.get("total_pct"))
        out.append({"ticker": tkr,
                    "signal": float(direction) * float(min(100.0, strength)),
                    "ret": float(ret) if ret == ret else 0.0})
    return out


def _bb_deepen_universe(shortlist: list[str], meta: pd.DataFrame,
                        fx: pd.DataFrame):
    """Fetch OHLCV + news and compute quant/signals for the universe shortlist.
    Returns (quant_metrics, signals, ticker_news, ohlcv). Best-effort: failures
    just shrink the idea pool, never break the build."""
    if not shortlist:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    try:
        ohlcv, _failed, _r = download_ohlcv(shortlist)
    except Exception as e:
        print(f"WARN bb universe OHLCV failed: {e}", file=sys.stderr)
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    if ohlcv.empty:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    try:
        ohlcv.to_parquet(BB_UNIVERSE_OHLCV_CACHE)
    except Exception:
        pass
    prices_close = ohlcv.xs("Close", axis=1, level=1).copy()
    meta2, _ = fetch_meta(list(prices_close.columns), meta)
    quant = compute_quant_metrics(ohlcv, fx, meta2)
    signals = compute_signals(prices_close)
    news, _ = fetch_ticker_news(list(prices_close.columns), load_ticker_news_cache())
    return quant, signals, news, ohlcv


_BB_NONEQUITY_HINTS = ("bond", "treasury", "gilt", "gold", "silver", "commodity",
                       "money market", "cash")


def _basket_equity_share(returns: pd.DataFrame, meta: pd.DataFrame) -> float | None:
    """Best-effort share of OPEN positions in equities (not bond/commodity/cash
    ETFs), classified by sector/industry keywords. Equal-weight (each open name
    counts once). None if unknowable."""
    if returns is None or returns.empty:
        return None
    op = returns[returns.status == "open"]
    if op.empty:
        return None
    total, equity = 0.0, 0.0
    for tkr, row in op.iterrows():
        w = _pred_num(row.get("weight"))
        if w != w or w <= 0:
            continue
        total += w
        label = ""
        if meta is not None and tkr in meta.index:
            label = (str(meta.loc[tkr, "industry"] or "") + " " +
                     str(meta.loc[tkr, "sector"] or "")).lower()
        if not any(h in label for h in _BB_NONEQUITY_HINTS):
            equity += w
    return (equity / total) if total > 0 else None


def render_bigbrain_macro(top_move: dict | None, equity_share: float | None) -> str:
    """Pinned macro callout for the Big Brain section head. Empty unless the
    sharpest tracked move clears BB_MACRO_DELTA_PP."""
    if not top_move:
        return ""
    d = top_move.get("delta_pp")
    if d is None or abs(d) < BB_MACRO_DELTA_PP:
        return ""
    arrow = "jumped" if d >= 0 else "fell"
    sign = "+" if d >= 0 else "&minus;"
    if equity_share is not None:
        exposure = f" Your basket is ~{equity_share * 100:.0f}% equities &mdash;"
    else:
        exposure = " For an equity-heavy basket like yours,"
    return (
        '<div class="bb-macro">'
        '<span class="bb-macro-tag">&#127760; Macro</span>'
        f'<span class="bb-macro-body">&ldquo;{_esc(top_move["theme"])}&rdquo; '
        f'{arrow} {sign}{abs(d):.0f}pp to {top_move["probability"]:.0f}% this week.'
        f'{exposure} worth knowing what the crowd is pricing.</span>'
        '</div>'
    )


_BB_OWN_BADGE = {"held": "held", "sold": "sold", "idea": "not owned"}


def render_bigbrain(observations: list[dict], as_of_str: str, macro_html: str = "",
                    memory: dict | None = None) -> str:
    """v2 discovery board: colour-coded cards (idea / held / sold). Up to 8
    cards (4 idea + 4 owned) shown 2:2 per "couple", with arrows to flip
    between couples (v2.5 #9)."""
    n = len(observations)
    head = (
        '<div class="bb-head">'
        '<h3>Big Brain says</h3>'
        f'<span class="bb-sub">{n} name{"s" if n != 1 else ""} punching above the noise this week</span>'
        f'<span class="bb-asof">as of {as_of_str}</span>'
        '</div>'
    )
    if not observations:
        body = ('<p class="bb-empty">Quiet week &mdash; nothing notable '
                'stacking across your basket or the market right now.</p>')
        return f'<section class="bigbrain-section">{head}{macro_html}{body}</section>'

    def render_card(o: dict) -> str:
        own = o.get("ownership", "held")
        tier = "idea" if own == "idea" else ("good" if own == "sold" else o["severity"])
        badge = _BB_OWN_BADGE.get(own, own)
        pills = "".join(f'<span class="bb-pill">{_esc(p)}</span>'
                        for p in o.get("pills", []))
        mem = (memory or {}).get(o["ticker"])
        mem_html = f'<div class="bb-memory">&#8617; {mem}</div>' if mem else ""
        # idea (universe) tickers have no modal payload -> not clickable
        if own == "idea":
            tkr_html = f'<span class="bb-ticker">{_esc(o["ticker"])}</span>'
        else:
            tkr_html = (f'<span class="bb-ticker ticker-clickable" '
                        f'data-ticker="{_esc(o["ticker"])}">{_esc(o["ticker"])}</span>')
        cite_html = ""
        c = o.get("cite")
        if c:
            pub = f' &mdash; {_esc(c["publisher"])}' if c.get("publisher") else ""
            title = _esc(c["title"][:80] + ("…" if len(c["title"]) > 80 else ""))
            cite_html = (f'<a class="bb-cite" href="{safe_url(c["link"])}" target="_blank" '
                         f'rel="noopener noreferrer">&#8599; {title}{pub}</a>')
        # v2.5 #2: current price next to the ticker (base ccy for held/sold,
        # native for universe ideas). Omitted when unavailable.
        price = o.get("price")
        price_html = (f'<span class="bb-price">{o.get("price_ccy", "")}{price:,.2f}</span>'
                      if price is not None and price == price else "")
        return (
            f'<div class="bb-card bb-tier-{tier}">'
            f'<div class="bb-card-hd">{tkr_html}{price_html}'
            f'<span class="bb-badge">{_esc(badge)}</span>'
            f'<span class="bb-tag">{_esc(o["title"])}</span></div>'
            f'<div class="bb-card-bd">'
            f'<div class="bb-body">{o["body"]}</div>'
            f'<div class="bb-pills">{pills}</div>'
            f'{mem_html}{cite_html}</div></div>'
        )

    # v2.6 #1: with a full board (>4 cards), page 1 = the names you DON'T own
    # (ideas), page 2 = the names you DO own — each flip is a clean
    # "discovery vs portfolio" switch. With <=4 cards there's nothing to flip.
    ideas = [o for o in observations if o.get("ownership") == "idea"]
    owned = [o for o in observations if o.get("ownership") != "idea"]
    if len(observations) <= 4:
        pages: list[list[dict]] = [observations]
    elif ideas and owned:
        pages = [ideas, owned]
    else:                                   # all one type -> chunk into pages of 4
        pages = [observations[i:i + 4] for i in range(0, len(observations), 4)]

    if len(pages) == 1:
        grid = f'<div class="bb-grid">{"".join(render_card(o) for o in pages[0])}</div>'
        return f'<section class="bigbrain-section">{head}{macro_html}{grid}</section>'

    page_divs = []
    for pi, page in enumerate(pages):
        active = " active" if pi == 0 else ""
        page_divs.append(
            f'<div class="bb-page{active}" data-page="{pi}">'
            f'<div class="bb-grid">{"".join(render_card(o) for o in page)}</div></div>'
        )
    nav = (
        '<div class="bb-nav">'
        '<button type="button" class="bb-arrow bb-prev" aria-label="Previous set">&#8249;</button>'
        f'<span class="bb-page-ind"><span class="bb-page-cur">1</span>&#8202;/&#8202;{len(pages)}</span>'
        '<button type="button" class="bb-arrow bb-next" aria-label="Next set">&#8250;</button>'
        '</div>'
    )
    return (f'<section class="bigbrain-section" data-bb-pages="{len(pages)}">{head}'
            f'{macro_html}{nav}<div class="bb-pages">{"".join(page_divs)}</div></section>')


def render_market_expectations(rows: list[dict], as_of_str: str) -> str:
    """Prediction-market sentiment: probability bars + since-last-build deltas,
    sorted by |delta| desc. Source-attributed per row. Shows PRED_WINDOW themes
    at a time with a "Reshuffle" button that cycles through the rest without
    repeats until exhausted (v2.5 #3)."""
    if not rows:
        head = (
            '<div class="me-head"><h3>Market expectations</h3>'
            '<span class="me-sub">what the crowd is pricing '
            f'&middot; as of {as_of_str}</span></div>'
        )
        return ('<section class="market-expectations-section">' + head +
                '<p class="me-empty">Tracking begins next build &mdash; first '
                'snapshot of the prediction markets.</p></section>')
    reshuffle = ('<button type="button" class="me-reshuffle" '
                 'aria-label="Show other themes">&#8635; Reshuffle</button>'
                 if len(rows) > PRED_WINDOW else '')
    head = (
        '<div class="me-head"><h3>Market expectations</h3>'
        f'{reshuffle}'
        '<span class="me-sub">what the crowd is pricing '
        f'&middot; as of {as_of_str}</span></div>'
    )
    legend = ('<p class="me-legend"><b>%</b> = market-implied probability '
              '&middot; the bar mirrors it &middot; '
              '<b>&#9650;/&#9660; pp</b> = change since the last build</p>')
    ordered = sorted(rows, key=lambda r: -abs(r.get("delta_pp") or 0))
    items = []
    for r in ordered:
        prob = r["probability"]
        d = r.get("delta_pp")
        if d is None:
            delta_html = '<span class="me-delta me-flat">&middot;</span>'   # no prior build
        elif abs(d) < 0.5:
            delta_html = '<span class="me-delta me-flat">0pp</span>'        # tracked, unchanged
        else:
            cls = "me-up" if d > 0 else "me-down"
            arrow = "&#9650;" if d > 0 else "&#9660;"
            delta_html = f'<span class="me-delta {cls}">{arrow} {abs(d):.0f}pp</span>'
        theme_html = (f'<a class="me-q" href="{safe_url(r["url"])}" target="_blank" '
                      f'rel="noopener noreferrer">{_esc(r["theme"])}</a>'
                      if r.get("url") else
                      f'<span class="me-q">{_esc(r["theme"])}</span>')
        # The specific market question gives the % meaning ("Fed rate decision"
        # alone is ambiguous; "...above 4.25% after Jun meeting? — 0%" is not).
        q = (r.get("question") or "").strip()
        if len(q) > 78:
            q = q[:77].rstrip() + "…"
        qsub = f'<span class="me-qsub">{_esc(q)}</span>' if q else ""
        items.append(
            f'<div class="me-row"><div class="me-q-wrap">{theme_html}{qsub}</div>'
            f'<span class="me-prob">{prob:.0f}%</span>{delta_html}'
            f'<span class="me-bar"><i style="width:{max(0, min(100, prob)):.0f}%"></i></span>'
            f'<span class="me-src">{_esc(r["source"])}</span></div>'
        )
    return ('<section class="market-expectations-section">' + head + legend +
            f'<div class="me-list" data-me-window="{PRED_WINDOW}" '
            f'data-me-total="{len(items)}">' + "".join(items) + '</div></section>')


def render_signal_strip(data: list[dict]) -> str:
    """Beeswarm: every open position placed along a bearish<->bullish technical
    signal axis, jittered vertically so none overlap, coloured by return (green
    up / red down). Dots are clickable + carry a hover tooltip. The extremes get
    text labels. Renders nothing for <2 positions."""
    if not data or len(data) < 2:
        return ""
    W, H = 920, 300
    PX0, PX1, midY, R, GAP = 60, 880, 150, 7.0, 2.0
    step = 2 * R + GAP
    max_off = H / 2 - R - 26          # keep the swarm inside the band

    def x_of(s):                      # signal -100..+100 -> px
        return PX0 + (max(-100.0, min(100.0, s)) + 100) / 200 * (PX1 - PX0)

    # Greedy beeswarm: sort by signal, then place each dot at the y closest to
    # the centre line that doesn't collide with an already-placed neighbour.
    placed = []                       # (x, y, d)
    for d in sorted(data, key=lambda d: d["signal"]):
        x = x_of(d["signal"])
        y = midY
        for k in range(0, 80):
            done = False
            for off in ((0,) if k == 0 else (k * step, -k * step)):
                cand = midY + off
                if abs(off) > max_off:
                    continue
                if not any(abs(px - x) < step and abs(py - cand) < step
                           for px, py, _ in placed):
                    y, done = cand, True
                    break
            if done:
                break
        placed.append((x, y, d))

    # Label only the strongest few on each end so the strip stays clean.
    label_tkrs = {d["ticker"] for d in sorted(data, key=lambda d: -abs(d["signal"]))[:6]}
    dots = []
    for x, y, d in placed:
        cls = "dot-up" if d["ret"] >= 0 else "dot-down"
        tk = _esc(d["ticker"])
        tone = "bullish" if d["signal"] >= 0 else "bearish"
        tip = f'{d["ticker"]} · {tone} signal · {d["ret"]:+.1f}%'
        dots.append(
            f'<circle class="q-dot {cls} ticker-clickable" data-ticker="{tk}" '
            f'cx="{x:.1f}" cy="{y:.1f}" r="{R:.0f}"><title>{_esc(tip)}</title></circle>'
        )
        if d["ticker"] in label_tkrs:
            anchor = "start" if d["signal"] >= 0 else "end"
            dx = (R + 3) if d["signal"] >= 0 else -(R + 3)
            dots.append(f'<text class="q-tkr" x="{x + dx:.1f}" y="{y + 3:.1f}" '
                        f'text-anchor="{anchor}">{tk}</text>')
    midX = (PX0 + PX1) / 2
    svg = (
        f'<svg viewBox="0 0 {W} {H}" width="100%">'
        f'<line x1="{PX0}" y1="{midY}" x2="{PX1}" y2="{midY}" stroke="var(--border)"/>'
        f'<line x1="{midX:.0f}" y1="42" x2="{midX:.0f}" y2="{H - 42}" stroke="var(--border)" stroke-dasharray="3,4"/>'
        f'<text class="q-q" x="{PX0}" y="{H - 12}">&#9664; bearish tape</text>'
        f'<text class="q-q" x="{midX:.0f}" y="{H - 12}" text-anchor="middle">neutral</text>'
        f'<text class="q-q" x="{PX1}" y="{H - 12}" text-anchor="end">bullish tape &#9654;</text>'
        + "".join(dots) + '</svg>'
    )
    return (
        '<section class="quadrant-section">'
        '<div class="q-head"><h3>Signal map</h3>'
        '<span class="q-sub">each open position by technical signal &middot; '
        'colour = return (green up / red down) &middot; hover or tap a dot</span></div>'
        + svg + '</section>'
    )


def render_basket_diversification(data: dict | None, meta: pd.DataFrame) -> str:
    """Three-panel + histogram view of pairwise correlation across open positions.

    Headline = mean of every pair's daily-return correlation; two side panels
    list the three most-correlated pairs (concentration risk) and the three
    most-independent positions (best diversifiers). A small bar chart shows
    the full distribution.
    """
    if not data:
        return ""
    avg = data["avg_corr"]
    n_pos = int(data.get("n_positions", 0))
    # Color the headline: low avg = well diversified (green), high = concentrated (red).
    if avg < 0.30:
        avg_cls, avg_meta = "pos", "well diversified"
    elif avg > 0.60:
        avg_cls, avg_meta = "neg", "highly correlated"
    else:
        avg_cls, avg_meta = "", "moderately diversified"
    # Count-aware honesty: a low average across only a handful of names is not
    # real diversification. Flag the small sample instead of asserting it.
    if n_pos and n_pos < 5 and avg < 0.30:
        avg_meta = f"low avg correlation &mdash; but only {n_pos} names"

    def _ind(t: str) -> str:
        return _industry_label(meta, t)

    # T13: each ticker symbol in the diversification panel gets the
    # `ticker-clickable` class -> opens its detail modal via the generic
    # handler we wired in Batch 2. Pair rows expose both tickers as
    # separately-clickable spans.
    most_rows = []
    for p in data["most_correlated"]:
        sec_a, sec_b = _ind(p["a"]), _ind(p["b"])
        sub = sec_a if sec_a == sec_b else f"{sec_a} / {sec_b}"
        most_rows.append(
            f'<li class="div-row">'
            f'<div class="div-pair">'
            f'<span class="div-pair-syms">'
            f'<span class="ticker-clickable" data-ticker="{_esc(p["a"])}">{_esc(p["a"])}</span>'
            f' &harr; '
            f'<span class="ticker-clickable" data-ticker="{_esc(p["b"])}">{_esc(p["b"])}</span>'
            f'</span>'
            f'<div class="div-pair-sub">{_esc(sub)}</div>'
            f'</div>'
            f'<span class="div-val div-val-hot">{p["corr"]:.2f}</span>'
            f'</li>'
        )

    div_rows = []
    for d in data["best_diversifiers"]:
        sec = _ind(d["ticker"])
        name = str(meta.loc[d["ticker"], "name"]) if d["ticker"] in meta.index else d["ticker"]
        sub = f"{name} &middot; {_esc(sec)}" if sec else _esc(name)
        div_rows.append(
            f'<li class="div-row">'
            f'<div class="div-pair">'
            f'<span class="div-pair-syms ticker-clickable" data-ticker="{_esc(d["ticker"])}">{_esc(d["ticker"])}</span>'
            f'<div class="div-pair-sub">{sub}</div>'
            f'</div>'
            f'<span class="div-val div-val-cool">{d["avg_corr"]:+.2f}</span>'
            f'</li>'
        )

    # Histogram: each bar's height = % of the tallest. Empty buckets get a
    # minimum 2% sliver so the x-axis stays anchored visually.
    # T14: each bar is wrapped in a full-height clickable column so even tiny
    # bars (e.g. one-pair buckets) get a usable click target. The visible bar
    # still scales with count; the parent column captures clicks.
    max_count = max((h["count"] for h in data["histogram"]), default=1) or 1
    bar_html_parts: list[str] = []
    xlabel_html_parts: list[str] = []
    for h in data["histogram"]:
        ratio = h["count"] / max_count
        height_pct = ratio * 100 if h["count"] > 0 else 2
        mid = (h["min"] + h["max"]) / 2
        tooltip = f'{h["min"]:+.2f} to {h["max"]:+.2f}: {h["count"]} pair' + ("s" if h["count"] != 1 else "")
        col_cls = "div-hist-col" + (" div-hist-col-clickable" if h["count"] > 0 else "")
        bar_html_parts.append(
            f'<div class="{col_cls}" '
            f'data-bucket-lo="{h["min"]:.2f}" data-bucket-hi="{h["max"]:.2f}" '
            f'title="{tooltip}">'
            f'<div class="div-hist-bar" style="height:{height_pct:.1f}%"></div>'
            f'</div>'
        )
        xlabel_html_parts.append(f'<span>{mid:+.2f}</span>')

    return f"""<section class="div-section">
  <div class="div-head">
    <h3>Basket diversification <span class="muted">({data["n_positions"]} open positions &middot; 6-month window)</span></h3>
    <p class="muted">How independent are the bets? Lower pairwise correlations
    = more diversification benefit; higher = redundant exposure. Computed on
    {data["n_pairs"]:,} unique pairs of native-currency daily returns.</p>
  </div>
  <div class="div-grid">
    <div class="div-card div-card-headline">
      <div class="div-label">Avg pairwise correlation</div>
      <div class="div-headline {avg_cls}">{avg:+.2f}</div>
      <div class="div-meta">{avg_meta}</div>
    </div>
    <div class="div-card">
      <div class="div-label">Most correlated &mdash; concentration risk</div>
      <ul class="div-list">{''.join(most_rows)}</ul>
    </div>
    <div class="div-card">
      <div class="div-label">Best diversifiers &mdash; lowest avg &rho; vs rest</div>
      <ul class="div-list">{''.join(div_rows)}</ul>
    </div>
  </div>
  <div class="div-histogram">
    <div class="div-hist-label">Distribution of pairwise correlations &mdash; {data["n_pairs"]:,} pairs</div>
    <div class="div-hist-bars">{''.join(bar_html_parts)}</div>
    <div class="div-hist-xlabels">{''.join(xlabel_html_parts)}</div>
  </div>
</section>"""


def build_industry_outlook(universe_outlook: pd.DataFrame | None,
                           log_tickers: set[str] | None = None,
                           min_holdings: int = 3,
                           top_industries: int = 6, top_stocks_per: int = 3) -> list[dict]:
    """Industry leaderboard built from the reference universe ONLY, with any
    ticker the user already holds (open or closed) filtered out so the section
    surfaces genuinely new ideas.

    Aggregates by industry: avg 12-month return, top stocks by analyst price-
    target upside. Requires min_holdings tickers per industry so a single
    name can't dominate the ranking.
    """
    if universe_outlook is None or universe_outlook.empty:
        return []
    skip = log_tickers or set()
    records: list[dict] = []
    for tkr, row in universe_outlook.iterrows():
        if tkr in skip:
            continue
        industry = str(row.get("industry") or "").strip()
        if not industry:
            continue
        ret_12m = row.get("ret_12m")
        if ret_12m is None or pd.isna(ret_12m):
            continue
        up_val = row.get("upside")
        mc_val = row.get("market_cap")
        records.append({
            "ticker": tkr, "industry": industry,
            "ret_12m": float(ret_12m),
            # market cap drives the group's cap-weighted return (v2.5 #1); NaN
            # when missing/non-positive so it drops out of the weighting.
            "market_cap": (float(mc_val) if mc_val is not None and pd.notna(mc_val)
                           and float(mc_val) > 0 else float("nan")),
            "upside": (None if up_val is None or pd.isna(up_val) else float(up_val)),
            "rec": str(row.get("recommendation") or ""),
            "n_an": int(row["num_analysts"]) if pd.notna(row.get("num_analysts")) else 0,
            "cap_tier": str(row.get("cap_tier") or ""),
        })
    if not records:
        return []
    df = pd.DataFrame(records)
    # Aggregate by industry — require min_holdings tickers per industry so a
    # one-stock industry can't dominate the chart.
    groups = []
    for ind, g in df.groupby("industry"):
        if len(g) < min_holdings:
            continue
        # v2.5 #1: market-cap-weighted average 12mo return — a bigger company
        # moves the group number more (standard index-weighting). Falls back to
        # the simple mean if no constituent has a usable market cap.
        mc = g["market_cap"]
        mask = mc > 0
        if mask.any():
            avg_ret = float((g.loc[mask, "ret_12m"] * mc[mask]).sum() / mc[mask].sum())
        else:
            avg_ret = float(g["ret_12m"].mean())
        # Top stocks per industry: prefer those with analyst upside, sort by
        # upside; tiebreak with 12mo return so non-covered names still appear
        # if there's room.
        g_sorted = g.sort_values(
            by=["upside", "ret_12m"], ascending=[False, False],
            na_position="last",
        ).head(top_stocks_per)
        top = [{
            "ticker": r.ticker,
            "ret_12m": float(r.ret_12m),
            "upside": (None if r.upside is None or pd.isna(r.upside) else float(r.upside)),
            "rec": str(r.rec),
            "n_an": int(r.n_an),
            "cap_tier": str(getattr(r, "cap_tier", "") or ""),
        } for r in g_sorted.itertuples(index=False)]
        groups.append({
            "industry": ind,
            "n_holdings": int(len(g)),
            "avg_ret_12m": avg_ret,
            "top_stocks": top,
        })
    groups.sort(key=lambda x: x["avg_ret_12m"], reverse=True)
    return groups[:top_industries]


def render_industry_outlook(groups: list[dict], universe_size: int) -> str:
    if not groups:
        return ""
    cards = []
    for g in groups:
        stock_rows = []
        for s in g["top_stocks"]:
            if s["upside"] is not None and s["rec"]:
                rec_label, rec_cls = _REC_LABELS.get(s["rec"], ("—", "an-rec-none"))
                up_cls = "pos" if s["upside"] >= 0 else "neg"
                meta_html = (f'<span class="an-rec {rec_cls}">{rec_label}</span>'
                             f'<span class="io-up {up_cls}">{s["upside"]:+.0f}% target</span>')
            else:
                meta_html = '<span class="io-no-cov">no analyst coverage</span>'
            # None/NaN guard: the data builder skips null ret_12m, but a stray
            # None here would TypeError on `>=` / format as "nan%".
            _r = s.get("ret_12m")
            _r_ok = _r is not None and _r == _r
            ret_cls = ("pos" if _r >= 0 else "neg") if _r_ok else ""
            ret_txt = f"{_r:+.0f}%" if _r_ok else "&mdash;"
            tier = s.get("cap_tier", "")
            tier_html = f'<span class="io-tier io-tier-{tier.lower()}">{tier}</span>' if tier else ""
            _tkr = _esc(s["ticker"])
            stock_rows.append(
                f'<div class="io-stock" data-ticker="{_tkr}">'
                f'<div class="io-tkr">{_tkr}{tier_html}</div>'
                f'<div class="io-ret {ret_cls}">{ret_txt}</div>'
                f'<div class="io-meta">{meta_html}</div>'
                f'</div>'
            )
        avg_cls = "pos" if g["avg_ret_12m"] >= 0 else "neg"
        # T11: each card is clickable -> opens "all tickers in this industry"
        # info-modal. The header carries a tiny "see all" hint so the
        # interaction is discoverable without cluttering the card body.
        cards.append(
            f'<div class="io-card industry-clickable" data-industry="{_esc(g["industry"])}" '
            f'title="Click to see every tracked ticker in {_esc(g["industry"])}">'
            f'<div class="io-head">'
            f'<div class="io-industry">{_esc(g["industry"])}</div>'
            f'<div class="io-avg {avg_cls}" title="Market-cap-weighted average 12-month return '
            f'(bigger companies count more)">{g["avg_ret_12m"]:+.0f}% avg 12mo</div>'
            f'</div>'
            f'<div class="io-sub muted">{g["n_holdings"]} tracked stocks <span class="io-expand-hint">&rarr; see all</span></div>'
            f'<div class="io-stocks">{"".join(stock_rows)}</div>'
            f'</div>'
        )
    return f"""<section class="industry-section">
  <div class="io-head-row">
    <h3>Industry outlook <span class="muted">({len(groups)})</span></h3>
    <p class="muted">12-mo return leaders from <code>universe.csv</code> ({universe_size} stocks), excluding everything in <code>log.xlsx</code>. Refreshed monthly.</p>
  </div>
  <div class="io-grid">{''.join(cards)}</div>
</section>"""


def build_value_screen(universe_outlook: "pd.DataFrame | None",
                       log_tickers: "set[str] | None" = None,
                       bb_idea_tickers: "set[str] | None" = None,
                       min_pass: "int | None" = None,
                       near_low_pct: "float | None" = None) -> list[dict]:
    """v2.6 Value screen: quality+value names trading near their 52-week low.

    Six filters: #1 near-52w-low is a REQUIRED gate; #2 cheap vs sector (P/E
    below the sector's median P/E); #3 positive FCF; #4 ROE > threshold; #5
    positive revenue growth; #6 debt/equity below threshold. #2-#6 are scored;
    a name needs VALUE_MIN_PASS of 6 to appear. Universe-only: names in
    log_tickers (held/sold) are dropped (discovery-led). Returns the full
    filtered list sorted by (pass_count desc, P/E discount-to-sector desc);
    the renderer caps + paginates. See value-screen-spec.md.
    """
    if universe_outlook is None or len(universe_outlook) == 0:
        return []
    min_pass = VALUE_MIN_PASS if min_pass is None else min_pass
    near_low_pct = VALUE_NEAR_LOW_PCT if near_low_pct is None else near_low_pct
    skip = log_tickers or set()
    bb = bb_idea_tickers or set()
    df = universe_outlook[~universe_outlook.index.isin(skip)]

    def _n(v) -> float:
        try:
            f = float(v)
        except (TypeError, ValueError):
            return float("nan")
        return f if f == f else float("nan")

    # Sector-median P/E (positive P/E only), per sector with enough priced peers.
    sector_med: dict[str, float] = {}
    if "sector" in df.columns and "pe" in df.columns:
        peers = pd.DataFrame({"sector": df["sector"].astype(str),
                              "pe": pd.to_numeric(df["pe"], errors="coerce")})
        peers = peers[peers["pe"] > 0]
        for sec, g in peers.groupby("sector"):
            if len(g) >= VALUE_MIN_SECTOR_N:
                sector_med[sec] = float(g["pe"].median())

    rows: list[dict] = []
    for tkr, r in df.iterrows():
        r52 = _n(r.get("range52w_pct"))
        if not (r52 == r52 and r52 <= near_low_pct):
            continue                                          # #1 gate
        sec = str(r.get("sector") or "")
        pe = _n(r.get("pe"))
        med = sector_med.get(sec)
        f_cheap = bool(pe == pe and pe > 0 and med is not None and pe < med)
        pe_disc = ((med - pe) / med) if (med and pe == pe and pe > 0) else float("nan")
        fcf = _n(r.get("fcf"));         f_fcf = bool(fcf == fcf and fcf > 0)
        roe = _n(r.get("roe"));         f_roe = bool(roe == roe and roe > VALUE_MIN_ROE)
        rev = _n(r.get("rev_growth"));  f_rev = bool(rev == rev and rev > 0)
        de_raw = _n(r.get("debt_to_equity"))
        de = (de_raw / 100.0) if de_raw == de_raw else float("nan")  # yfinance D/E is a %
        f_de = bool(de == de and de < VALUE_MAX_DE)
        passed = {"near_low": True, "cheap": f_cheap, "fcf": f_fcf,
                  "roe": f_roe, "rev": f_rev, "de": f_de}
        pass_count = sum(1 for v in passed.values() if v)
        if pass_count < min_pass:
            continue
        rows.append({
            "ticker": tkr, "name": str(r.get("name") or tkr), "sector": sec,
            "price": _n(r.get("current_price")),
            "pe": pe, "sector_median_pe": med, "pe_discount": pe_disc,
            "pb": _n(r.get("pb")), "roe": roe, "rev_growth": rev,
            "fcf_positive": f_fcf, "debt_to_equity": de, "range52w_pct": r52,
            "pass_count": pass_count, "passed": passed, "is_bb_idea": tkr in bb,
        })
    rows.sort(key=lambda x: (
        -x["pass_count"],
        -(x["pe_discount"] if x["pe_discount"] == x["pe_discount"] else float("-inf")),
    ))
    return rows


def render_value_screen(rows: list[dict], as_of_str: str) -> str:
    """Scorecard table of value-screen survivors. Price + optional BB tag next to
    the ticker; green cells = filter passed; up to VALUE_MAX_ROWS shown in pages
    of VALUE_PAGE with flip arrows. `as_of_str` is the universe cache's refresh
    date (the data is only as fresh as that monthly fetch). See value-screen-spec.md."""
    head_h3 = '<h3>Value screen</h3>'
    if not rows:
        return (f'<section class="value-screen-section"><div class="vs-head">{head_h3}'
                f'<span class="vs-sub">as of {as_of_str}</span></div>'
                '<p class="vs-empty">No S&amp;P 500 names cleared the value filters near a '
                '52-week low this build.</p></section>')

    total = len(rows)
    shown = rows[:VALUE_MAX_ROWS]
    plural = "name" if total == 1 else "names"
    cap_note = f' &middot; showing top {VALUE_MAX_ROWS}' if total > VALUE_MAX_ROWS else ''
    bar = ('passed all 6 filters' if VALUE_MIN_PASS >= 6
           else f'passed &ge;{VALUE_MIN_PASS}/6')
    sub = (f'<span class="vs-sub">{total} {plural} {bar} '
           f'&middot; S&amp;P 500 &middot; as of {as_of_str}{cap_note}</span>')
    legend = ('<p class="vs-legend"><b>Near a 52-week low</b> (required) + cheap vs sector, '
              'positive FCF, ROE&gt;10%, revenue growth, debt/equity&lt;1.5. '
              'Cells shade by strength vs the others shown &mdash; deeper = better; '
              'hover a column heading for what it measures. '
              'New ideas only &mdash; excludes names you already hold.</p>')

    def _num(v, d=1):
        return f'{v:.{d}f}' if (v is not None and v == v) else '&mdash;'

    def _pct(v):
        return f'{v * 100:.0f}%' if (v is not None and v == v) else '&mdash;'

    def _rng(key):
        vals = [r.get(key) for r in shown
                if r.get(key) is not None and r.get(key) == r.get(key)]
        return (min(vals), max(vals)) if vals else (None, None)

    def _heat(val, lo, hi, invert=False, rgb="52,211,153"):
        """Faint background whose opacity scales with where `val` sits in
        [lo,hi] across the shown rows, so a strong value reads deeper than a
        marginal one. invert=True for 'lower is better' columns (P/E, P/B,
        D/E, 52w distance)."""
        if val is None or val != val or lo is None or hi is None or hi <= lo:
            return ""
        t = (val - lo) / (hi - lo)
        if invert:
            t = 1 - t
        t = max(0.0, min(1.0, t))
        return f'background:rgba({rgb},{0.05 + t * 0.35:.2f})'

    pe_lo, pe_hi = _rng("pe");  pb_lo, pb_hi = _rng("pb");  roe_lo, roe_hi = _rng("roe")
    rev_lo, rev_hi = _rng("rev_growth");  de_lo, de_hi = _rng("debt_to_equity")
    w_lo, w_hi = _rng("range52w_pct")

    body = []
    for r in shown:
        bb_tag = '<span class="vs-bb-tag" title="Also a Big Brain idea">BB</span>' if r.get("is_bb_idea") else ''
        row_cls = 'vs-row vs-bb' if r.get("is_bb_idea") else 'vs-row'
        price = r.get("price")
        price_html = f'<span class="vs-price">${price:,.2f}</span>' if (price is not None and price == price) else ''
        fcf_html = ('<span class="vs-pass">+</span>' if r.get("fcf_positive")
                    else '<span class="vs-fail">&minus;</span>')
        pe, pb, roe = r.get("pe"), r.get("pb"), r.get("roe")
        rev, de, w = r.get("rev_growth"), r.get("debt_to_equity"), r.get("range52w_pct")
        pass_cls = "vs-pass" if r["pass_count"] >= 6 else "vs-mid"
        body.append(
            f'<tr class="{row_cls}">'
            f'<td class="vs-tkr"><span class="vs-sym">{_esc(r["ticker"])}</span>{bb_tag}{price_html}</td>'
            f'<td class="vs-sector">{_esc(r["sector"] or "&mdash;")}</td>'
            f'<td class="num vs-cell vs-c-pe" style="{_heat(pe, pe_lo, pe_hi, invert=True)}">{_num(pe)}</td>'
            f'<td class="num vs-cell vs-c-pb" style="{_heat(pb, pb_lo, pb_hi, invert=True)}">{_num(pb)}</td>'
            f'<td class="num vs-cell vs-c-roe" style="{_heat(roe, roe_lo, roe_hi)}">{_pct(roe)}</td>'
            f'<td class="num vs-cell vs-c-rev" style="{_heat(rev, rev_lo, rev_hi)}">{_pct(rev)}</td>'
            f'<td class="num vs-cell vs-c-fcf">{fcf_html}</td>'
            f'<td class="num vs-cell vs-c-de" style="{_heat(de, de_lo, de_hi, invert=True)}">{_num(de, 2)}</td>'
            f'<td class="num vs-cell vs-c-52w" style="{_heat(w, w_lo, w_hi, invert=True, rgb="245,158,11")}">{_num(w, 0)}%</td>'
            f'<td class="num {pass_cls} vs-passcount vs-c-pass">{r["pass_count"]}/6</td>'
            f'</tr>'
        )

    n_shown = len(shown)
    nav, section_attr, table_attr = '', '', ''
    if n_shown > VALUE_PAGE:
        npages = (n_shown + VALUE_PAGE - 1) // VALUE_PAGE
        section_attr = f' data-vs-pages="{npages}"'
        table_attr = f' data-vs-page="{VALUE_PAGE}"'
        nav = ('<div class="vs-nav">'
               '<button type="button" class="vs-arrow vs-prev" aria-label="Previous set">&#8249;</button>'
               f'<span class="vs-page-ind"><span class="vs-page-cur">1</span>&#8202;/&#8202;{npages}</span>'
               '<button type="button" class="vs-arrow vs-next" aria-label="Next set">&#8250;</button>'
               '</div>')

    header = ('<thead><tr>'
              '<th>Ticker</th><th>Sector</th>'
              '<th class="num" title="Trailing price/earnings. Passes when positive and below its sector median (cheaper than peers).">P/E</th>'
              '<th class="num" title="Price/book ratio. Lower is cheaper relative to book value.">P/B</th>'
              '<th class="num" title="Return on equity — profitability. Passes above 10%.">ROE</th>'
              '<th class="num" title="Year-over-year revenue growth. Passes when positive.">Rev</th>'
              '<th class="num" title="Free cash flow. Passes when positive (the + sign).">FCF</th>'
              '<th class="num" title="Debt / equity. Passes below 1.5.">D/E</th>'
              '<th class="num" title="Where the price sits in its 52-week range: 0% = at the low. Required gate: within 10% of the low.">52w</th>'
              '<th class="num" title="How many of the six filters this name passed.">Pass</th>'
              '</tr></thead>')
    return (f'<section class="value-screen-section"{section_attr}>'
            f'<div class="vs-head">{head_h3}{sub}</div>{legend}{nav}'
            f'<div class="vs-scroll"><table class="vs-table"{table_attr}>'
            f'{header}<tbody>{"".join(body)}</tbody></table></div></section>')


def render_news(news_items: list[dict]) -> str:
    if not news_items:
        return """<section class="news-section">
  <div class="news-head"><h3>Market news</h3><span class="muted news-stale">unavailable at last build</span></div>
  <p class="muted">RSS feed unreachable when this page was generated.</p>
</section>"""
    rows = []
    sources_seen = []  # preserve order, dedupe
    for it in news_items:
        src = str(it["source"])
        if src not in sources_seen:
            sources_seen.append(src)
        rows.append(
            f'<a class="news-row" data-source="{_esc(src)}" href="{safe_url(it["link"])}" target="_blank" rel="noopener noreferrer">'
            f'  <div class="news-title">{_esc(it["title"])}</div>'
            f'  <div class="news-meta"><span class="news-src">{_esc(src)}</span>'
            f'  <span class="news-dot">·</span><span class="news-when">{_esc(it["published_pretty"])}</span></div>'
            f'</a>'
        )
    chips = ['<button class="news-chip active" data-src="*">All</button>']
    for src in sources_seen:
        chips.append(f'<button class="news-chip" data-src="{_esc(src)}">{_esc(src)}</button>')
    built = datetime.now(timezone.utc).strftime("%d %b %H:%M UTC")
    return f"""<section class="news-section">
  <div class="news-head">
    <h3>Market news</h3>
    <span class="muted news-stale">as of {built}</span>
  </div>
  <div class="news-chips" role="tablist">{''.join(chips)}</div>
  <div class="news-list">{''.join(rows)}</div>
</section>"""


def _rec_label(rec: str) -> "tuple[str, str]":
    """(display label, css class) for a normalized rec key."""
    return _REC_LABELS.get(_norm_rec(rec), ("&mdash;", "an-rec-none"))


_RM_GROUP_CAP = 10      # max rows per group
_RM_GROUP_RESERVE = 4   # slots kept for the opposite direction (cuts / downgrades)


def _cap_with_reserve(good: list, bad: list,
                      total: int = _RM_GROUP_CAP,
                      reserve: int = _RM_GROUP_RESERVE) -> list:
    """Show `good` first (priority), but reserve up to `reserve` slots for `bad`
    when any exist, so the opposite direction never fully disappears under the
    cap. Falls back to all-`good` when there's no `bad` (e.g. no target cuts)."""
    keep_bad = min(len(bad), reserve)
    good_show = good[: max(total - keep_bad, 0)]
    bad_show = bad[: total - len(good_show)]
    return good_show + bad_show


def _rm_target_row(m: dict) -> str:
    """A price-target row. Direct grid children so the $ columns align across
    rows: ticker | before | arrow | after | pct | rec(context)."""
    tkr = _esc(m["ticker"])
    pct = m["pct_change"]
    pcls = "pos" if pct >= 0 else "neg"
    sign = "+" if pct >= 0 else ""
    clabel, ccls = _rec_label(m.get("cur_rec", ""))
    return (
        f'<div class="rm-row rm-row--target" data-ticker="{tkr}">'
        f'<span class="rm-tkr ticker-clickable" data-ticker="{tkr}">{tkr}</span>'
        f'<span class="rm-from">${m["before"]:,.2f}</span>'
        f'<span class="rm-arrow">&rarr;</span>'
        f'<span class="rm-to">${m["after"]:,.2f}</span>'
        f'<span class="rm-pct {pcls}">{sign}{pct:.1f}%</span>'
        f'<span class="rm-rec rm-rec-static {ccls}">{clabel}</span>'
        f'</div>'
    )


def _rm_rec_row(m: dict) -> str:
    """A recommendation-change row. Grid children: ticker | target(context) |
    rec move (arrow + before -> after)."""
    tkr = _esc(m["ticker"])
    d = _rec_direction(m["before"], m["after"])
    if d == "up":
        dir_cls, arrow = "rm-up", "&uarr;"
    elif d == "down":
        dir_cls, arrow = "rm-down", "&darr;"
    else:
        dir_cls, arrow = "rm-lat", "&rarr;"
    blabel, _ = _rec_label(m["before"])
    alabel, _ = _rec_label(m["after"])
    ct = m.get("cur_target")
    tgt_ctx = (f'<span class="rm-target rm-target-static">${ct:,.2f}</span>'
               if ct is not None else
               '<span class="rm-target rm-target-static">&mdash;</span>')
    return (
        f'<div class="rm-row rm-row--rec" data-ticker="{tkr}">'
        f'<span class="rm-tkr ticker-clickable" data-ticker="{tkr}">{tkr}</span>'
        f'{tgt_ctx}'
        f'<span class="rm-rec rm-rec-move {dir_cls}">'
        f'<span class="rm-rec-arrow">{arrow}</span>'
        f'<span class="rm-rec-from">{blabel}</span>'
        f'<span class="rm-arrow">&rarr;</span>'
        f'<span class="rm-rec-to">{alabel}</span>'
        f'</span>'
        f'</div>'
    )


def render_rating_moves(moves: list[dict], prior_exists: bool,
                        baseline_date=None, baseline_seeded: bool = False) -> str:
    """v2.7 #4: split into two groups so BOTH kinds always show:
      - Price targets (upsides first, then cuts by magnitude) — each row also
        shows the current recommendation.
      - Recommendations (upgrades/initiations first, then downgrades) — each row
        flags direction (green up arrow / red down arrow) + the current target.
    v2.8: moves are measured against a rolling ~2-week baseline; when its date is
    known we show "since <date>" so the window is never a mystery (and a stale
    note if the baseline is unusually old). Empty-state copy depends on whether a
    baseline exists yet."""
    head = '<h3>Rating moves</h3>'
    if baseline_date is not None:
        _bd = pd.Timestamp(baseline_date)
        _label = f"{_bd.day} {_bd.strftime('%b')}"
        _age = (pd.Timestamp(datetime.now(timezone.utc).date()).normalize()
                - _bd.normalize()).days
        _stale = f' &middot; baseline {_age}d old' if _age > 16 else ''
        if baseline_seeded:
            # Cold-start: the baseline is a backdated copy of the legacy snapshot,
            # not a real measurement on that date — don't imply it was.
            sub = ('<span class="muted rm-sub">vs seeded baseline &middot; real '
                   '2-week history still building &middot; target &geq; 5% or rec change</span>')
        else:
            sub = (f'<span class="muted rm-sub">since {_label} &middot; '
                   f'target &geq; 5% or rec change{_stale}</span>')
    else:
        sub = ('<span class="muted rm-sub">rolling ~2-week window &middot; '
               'target &geq; 5% or rec change</span>')
    targets = [m for m in moves if m["kind"] == "target"]
    recs = [m for m in moves if m["kind"] == "recommendation"]
    if not targets and not recs:
        empty_msg = ("Building the 2-week history &mdash; rating moves appear as "
                     "daily analyst snapshots accumulate."
                     if not prior_exists
                     else "No material moves in the last ~2 weeks.")
        return f"""<section class="rating-moves-section">
  <div class="rm-head">{head}{sub}</div>
  <p class="muted rm-empty">{empty_msg}</p>
</section>"""
    # Price targets: upsides first (desc), then cuts by magnitude (desc) — but a
    # few slots are reserved for cuts so a big -X% never disappears under the cap.
    ups = sorted([m for m in targets if m["pct_change"] >= 0],
                 key=lambda m: (-m["pct_change"], m["ticker"]))
    cuts = sorted([m for m in targets if m["pct_change"] < 0],
                  key=lambda m: (m["pct_change"], m["ticker"]))   # most negative first
    targets_display = _cap_with_reserve(ups, cuts)
    # Recommendations: upgrades/initiations first, then downgrades; within a
    # group by resulting rating strength, then ticker. Downgrades get reserved
    # slots too, mirroring the targets group.
    def _rec_rank_key(m):
        return (_REC_RANK.get(_norm_rec(m["after"]), 9), m["ticker"])
    # Green upgrades must RESULT in buy or stronger (no "upgrade to hold").
    rec_up = sorted([m for m in recs
                     if _rec_direction(m["before"], m["after"]) == "up"
                     and _REC_RANK.get(_norm_rec(m["after"]), 9) <= 1],
                    key=_rec_rank_key)
    rec_down = sorted([m for m in recs
                       if _rec_direction(m["before"], m["after"]) == "down"],
                      key=_rec_rank_key)
    recs_display = _cap_with_reserve(rec_up, rec_down)

    groups = []
    if targets_display:
        rows = "".join(_rm_target_row(m) for m in targets_display)
        groups.append(
            '<div class="rm-group rm-group--targets">'
            '<div class="rm-group-label">Price targets '
            '<span class="muted">&middot; upsides first</span></div>'
            f'<div class="rm-list">{rows}</div></div>'
        )
    if recs_display:
        rows = "".join(_rm_rec_row(m) for m in recs_display)
        groups.append(
            '<div class="rm-group rm-group--recs">'
            '<div class="rm-group-label">Recommendations '
            '<span class="muted">&middot; upgrades first</span></div>'
            f'<div class="rm-list">{rows}</div></div>'
        )
    return f"""<section class="rating-moves-section">
  <div class="rm-head">{head}{sub}</div>
  <div class="rm-cols">{''.join(groups)}</div>
</section>"""


def render_toolbar(panel_n: int, n: int, returns: pd.DataFrame | None = None,
                   meta: pd.DataFrame | None = None) -> str:
    # Sector chips — derived from the GICS sectors actually present in returns.
    # Empty sector falls into "Other" so users can still reach those.
    sector_chips = ""
    if returns is not None and meta is not None and not returns.empty:
        sectors_seen: dict[str, int] = {}
        for tkr in returns.index:
            sec = (str(meta.loc[tkr, "sector"]).strip() if tkr in meta.index else "") or "Other"
            sectors_seen[sec] = sectors_seen.get(sec, 0) + 1
        ordered = sorted(sectors_seen.items(), key=lambda x: (-x[1], x[0]))
        chip_html = ['<button class="chip chip-sm chip-sec active" data-sector="*">All sectors</button>']
        for sec, count in ordered:
            chip_html.append(
                f'<button class="chip chip-sm chip-sec" data-sector="{_esc(sec)}">'
                f'{_esc(sec)} <span class="chip-count">{count}</span></button>'
            )
        sector_chips = f'<div class="chips chips-sectors" role="tablist">{"".join(chip_html)}</div>'
    return f"""<div class="toolbar">
  <input class="search" placeholder="Filter by ticker..." aria-label="Filter by ticker" />
  <div class="chips" role="tablist">
    <button class="chip active" data-filter="all">All {n}</button>
    <button class="chip" data-filter="basket">Open</button>
    <button class="chip" data-filter="closed">Closed</button>
    <button class="chip" data-filter="winners">Winners</button>
    <button class="chip" data-filter="losers">Losers</button>
    <button class="chip" data-filter="top10">Top 10</button>
    <button class="chip" data-filter="bottom10">Bottom 10</button>
  </div>
  {sector_chips}
</div>"""


# v1.8 T1: fixed-viewBox coordinate space for the modal chart. JS scales the
# viewBox to whatever the modal renders at via preserveAspectRatio="none", so
# the polyline + axis tick coordinates stay correct regardless of modal size.
# Padding chosen to match the legacy JS render (padL/padR/padT/padB) so the
# visual proportions are preserved.
MODAL_VB_W, MODAL_VB_H = 1000, 600
MODAL_VB_PAD_L, MODAL_VB_PAD_R = 56, 32
MODAL_VB_PAD_T, MODAL_VB_PAD_B = 28, 56


def _modal_polyline_d(rebased_values: list[float]) -> dict:
    """Pre-compute the modal chart's polyline + axis ticks in fixed viewBox
    coordinates (T1). Returns a dict that the JS render path consumes:

        {
          "points":  "x1,y1 x2,y2 ..." for the rebased polyline,
          "area_d":  "M ... Z" for the gradient-filled area under the line,
          "zero_y":  y-coordinate of the 0% baseline,
          "y_ticks": [{"v": pct, "y": y}, ...] 6 ticks,
          "x_ticks": [{"idx": i, "x": x}, ...] up to 5 ticks (idx into series),
          "xs":      [float, ...] per-point x coords (for hover indexing),
          "ys":      [float, ...] per-point y coords (for hover dot positioning),
          "vmin":    minimum rebased value (incl. 0% floor),
          "vmax":    maximum rebased value (incl. 0% floor),
        }

    JS uses `xs` + `ys` for hover snapping, and the other fields as-is to
    paint the chart at first open.
    """
    n = len(rebased_values)
    if n == 0:
        return {"points": "", "area_d": "", "zero_y": 0,
                "y_ticks": [], "x_ticks": [], "xs": [], "ys": [],
                "vmin": 0.0, "vmax": 0.0}
    vmin = min(0.0, min(rebased_values))
    vmax = max(0.0, max(rebased_values))
    span = max(vmax - vmin, 1e-9)
    inner_w = MODAL_VB_W - MODAL_VB_PAD_L - MODAL_VB_PAD_R
    inner_h = MODAL_VB_H - MODAL_VB_PAD_T - MODAL_VB_PAD_B

    def _x(i: int) -> float:
        if n == 1:
            return MODAL_VB_PAD_L + inner_w / 2
        return MODAL_VB_PAD_L + (i / (n - 1)) * inner_w

    def _y(v: float) -> float:
        return MODAL_VB_PAD_T + (1 - (v - vmin) / span) * inner_h

    xs = [_x(i) for i in range(n)]
    ys = [_y(v) for v in rebased_values]
    points = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
    zero_y = _y(0.0)
    # 6 y-ticks (incl. endpoints) — matches the legacy JS render.
    y_ticks = []
    for i in range(6):
        v = vmin + (i / 5) * span
        y_ticks.append({"v": float(v), "y": float(_y(v))})
    # Up to 5 x-ticks, evenly spaced over the series (indices into the
    # weekly series so JS can label them with the matching date).
    x_tick_count = min(5, n)
    x_tick_idx = []
    for i in range(x_tick_count):
        if x_tick_count == 1:
            x_tick_idx.append(0)
        else:
            x_tick_idx.append(int(round((i / (x_tick_count - 1)) * (n - 1))))
    # v1.9 #2: per-segment color. Split the polyline into same-sign runs at
    # zero crossings so the JS render can color above-baseline portions green
    # and below-baseline portions red. Crossing x is linearly interpolated
    # against the rebased values so the color flip lands exactly on the
    # baseline, not the next data point. Single-segment outputs (positions
    # that never cross zero) are the common case for clean winners.
    segments: list[dict] = []
    if n >= 1:
        cur_above = rebased_values[0] >= 0
        cur_pts: list[tuple[float, float]] = [(xs[0], ys[0])]
        for i in range(1, n):
            prev_v, cur_v = rebased_values[i - 1], rebased_values[i]
            x_prev, x_cur = xs[i - 1], xs[i]
            if (prev_v >= 0) == (cur_v >= 0):
                cur_pts.append((x_cur, ys[i]))
            else:
                denom = abs(prev_v) + abs(cur_v)
                t = abs(prev_v) / denom if denom > 0 else 0.5
                x_cross = x_prev + t * (x_cur - x_prev)
                cur_pts.append((x_cross, zero_y))
                segments.append({
                    "pts": " ".join(f"{x:.1f},{y:.1f}" for x, y in cur_pts),
                    "above": cur_above,
                })
                cur_above = cur_v >= 0
                cur_pts = [(x_cross, zero_y), (x_cur, ys[i])]
        segments.append({
            "pts": " ".join(f"{x:.1f},{y:.1f}" for x, y in cur_pts),
            "above": cur_above,
        })
    # Slimmed payload: JS reconstructs xs/ys/area-path at render time
    # (cheap, sub-millisecond) which keeps the payload ~600 KB smaller across
    # the basket. Tick x-coords are derived in JS from `_x(idx)` mirror.
    return {
        "points": points,
        "segments": segments,
        "zero_y": float(zero_y),
        "y_ticks": y_ticks,
        "x_tick_idx": x_tick_idx,
        "n": n,
        "vmin": float(vmin),
        "vmax": float(vmax),
    }


# v2.0 lazy-modal: fields kept inline in docs/index.html (needed at first paint
# by the main table, sort/filter chips, contributors panel). Everything NOT in
# this set moves to docs/data/payload.json, fetched on idle and merged into
# DATA[tkr] on first modal-open.
# Conservative: includes a few fields (status, contribution) that the modal
# also uses but which the main-table render path needs at startup. Cost of
# duplication ~10 KB; cost of missing one = broken table at first paint.
LIGHT_KEYS: frozenset[str] = frozenset({
    "name", "sector", "industry", "currency",
    "weight", "total", "ytd", "w1", "m1", "m3",
    "status", "signal", "signal_tone", "signal_detail",
    "contribution",
})


def split_payload(full: dict) -> tuple[dict, dict]:
    """Split the per-ticker payload into light (inline) + heavy (sidecar).

    Light dict carries only LIGHT_KEYS that exist on each entry (handles
    watchlist entries gracefully — they may omit modal-only fields).
    Heavy dict carries everything else."""
    light, heavy = {}, {}
    for tkr, d in full.items():
        light[tkr] = {k: d[k] for k in LIGHT_KEYS if k in d}
        heavy_entry = {k: v for k, v in d.items() if k not in LIGHT_KEYS}
        if heavy_entry:
            heavy[tkr] = heavy_entry
    return light, heavy


def build_data_payload(returns: pd.DataFrame, prices: pd.DataFrame,
                       meta: pd.DataFrame, contrib: pd.DataFrame,
                       signals: pd.DataFrame,
                       prices_native: pd.DataFrame,
                       returns_native: pd.DataFrame,
                       quant_metrics: pd.DataFrame | None = None,
                       ticker_news: pd.DataFrame | None = None) -> dict:
    daily = prices.ffill()
    contrib_lookup = contrib["contribution_pp"].to_dict() if not contrib.empty else {}
    payload = {}
    for tkr, r in returns.iterrows():
        if tkr not in daily.columns:
            continue
        s = daily[tkr].dropna()
        if s.empty:
            continue
        # Draw the FULL holding path from the earliest buy across all cycles, so a
        # sold-then-rebought name (e.g. MSTR under the every-sell-resets snapshot)
        # shows its pre-re-buy history + trade markers. The 0%/baseline divisor
        # below stays r.baseline (active-cycle cost), so no displayed % moves.
        _chart_start = (r.first_acquired_date
                        if "first_acquired_date" in r.index and pd.notna(r.first_acquired_date)
                        else r.baseline_date)
        s_from_base = s.loc[s.index >= _chart_start]
        s_weekly = s_from_base.resample("W-FRI").last().ffill().dropna()
        if s_weekly.empty:
            s_weekly = s_from_base.tail(1)
        sig = signals.loc[tkr] if tkr in signals.index else None
        ccy = ticker_currency(meta, tkr)
        ccy_symbol = CCY_SYMBOLS.get(ccy, ccy + " ")

        # FX attribution: (1 + total_base) = (1 + total_native) * (1 + fx_change)
        # So fx_change = (1 + total_base) / (1 + total_native) - 1
        native_total = None
        native_baseline = None
        native_latest = None
        fx_change = None
        if ccy != BASE_CCY and tkr in returns_native.index:
            rn = returns_native.loc[tkr]
            native_total = float(rn.total_pct)
            native_baseline = float(rn.baseline)
            native_latest = float(rn.latest)
            denom = 1 + native_total / 100
            if abs(denom) > 1e-9:
                fx_change = ((1 + r.total_pct / 100) / denom - 1) * 100

        # NaN-safe number helper for JSON (Python's json doesn't allow NaN)
        def _safe(v):
            if v is None:
                return None
            try:
                fv = float(v)
                return None if (fv != fv) else fv  # NaN check
            except (TypeError, ValueError):
                return None

        payload[tkr] = {
            "name": str(meta.loc[tkr, "name"]) if tkr in meta.index else tkr,
            "sector": str(meta.loc[tkr, "sector"]) if tkr in meta.index else "",
            "industry": str(meta.loc[tkr, "industry"]) if tkr in meta.index else "",
            "currency": ccy,
            "ccy_symbol": ccy_symbol,
            "baseline": float(r.baseline),
            "baseline_date": r.baseline_date.strftime("%Y-%m-%d"),
            # v1.8.1 B4: expose last_action_date (most recent SELL for closed,
            # last txn for any status) so the modal can show "between buy and
            # sell" rather than the misleading "since buy" for closed positions.
            "last_action_date": (pd.Timestamp(r.last_action_date).strftime("%Y-%m-%d")
                                  if "last_action_date" in r.index and pd.notna(r.last_action_date)
                                  else None),
            "latest": float(r.latest),
            "total": float(r.total_pct),
            "w1": _safe(r["1w_pct"]),
            "m1": _safe(r["1m_pct"]),
            "m3": _safe(r["3m_pct"]),
            "ytd": _safe(r.ytd_pct),
            "weight": float(r.weight),
            "contribution": float(contrib_lookup.get(tkr, 0.0)) if r.weight > 0 else None,
            "signal": str(sig.signal) if sig is not None else "",
            "signal_tone": str(sig.tone) if sig is not None else "neutral",
            "signal_detail": str(sig.detail) if sig is not None else "",
            "native_total": native_total,
            "native_baseline": native_baseline,
            "native_latest": native_latest,
            "fx_change": fx_change,
            "status": str(r.status) if "status" in r.index else "open",
            "shares_held": float(r.shares_held) if "shares_held" in r.index else 0.0,
            "total_invested": float(r.total_invested) if "total_invested" in r.index else 0.0,
            "total_received": float(r.total_received) if "total_received" in r.index else 0.0,
            "realized_pnl": float(r.realized_pnl) if "realized_pnl" in r.index else 0.0,
            "unrealized_pnl": float(r.unrealized_pnl) if "unrealized_pnl" in r.index else 0.0,
            "post_exit": _safe(r.post_exit_pct) if "post_exit_pct" in r.index else None,
            "transactions": list(r.transactions) if "transactions" in r.index else [],
            "dates": [d.strftime("%Y-%m-%d") for d in s_weekly.index],
            "prices": [round(float(p), 4) for p in s_weekly.tolist()],
        }
        # T1: precompute the modal chart polyline in fixed-viewBox coordinates.
        # Rebased values are (p / baseline - 1) * 100 — the same formula the
        # legacy JS used at render time. Doing it here saves ~150-300 ms on
        # first modal-open and makes the chart resize-free (viewBox handles it).
        _baseline = float(r.baseline)
        if _baseline:
            _rebased = [(float(p) / _baseline - 1) * 100 for p in s_weekly.tolist()]
            payload[tkr]["chart"] = _modal_polyline_d(_rebased)
        if quant_metrics is not None and tkr in quant_metrics.index:
            q = quant_metrics.loc[tkr]
            payload[tkr]["quant"] = {
                "sma200_dist_pct": _safe(q.sma200_dist_pct),
                "atr14_gbp": _safe(q.atr14_gbp),
                "atr14_pct": _safe(q.atr14_pct),
                "rsi14": _safe(q.rsi14),
                "range52w_pct": _safe(q.range52w_pct),
                "vol_ratio": _safe(q.vol_ratio),
            }
        # Per-ticker recent news (top 5). Stored JSON-encoded in the cache;
        # decode once here, surface as a clean list in the payload.
        if ticker_news is not None and tkr in ticker_news.index:
            raw_json = ticker_news.loc[tkr, "items_json"]
            try:
                items = json.loads(raw_json) if isinstance(raw_json, str) and raw_json else []
            except (TypeError, ValueError):
                items = []
            if items:
                payload[tkr]["news"] = items
    return payload


def _hero_legend_html(has_nasdaq: bool) -> str:
    """Hero-chart legend items. Nasdaq is an optional toggle button rendered only
    when QQQ data is present; basket + SPY are fixed reference labels (they drive
    the vs-SPY area, delta badge and alpha sparkline, so they are not toggleable)."""
    items = [
        '<div class="leg"><span class="leg-swatch basket"></span>Basket</div>',
        '<div class="leg"><span class="leg-swatch spy"></span>SPY</div>',
    ]
    if has_nasdaq:
        items.append(
            '<button type="button" class="leg leg-toggle" data-series="nasdaq" '
            'aria-pressed="false" title="Show/hide the Nasdaq-100 (QQQ) line">'
            '<span class="leg-swatch nasdaq"></span>Nasdaq</button>'
        )
    items.append('<div class="leg"><span class="leg-swatch fx"></span>GBP/USD</div>')
    return "\n        ".join(items)


def build_portfolio_payload(basket: pd.Series, bench: pd.Series,
                            first_purchase: pd.Timestamp,
                            fx: pd.DataFrame | None = None,
                            nasdaq: pd.Series | None = None) -> dict:
    # Resample to weekly to keep JSON small (~3KB vs ~25KB daily)
    def _weekly(s):
        if s.empty:
            return [], []
        w = s.resample("W-FRI").last().ffill().dropna()
        return [d.strftime("%Y-%m-%d") for d in w.index], [round(float(v), 4) for v in w.tolist()]

    b_dates, b_values = _weekly(basket)
    s_dates, s_values = _weekly(bench)
    n_dates, n_values = (_weekly(nasdaq)
                         if nasdaq is not None and not nasdaq.empty else ([], []))
    # FX overlay: yfinance gives USDGBP=X (pounds per dollar, typical ~0.78).
    # Invert to GBP/USD (dollars per pound, typical ~1.28) so the bar chart
    # reads naturally: up bar = stronger GBP, down bar = weaker GBP.
    fx_dates: list[str] = []
    fx_values: list[float] = []
    if fx is not None and not fx.empty and "USDGBP=X" in fx.columns:
        fxs = fx["USDGBP=X"].dropna()
        if not fxs.empty:
            fxs = 1.0 / fxs   # invert to GBP/USD
            fx_dates, fx_values = _weekly(fxs)
    return {
        "first_purchase": first_purchase.strftime("%Y-%m-%d"),
        "basket": {"dates": b_dates, "values": b_values},
        "spy":    {"dates": s_dates, "values": s_values},
        "nasdaq": {"dates": n_dates, "values": n_values},
        "fx":     {"dates": fx_dates, "values": fx_values, "pair": "GBP/USD"},
    }


def build_aux_payload(returns: pd.DataFrame, prices: pd.DataFrame,
                      meta: pd.DataFrame, universe_outlook: pd.DataFrame | None,
                      diversification_data: dict | None,
                      basket_first_date: pd.Timestamp | None = None,
                      log_tickers: "set[str] | None" = None) -> dict:
    """T11/T12/T14/T15: pre-shape per-modal data so the JS click handlers can
    look up "what to show in the drill-down modal" by O(1) lookup.

    Returns four top-level keys:
      - industries:    dict[industry_label] -> [{ticker, name, return_12mo, cap_tier}, ...]
      - sectors:       dict[sector_label]   -> [{ticker, name, weight, total_pct, contribution_pp}, ...]
      - pairs:         [{a, b, corr}, ...] (sorted by abs(corr) desc)
      - weekly_movers: dict[date_str]       -> {up: [...], down: [...]}
    """
    out: dict = {"industries": {}, "sectors": {}, "pairs": [], "weekly_movers": {}}

    # T11: every universe ticker grouped by industry (so a click on the
    # industry-outlook card opens "all tickers in this industry").
    # Schema note: universe_outlook uses `ret_12m` (not return_12m_pct), and
    # the ticker name lives in `meta` (joined by ticker index), not in the
    # universe frame itself.
    if universe_outlook is not None and not universe_outlook.empty:
        # v2.6 #3: match the industry-outlook card's count exactly — it excludes
        # held names (log.xlsx) and skips tickers without a 12-mo return, so the
        # "see all" modal must too (otherwise the card says 4 but lists 7).
        _skip = log_tickers or set()
        for tkr, row in universe_outlook.iterrows():
            if tkr in _skip:
                continue
            ind = str(row.get("industry") or "").strip()
            if not ind:
                continue
            r12 = row.get("ret_12m")
            if r12 is None or pd.isna(r12):
                continue
            tier = row.get("cap_tier")
            name = (str(meta.loc[tkr, "name"]).strip()
                    if (tkr in meta.index and pd.notna(meta.loc[tkr, "name"]))
                    else str(tkr))
            entry = {
                "ticker": str(tkr),
                "name": name or str(tkr),
                "return_12mo": (float(r12) if pd.notna(r12) else None),
                "cap_tier": (str(tier) if pd.notna(tier) and tier else ""),
            }
            out["industries"].setdefault(ind, []).append(entry)
        # Sort each industry by 12mo return descending (best at top).
        for ind in out["industries"]:
            out["industries"][ind].sort(
                key=lambda e: (e["return_12mo"] is None, -(e["return_12mo"] or 0)),
            )

    # T12: open positions grouped by INDUSTRY (matching the attribution row key
    # logic in build_industry_attribution: industry first, sector fallback,
    # "Other" if neither). The dict key here is named "sectors" in the payload
    # for forward-compat with potential future sector-only views but matches
    # whatever the attribution rows use as their group label.
    if not returns.empty:
        open_pos = returns[returns.status == "open"]
        total_weight = float(open_pos["weight"].sum()) if not open_pos.empty else 0.0
        for tkr, r in open_pos.iterrows():
            if tkr in meta.index:
                key = (str(meta.loc[tkr, "industry"] or meta.loc[tkr, "sector"] or "").strip()
                       or "Other")
                name = str(meta.loc[tkr, "name"] or tkr).strip()
            else:
                key, name = "Other", str(tkr)
            wt = float(r.weight) if pd.notna(r.weight) else 0.0
            wt_pct = (wt / total_weight * 100) if total_weight > 0 else 0.0
            tot = float(r.total_pct) if pd.notna(r.total_pct) else 0.0
            contrib_pp = (wt_pct / 100.0) * tot
            out["sectors"].setdefault(key, []).append({
                "ticker": str(tkr),
                "name": name,
                "weight_pct": wt_pct,
                "total_pct": tot,
                "contribution_pp": contrib_pp,
            })
        # Sort each group by contribution descending (most-positive at top).
        for k in out["sectors"]:
            out["sectors"][k].sort(key=lambda e: -e["contribution_pp"])

    # T14: full pair-correlation list (already sorted by abs(corr) desc).
    if diversification_data and "all_pairs" in diversification_data:
        out["pairs"] = diversification_data["all_pairs"]

    # T15: per-week top-5 and bottom-5 movers across all held tickers, keyed
    # by the week-ending date (Friday). Computed from the same `prices` frame
    # used elsewhere -- already in base currency (GBP), so percentages reflect
    # what the user would actually see in P&L terms.
    # v1.8.1 B3: filter per week by each ticker's hold window. Previously a
    # closed ticker (e.g. Corvus sold Jan 2025) still appeared in mover lists
    # for every later week because prices.ffill kept its price series alive.
    # Now: for each ticker, include only weeks within [first_buy_date,
    # last_action_date] for closed, or [first_buy_date, +inf) for open.
    if not prices.empty and not returns.empty:
        held_tickers = [t for t in returns.index if t in prices.columns]
        # Per-ticker hold window. None on either end means "no bound".
        holds: dict[str, tuple[pd.Timestamp | None, pd.Timestamp | None]] = {}
        for tkr in held_tickers:
            row = returns.loc[tkr]
            fbd = pd.Timestamp(row.first_buy_date) if "first_buy_date" in row.index and pd.notna(row.first_buy_date) else None
            lad = pd.Timestamp(row.last_action_date) if "last_action_date" in row.index and pd.notna(row.last_action_date) else None
            status = str(row.status) if "status" in row.index else "open"
            holds[tkr] = (fbd, lad if status == "closed" else None)
        if held_tickers:
            sub = prices[held_tickers].copy()
            weekly = sub.resample("W-FRI").last().ffill()
            weekly_pct = weekly.pct_change() * 100
            for date_idx in weekly_pct.index:
                row = weekly_pct.loc[date_idx].dropna()
                if row.empty:
                    continue
                date_ts = pd.Timestamp(date_idx)
                # Keep only tickers held as of this week-ending date.
                def _held(t: str) -> bool:
                    fbd, lad = holds.get(t, (None, None))
                    if fbd is not None and date_ts < fbd:
                        return False
                    if lad is not None and date_ts > lad:
                        return False
                    return True
                row = row[[t for t in row.index if _held(str(t))]]
                if row.empty:
                    continue
                ordered = row.sort_values()
                top = [(t, float(v)) for (t, v) in ordered.tail(5).iloc[::-1].items() if v > 0]
                bot = [(t, float(v)) for (t, v) in ordered.head(5).items() if v < 0]
                if not top and not bot:
                    continue
                key = date_ts.strftime("%Y-%m-%d")
                out["weekly_movers"][key] = {
                    "up":   [{"ticker": t, "pct": v} for (t, v) in top],
                    "down": [{"ticker": t, "pct": v} for (t, v) in bot],
                }
    return out


def render_html(returns: pd.DataFrame, prices: pd.DataFrame, meta: pd.DataFrame,
                basket: pd.Series, bench: pd.Series, contrib: pd.DataFrame,
                transactions: pd.DataFrame, signals: pd.DataFrame,
                prices_native: pd.DataFrame, returns_native: pd.DataFrame,
                untracked: pd.DataFrame | None = None,
                watchlist: pd.DataFrame | None = None,
                news_items: list[dict] | None = None,
                analyst: pd.DataFrame | None = None,
                analyst_candidates: list[str] | None = None,
                fx: pd.DataFrame | None = None,
                universe_outlook: pd.DataFrame | None = None,
                quant_metrics: pd.DataFrame | None = None,
                ticker_news: pd.DataFrame | None = None,
                demo_mode: bool = False,
                watchlist_only: bool = False,
                sortable_inline_js: str | None = None,
                build_health: dict | None = None,
                rating_moves: list[dict] | None = None,
                prior_analyst_exists: bool = False,
                rating_baseline_date=None,
                rating_baseline_seeded: bool = False,
                unusual_vol: list[dict] | None = None,
                bb_universe_obs: list[dict] | None = None,
                prediction_rows: list[dict] | None = None,
                nasdaq_series: pd.Series | None = None,
                value_rows: "list[dict] | None" = None,
                auto_tickers: "list[str] | None" = None) -> str:
    weekly = prices.resample("W-FRI").last().ffill()
    defs_html = render_svg_defs()
    table_html = render_table(returns, weekly, meta, signals,
                              analyst=analyst if analyst is not None else None)
    detractors_html = render_detractors_strategy(
        contrib, returns, signals,
        analyst if analyst is not None else pd.DataFrame(),
        meta,
        quant_metrics=quant_metrics,
        prices=prices,
        prices_native=prices_native,
    )
    attribution_rows, basket_avg = build_industry_attribution(returns, meta)
    attribution_html = render_industry_attribution(attribution_rows, basket_avg)
    regret_html = render_regret_tracker(returns, meta)
    untracked_html = render_untracked(untracked) if untracked is not None else ""
    # v2.7: revived + enriched watchlist. Base card data, then an entry-signal
    # layer (verdict + trigger chips + news cite) reusing already-computed
    # quant/analyst/news. The module is reinstated next to "Re-entry ideas".
    _two_signal_set = {r["ticker"] for r in (value_rows or []) if r.get("is_bb_idea")}
    _combined_watchlist = build_combined_watchlist(watchlist, auto_tickers or [], _two_signal_set)
    watchlist_payload = (build_watchlist_payload(_combined_watchlist, prices, prices_native, meta)
                         if not _combined_watchlist.empty else {})
    if watchlist_payload:
        watchlist_payload = build_watchlist_signals(
            watchlist_payload, quant_metrics, analyst, ticker_news, meta)
    watchlist_html = render_watchlist(watchlist_payload, meta)
    candidates = analyst_candidates or []
    analyst_rows = (build_analyst_payload(candidates, analyst, prices_native, meta,
                                          signals=signals, quant_metrics=quant_metrics)
                    if analyst is not None and not analyst.empty else [])
    analyst_html = render_analyst_signals(analyst_rows, len(candidates)) if (analyst_rows or candidates) else ""
    # Industry outlook — universe-only, excluding everything in log.xlsx
    log_tickers_set = set(transactions.ticker.unique().tolist()) if not transactions.empty else set()
    industry_groups = build_industry_outlook(
        universe_outlook,
        log_tickers=log_tickers_set,
    )
    universe_count = (len(universe_outlook) if universe_outlook is not None else 0)
    industry_html = render_industry_outlook(industry_groups, universe_count)
    news_html = render_news(news_items or [])

    # Basket diversification — 6-month pairwise correlation summary across
    # open positions. Returns None (and renders nothing) if fewer than 2
    # open positions or insufficient overlapping history.
    diversification_data = compute_basket_correlation(returns, prices_native,
                                                       lookback_days=126)
    diversification_html = render_basket_diversification(diversification_data, meta) \
        if diversification_data else ""
    # v1.9 #3: currency exposure
    ccy_exposure_rows = compute_currency_exposure(returns, meta)
    ccy_exposure_html = render_currency_exposure(ccy_exposure_rows)

    latest_date = prices.index[-1].strftime("%d %b %Y")
    built = datetime.now(timezone.utc).strftime("%d %b %Y &middot; %H:%M UTC")
    _dash_version = _dashboard_version()
    version_html = (f' &middot; <span class="build-ver">v{_esc(_dash_version)}</span>'
                    if _dash_version else "")

    # v2.2 rethink: per-ticker signal-stacking. Pure post-processing over
    # already-computed metrics; sits at the top of the module stack.
    bigbrain_observations = compute_bigbrain_observations(
        returns, meta, contrib=contrib, quant_metrics=quant_metrics,
        signals=signals, analyst=analyst, rating_moves=rating_moves,
        ticker_news=ticker_news, universe_observations=bb_universe_obs,
        fx=fx,
    )
    # v2.3 memory: log today's flags + compute "since" notes (real build only)
    bigbrain_memory = {}
    if not demo_mode:
        _obs_prices = {}
        for o in bigbrain_observations:
            t = o["ticker"]
            if t in returns.index:
                _obs_prices[t] = float(returns.loc[t, "latest"])
            elif universe_outlook is not None and t in universe_outlook.index:
                _obs_prices[t] = _bb_num(universe_outlook.loc[t].get("current_price"))
        _today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        log_bigbrain_flags(bigbrain_observations, _today, _obs_prices)
        try:
            _log = pd.read_csv(BIGBRAIN_LOG_CSV, dtype=str) if BIGBRAIN_LOG_CSV.exists() else None
            bigbrain_memory = compute_bigbrain_memory(_log, _obs_prices, _today) if _log is not None else {}
        except Exception:
            bigbrain_memory = {}
    _bb_top_move = max(prediction_rows or [],
                       key=lambda r: abs(r.get("delta_pp") or 0), default=None)
    _bb_macro_html = render_bigbrain_macro(_bb_top_move, _basket_equity_share(returns, meta))
    bigbrain_html = render_bigbrain(bigbrain_observations, latest_date,
                                    macro_html=_bb_macro_html, memory=bigbrain_memory)
    # "as of" the prediction-market fetch date carried in the data (honest for
    # both the live build and the committed-cache demo/CI path); fall back to the
    # price date only for legacy caches with no stamped fetch date.
    _pred_asof = next((r.get("fetched_at") for r in (prediction_rows or [])
                       if r.get("fetched_at")), latest_date)
    market_expectations_html = render_market_expectations(prediction_rows or [], _pred_asof)
    quadrant_html = render_signal_strip(build_signal_strip_data(returns, quant_metrics, signals))

    # v2.6 Value screen — fundamental quality+value names near a 52-week low.
    # New ideas only (excludes held); cross-tags names Big Brain also flagged.
    if value_rows is None:
        _bb_idea_tkrs = {o["ticker"] for o in bigbrain_observations
                         if o.get("ownership") == "idea"}
        value_rows = build_value_screen(universe_outlook, log_tickers=log_tickers_set,
                                        bb_idea_tickers=_bb_idea_tkrs)
    # "as of" = the universe cache's last refresh (the monthly fetch), not today,
    # since the screen's data is only as fresh as that fetch. Use the embedded
    # cache_date (NOT mtime, which CI resets every run → would always read "today"
    # on month-old data, the exact M-VAL/M-IND staleness mislabel).
    _uni_date = _universe_cache_date()
    value_as_of = (_uni_date.strftime("%d %b %Y") if _uni_date is not None else latest_date)
    value_html = render_value_screen(value_rows, value_as_of)

    n_total = len(returns)
    n_open = int((returns.status == "open").sum()) if not returns.empty else 0
    n_closed = int((returns.status == "closed").sum()) if not returns.empty else 0
    if not transactions.empty:
        first_purchase = pd.Timestamp(transactions[transactions.action == "BUY"].date.min())
    else:
        first_purchase = DEFAULT_BASELINE
    first_purchase_str = first_purchase.strftime("%d %b %Y")
    # Names used by surviving templating
    n_basket = n_open  # the eyebrow + filter counts still call this "in basket"
    n_watch = n_closed

    basket_final = float(basket.iloc[-1]) if not basket.empty else 0.0
    spy_final = float(bench.iloc[-1]) if not bench.empty else 0.0
    vs_spy = basket_final - spy_final

    # T4: total + geometric-annualized return for the hero subtitle.
    # basket_final is a percentage (e.g. 31.7 means +31.7%), so we divide by
    # 100 before compounding and multiply back at the end. Annualization is
    # suppressed below 3 months elapsed -- extrapolating from a tiny window
    # produces misleadingly large numbers.
    years_elapsed = max(0.0,
        (pd.Timestamp.now() - first_purchase).days / 365.25)
    _total_cls = "pos" if basket_final >= 0 else "neg"
    _total_html = f'<span class="{_total_cls}">{basket_final:+.1f}%</span>'
    _ann_html = ""
    if years_elapsed >= 0.25 and basket_final > -100:
        annualized_pct = ((1 + basket_final / 100) ** (1 / years_elapsed) - 1) * 100
        _ann_cls = "pos" if annualized_pct >= 0 else "neg"
        _ann_html = (f' <span class="hero-sub-sep">&middot;</span> '
                     f'<span class="{_ann_cls}">{annualized_pct:+.1f}%</span> annualized')

    # v1.8.1 B2: YTD return next to the annualized %. basket is cumulative-%,
    # so YTD = ((1 + final/100) / (1 + at_jan1/100) - 1) * 100 -- recovers the
    # true compounding return for the current calendar year rather than the
    # plain pp-delta on cumulative %. Skip rendering when the basket series
    # doesn't reach this year (e.g. first build in a new year before any data).
    _ytd_html = ""
    if not basket.empty:
        ytd_cutoff = pd.Timestamp(f"{basket.index[-1].year}-01-01")
        ytd_sub = basket.loc[basket.index <= ytd_cutoff]
        ytd_ref = float(ytd_sub.iloc[-1]) if not ytd_sub.empty else None
        if ytd_ref is not None and (1 + ytd_ref / 100) != 0:
            ytd_pct = ((1 + basket_final / 100) / (1 + ytd_ref / 100) - 1) * 100
            _ytd_cls = "pos" if ytd_pct >= 0 else "neg"
            _ytd_html = (f' <span class="hero-sub-sep">&middot;</span> '
                         f'<span class="{_ytd_cls}">{ytd_pct:+.1f}%</span> YTD')

    hero_sub_html = (
        f'{_total_html} total{_ann_html}{_ytd_html}'
        f' <span class="hero-sub-meta">&middot; equal-weight basket &middot; '
        f'avg per-position TWR, renormalized as positions enter</span>'
    )

    # T10: unusual-volume chips. Compose a small pinned row of pills for
    # held names trading on >2x average volume with non-trivial moves.
    # Empty when nothing qualifies (the whole row is suppressed).
    _uv = unusual_vol or []
    if _uv:
        _chips = []
        for u in _uv:
            tkr = _esc(u["ticker"])
            move_sign = "+" if u["daily_pct"] >= 0 else ""
            move_cls = "uv-up" if u["daily_pct"] >= 0 else "uv-down"
            _chips.append(
                f'<button type="button" class="uv-chip ticker-clickable {move_cls}" '
                f'data-ticker="{tkr}" '
                f'title="Click to open {tkr} detail">'
                f'<span class="uv-tkr">{tkr}</span>'
                f'<span class="uv-move">{move_sign}{u["daily_pct"]:.1f}%</span>'
                f'<span class="uv-vol">{u["vol_ratio"]:.1f}&times; vol</span>'
                f'</button>'
            )
        unusual_vol_html = (
            f'<div class="unusual-vol-row" role="region" aria-label="Unusual volume">'
            f'<span class="uv-label">Unusual volume today:</span>'
            f'{"".join(_chips)}'
            f'</div>'
        )
    else:
        unusual_vol_html = ""

    # T7: 30-day rolling alpha sparkline (basket excess over SPY, pp).
    # Server-side renders a small inline SVG; latest value is shown next to
    # the label for the at-a-glance "are we currently ahead?" signal. Cosmetic
    # follow-up: hover shows date + value at the nearest point via JS overlay
    # (data-dates / data-values attributes consumed by setupAlphaHover()).
    # v2.7 #6: shared calendar domain (basket start -> today) so the alpha +
    # drawdown sparklines map x by DATE over the same span as the main chart,
    # instead of each stretching its own length across the full width.
    _spark_dom_start = basket.index[0] if not basket.empty else None
    _spark_dom_end = basket.index[-1] if not basket.empty else None
    _spark_dom_iso = ("" if _spark_dom_start is None
                      else _spark_dom_start.strftime("%Y-%m-%d"))
    _spark_dom_end_iso = ("" if _spark_dom_end is None
                          else _spark_dom_end.strftime("%Y-%m-%d"))

    rolling_alpha = compute_rolling_alpha(basket, bench, window_days=30)
    if rolling_alpha.empty or len(rolling_alpha) < 5:
        alpha_sparkline_html = ""
    else:
        SW, SH = 240, 36  # viewBox in svg units
        # pad_x = 0: horizontal inset is handled by the .spark-plot CSS wrapper
        # (padding-left/right = the chart's padL/padR) so the plot area matches.
        pad_x, pad_y = 0, 4
        vals = rolling_alpha.tolist()
        dates = [d.strftime("%Y-%m-%d") for d in rolling_alpha.index]
        vmin, vmax = min(vals), max(vals)
        # Add a hair of headroom so flat-line edge cases don't divide-by-zero.
        vrange = max(vmax - vmin, 1e-9)
        zero_in_range = (vmin <= 0 <= vmax)
        n = len(vals)
        # Map each point to (x, y). x evenly spaced; y inverted (SVG down=+).
        def _y(v: float) -> float:
            return pad_y + (vmax - v) / vrange * (SH - 2 * pad_y)
        def _x(i: int) -> float:
            if _spark_dom_start is None or _spark_dom_end is None:
                return pad_x + i / max(n - 1, 1) * (SW - 2 * pad_x)
            f = _date_fraction(rolling_alpha.index[i], _spark_dom_start, _spark_dom_end)
            return pad_x + f * (SW - 2 * pad_x)
        points = " ".join(f"{_x(i):.1f},{_y(v):.2f}" for i, v in enumerate(vals))
        baseline_y = _y(0.0) if zero_in_range else None
        baseline_svg = (
            f'<line x1="0" y1="{baseline_y:.2f}" x2="{SW}" y2="{baseline_y:.2f}" '
            f'stroke="var(--text-dim)" stroke-width="0.5" stroke-dasharray="2,3" opacity="0.5"/>'
            if baseline_y is not None else ""
        )
        latest = vals[-1]
        latest_cls = "pos" if latest >= 0 else "neg"
        stroke_color = "var(--up)" if latest >= 0 else "var(--down)"
        # v1.8 T3: split into same-sign runs at zero crossings so the chart is
        # green where the basket led SPY and red where it trailed -- previously
        # the entire line used a single color picked from the LATEST value, so a
        # currently-leading line that had recently been deep red still read as
        # "all green". Interpolate the exact x at the y=0 crossing so the color
        # flip lands on the baseline, not the next data point.
        def _build_segmented_polylines() -> str:
            if not zero_in_range:
                # No crossing — emit one polyline at the sign of the run.
                color = "var(--up)" if vals[0] >= 0 else "var(--down)"
                return (f'<polyline points="{points}" fill="none" stroke="{color}" '
                        f'stroke-width="1.2" stroke-linejoin="round"/>')
            segments: list[tuple[list[tuple[float, float]], str]] = []
            cur_color = "var(--up)" if vals[0] >= 0 else "var(--down)"
            cur_pts: list[tuple[float, float]] = [(_x(0), _y(vals[0]))]
            for i in range(1, len(vals)):
                prev_v, cur_v = vals[i - 1], vals[i]
                x_prev, x_cur = _x(i - 1), _x(i)
                if (prev_v >= 0) == (cur_v >= 0):
                    cur_pts.append((x_cur, _y(cur_v)))
                else:
                    denom = abs(prev_v) + abs(cur_v)
                    t = abs(prev_v) / denom if denom > 0 else 0.5
                    x_cross = x_prev + t * (x_cur - x_prev)
                    y_zero = _y(0.0)
                    cur_pts.append((x_cross, y_zero))
                    segments.append((cur_pts, cur_color))
                    cur_color = "var(--up)" if cur_v >= 0 else "var(--down)"
                    cur_pts = [(x_cross, y_zero), (x_cur, _y(cur_v))]
            segments.append((cur_pts, cur_color))
            return "".join(
                f'<polyline points="{" ".join(f"{x:.1f},{y:.2f}" for x, y in pts)}" '
                f'fill="none" stroke="{color}" stroke-width="1.2" stroke-linejoin="round"/>'
                for pts, color in segments
            )
        polyline_svg = _build_segmented_polylines()
        # Embed dates + values for the hover layer. Comma-separated keeps the
        # attribute compact; JS parses on demand.
        dates_attr = ",".join(dates)
        values_attr = ",".join(f"{v:.3f}" for v in vals)
        alpha_sparkline_html = (
            f'<div class="alpha-sparkline-wrap" id="alpha-sparkline-wrap">'
            f'  <div class="alpha-sparkline-head spark-plot">'
            f'    <span class="alpha-sparkline-label" title="Raw excess return vs SPY &mdash; not beta-adjusted, so persistent green can reflect higher market exposure rather than stock-picking skill">30-day excess vs SPY</span>'
            f'    <span class="alpha-sparkline-latest {latest_cls}" id="alpha-sparkline-latest" '
            f'          data-default-text="{latest:+.1f} pp">{latest:+.1f} pp</span>'
            f'  </div>'
            f'  <div class="spark-plot">'
            f'  <svg class="alpha-sparkline" id="alpha-sparkline-svg" '
            f'       viewBox="0 0 {SW} {SH}" preserveAspectRatio="none" '
            f'       data-dates="{dates_attr}" data-values="{values_attr}" '
            f'       data-domain-start="{_spark_dom_iso}" data-domain-end="{_spark_dom_end_iso}" '
            f'       data-baseline-y="{baseline_y if baseline_y is not None else -1}">'
            f'    {baseline_svg}'
            f'    {polyline_svg}'
            f'    <line class="alpha-cross" x1="0" y1="0" x2="0" y2="{SH}" '
            f'          stroke="var(--text-dim)" stroke-width="0.5" opacity="0" pointer-events="none"/>'
            f'    <circle class="alpha-dot" cx="0" cy="0" r="2.2" '
            f'            fill="{stroke_color}" opacity="0" pointer-events="none"/>'
            f'  </svg>'
            f'  </div>'
            f'</div>'
        )

    # v1.8 T5: drawdown sparkline inset. Sits directly beneath the alpha
    # sparkline so the eye can compare "how often were we ahead of SPY?" with
    # "how deep are our drawdowns?" at a glance. Same fixed-viewBox approach
    # for crisp scaling. Single red polyline + light area fill underneath; the
    # current DD pill mirrors the alpha latest pill on the right.
    dd_series = compute_drawdown_series(basket)
    if dd_series.empty or len(dd_series) < 5:
        dd_sparkline_html = ""
    else:
        DW, DH = 240, 30
        dpad_x, dpad_y = 0, 3   # x inset via .spark-plot CSS wrapper (match chart)
        dd_vals = dd_series.tolist()
        # All values <= 0. Y axis goes from 0 at the top to min(dd) at the bottom.
        dd_min = min(dd_vals + [0.0])
        dd_range = max(abs(dd_min), 1e-9)
        nd = len(dd_vals)
        def _dy(v: float) -> float:
            # v in [dd_min, 0]; 0 -> top, dd_min -> bottom.
            return dpad_y + (-v) / dd_range * (DH - 2 * dpad_y)
        def _dx(i: int) -> float:
            if _spark_dom_start is None or _spark_dom_end is None:
                return dpad_x + i / max(nd - 1, 1) * (DW - 2 * dpad_x)
            f = _date_fraction(dd_series.index[i], _spark_dom_start, _spark_dom_end)
            return dpad_x + f * (DW - 2 * dpad_x)
        dd_dates = [d.strftime("%Y-%m-%d") for d in dd_series.index]
        dd_points = " ".join(f"{_dx(i):.1f},{_dy(v):.2f}" for i, v in enumerate(dd_vals))
        # Filled area: polyline + bottom-right + bottom-left corners.
        floor_y = DH - dpad_y
        dd_area = (f"M {_dx(0):.1f},{floor_y:.2f} "
                   + " ".join(f"L {_dx(i):.1f},{_dy(v):.2f}" for i, v in enumerate(dd_vals))
                   + f" L {_dx(nd - 1):.1f},{floor_y:.2f} Z")
        dd_latest = dd_vals[-1]
        dd_worst = min(dd_vals)
        dd_dates_attr = ",".join(dd_dates)
        dd_values_attr = ",".join(f"{v:.3f}" for v in dd_vals)
        dd_sparkline_html = (
            f'<div class="dd-sparkline-wrap" id="dd-sparkline-wrap">'
            f'  <div class="dd-sparkline-head spark-plot">'
            f'    <span class="dd-sparkline-label">Drawdown from prior peak '
            f'<span class="dd-sparkline-meta">&middot; worst {dd_worst:+.1f}%</span></span>'
            f'    <span class="dd-sparkline-latest" id="dd-sparkline-latest" '
            f'          data-default-text="{dd_latest:+.1f}%">{dd_latest:+.1f}%</span>'
            f'  </div>'
            f'  <div class="spark-plot">'
            f'  <svg class="dd-sparkline" id="dd-sparkline-svg" '
            f'       viewBox="0 0 {DW} {DH}" preserveAspectRatio="none" '
            f'       data-dates="{dd_dates_attr}" data-values="{dd_values_attr}" '
            f'       data-domain-start="{_spark_dom_iso}" data-domain-end="{_spark_dom_end_iso}">'
            f'    <path d="{dd_area}" class="dd-sparkline-fill"/>'
            f'    <polyline points="{dd_points}" fill="none" stroke="var(--down)" '
            f'              stroke-width="1.2" stroke-linejoin="round"/>'
            f'    <line class="dd-cross" x1="0" y1="0" x2="0" y2="{DH}" '
            f'          stroke="var(--text-dim)" stroke-width="0.5" opacity="0" pointer-events="none"/>'
            f'    <circle class="dd-dot" cx="0" cy="0" r="2.2" '
            f'            fill="var(--down)" opacity="0" pointer-events="none"/>'
            f'  </svg>'
            f'  </div>'
            f'</div>'
        )

    if not contrib.empty:
        best_contrib_name = contrib.iloc[0].name
        best_contrib_pp = float(contrib.iloc[0].contribution_pp)
        best_contrib_wt = float(contrib.iloc[0].weight)
        worst_contrib_name = contrib.iloc[-1].name
        worst_contrib_pp = float(contrib.iloc[-1].contribution_pp)
        worst_contrib_wt = float(contrib.iloc[-1].weight)
    else:
        best_contrib_name = worst_contrib_name = "&mdash;"
        best_contrib_pp = worst_contrib_pp = 0.0
        best_contrib_wt = worst_contrib_wt = 0.0

    # --- Replacement stats (replace Basket return + vs SPY in the stat strip) ---
    # 1. Win rate — share of closed positions that ended profitable
    closed_positions = returns[returns.status == "closed"] if not returns.empty else returns.iloc[0:0]
    n_closed_total = len(closed_positions)
    n_wins = int((closed_positions["total_pct"] > 0).sum()) if n_closed_total else 0
    win_rate = (n_wins / n_closed_total * 100) if n_closed_total else 0.0

    # T6 / v2.4: win/loss magnitude ratio, now EQUAL-WEIGHT. Each closed
    # position contributes its RETURN (total_pct, %) rather than a £ amount, so
    # no high-priced name dominates and nothing about position size leaks into
    # the UI. Ratio = avg win % / avg loss %; dimensionless and currency-neutral.
    avg_win_pct = 0.0
    avg_loss_pct = 0.0      # stored as a negative number
    win_loss_ratio: float | None = None
    if not closed_positions.empty:
        pnl = closed_positions["total_pct"]
        wins   = pnl[pnl > 0]
        losses = pnl[pnl < 0]
        avg_win_pct  = float(wins.mean())   if len(wins)   > 0 else 0.0
        avg_loss_pct = float(losses.mean()) if len(losses) > 0 else 0.0
        if avg_loss_pct < 0 and len(wins) > 0:
            win_loss_ratio = avg_win_pct / abs(avg_loss_pct)
        elif len(wins) > 0 and len(losses) == 0:
            win_loss_ratio = float("inf")
        elif len(losses) > 0 and len(wins) == 0:
            win_loss_ratio = 0.0
    if win_loss_ratio is None:
        wlr_value_str = "&mdash;"
        wlr_cls = "dim"
        wlr_meta_str = "no closed positions"
    elif win_loss_ratio == float("inf"):
        wlr_value_str = "&infin;"
        wlr_cls = "pos"
        wlr_meta_str = "no losers yet"
    else:
        wlr_value_str = f"{win_loss_ratio:.2f}&times;"
        if win_loss_ratio >= 1.5:
            wlr_cls = "pos"
        elif win_loss_ratio < 1:
            wlr_cls = "neg"
        else:
            wlr_cls = ""
        wlr_meta_str = (f"{avg_win_pct:+.1f}% avg win &middot; "
                        f"{avg_loss_pct:+.1f}% avg loss")

    # 2. Equal-weight average analyst upside across open positions. Uses the
    #    analyst cache directly (native ccy for both target and current_price,
    #    so the ratio is currency-neutral).
    open_positions = returns[returns.status == "open"] if not returns.empty else returns.iloc[0:0]
    upside_sum = 0.0
    upside_weight_sum = 0.0
    n_open_covered = 0
    if analyst is not None and not analyst.empty:
        for tkr, r in open_positions.iterrows():
            if tkr not in analyst.index:
                continue
            a = analyst.loc[tkr]
            target = a.get("target_mean")
            cur = a.get("current_price")
            if (target is None or pd.isna(target) or target <= 0
                or cur is None or pd.isna(cur) or cur <= 0):
                continue
            u = (float(target) / float(cur) - 1) * 100
            w = float(r.weight) if r.weight else 1.0
            upside_sum += u * w
            upside_weight_sum += w
            n_open_covered += 1
    avg_upside = (upside_sum / upside_weight_sum) if upside_weight_sum > 0 else 0.0

    # 3. Max drawdown on the basket NAV series. The basket values are already
    #    % returns over baseline, so convert to multipliers, take cummax, and
    #    measure each point's drop from that running peak.
    #    (Stays computed even though the hero card now shows Sharpe instead --
    #    T8's stats registry will offer Max DD as an opt-in choice.)
    max_drawdown = 0.0
    max_drawdown_date_str = ""
    if not basket.empty:
        mult = 1 + basket / 100.0
        running_peak = mult.cummax()
        dd_series = (mult / running_peak - 1) * 100
        max_drawdown = float(dd_series.min())
        if max_drawdown < 0:
            max_drawdown_date_str = pd.Timestamp(dd_series.idxmin()).strftime("%d %b %y")

    # T5: weekly-cadence Sharpe ratio.
    #   - Project convention: risk-free rate = 0 (matches the implicit TWR
    #     assumption used elsewhere in the dashboard).
    #   - Weekly log returns from W-FRI resample; std handles volatility.
    #   - Annualized by sqrt(52). Daily (sqrt(252)) would mechanically yield
    #     a higher number; we picked weekly to match the rest of the dashboard.
    #   - Suppressed when fewer than ~4 weekly returns exist (too few obs for
    #     std to be meaningful -- shows "--" instead of a misleading huge value).
    sharpe_ratio: float | None = None
    if not basket.empty:
        weekly_mult = (1 + basket / 100.0).resample("W-FRI").last().dropna()
        if len(weekly_mult) >= 5:
            weekly_log_rets = np.log(weekly_mult).diff().dropna()
            std = float(weekly_log_rets.std())
            if std > 0:
                sharpe_ratio = (float(weekly_log_rets.mean()) / std) * (52 ** 0.5)
    if sharpe_ratio is None:
        sharpe_value_str = "&mdash;"
        sharpe_cls = "dim"
        sharpe_meta_str = "needs &geq; 4 weeks of data"
    else:
        sharpe_value_str = f"{sharpe_ratio:.2f}"
        if sharpe_ratio >= 1:
            sharpe_cls = "pos"
        elif sharpe_ratio < 0:
            sharpe_cls = "neg"
        else:
            sharpe_cls = ""
        sharpe_meta_str = "vol-adjusted &middot; weekly &times; &radic;52 &middot; full history"

    # T8: Hero stats registry. Renders all 10 stat cards server-side; CSS hides
    # the ones not in the user's selection. Default selection matches what
    # T5/T6 left visible (annualized, sharpe, win_rate, win_loss_ratio,
    # avg_upside). User can pick a different 5 (or up to 10) via edit-mode.
    #
    # Each entry is (slug, label, value_html, value_class, meta_html) -- a
    # closure-free flat tuple so the registry stays a pure data structure.
    # ann_pct only exists when years_elapsed >= 0.25; provide a fallback.
    _ann_value_html = "&mdash;"
    _ann_meta = "needs &geq; 3 months of data"
    _ann_cls = "dim"
    if years_elapsed >= 0.25 and basket_final > -100:
        _ann_pct = ((1 + basket_final / 100) ** (1 / years_elapsed) - 1) * 100
        _ann_value_html = f"{_ann_pct:+.1f}%"
        _ann_cls = "pos" if _ann_pct >= 0 else "neg"
        _ann_meta = f"over {years_elapsed:.1f} years &middot; geometric"

    # L5: the contributor "weight" is just 1 unit in equal mode, so a "£1 basis"
    # tail invites a position-size misread. Only append a £ basis in value mode,
    # where the weight is a real cost basis; otherwise show just the name.
    if WEIGHT_MODE == "value":
        _top_contrib_meta = f"{best_contrib_name} &middot; {BASE_SYMBOL}{best_contrib_wt:,.0f} basis"
        _top_detract_meta = f"{worst_contrib_name} &middot; {BASE_SYMBOL}{worst_contrib_wt:,.0f} basis"
    else:
        _top_contrib_meta = best_contrib_name
        _top_detract_meta = worst_contrib_name

    _stats_registry = [
        # slug,             label,                value_html,                    cls,                                meta_html
        ("total_return",    "Total return",       f"{basket_final:+.1f}%",       _cls(basket_final),                 f"equal-weight &middot; since {first_purchase_str}"),
        ("annualized",      "Annualized",         _ann_value_html,               _ann_cls,                           _ann_meta),
        ("sharpe",          "Sharpe",             sharpe_value_str,              sharpe_cls,                         sharpe_meta_str),
        ("win_rate",        "Win rate",           f"{win_rate:.0f}%",            _cls(win_rate - 50),                f"{n_wins} of {n_closed_total} closed wins"),
        ("win_loss_ratio",  "Win / loss ratio",   wlr_value_str,                 wlr_cls,                            wlr_meta_str),
        ("avg_upside",      "Avg analyst upside", f"{avg_upside:+.1f}%",         _cls(avg_upside),                   f"{n_open_covered} of {n_open} open covered"),
        ("max_drawdown",    "Max drawdown",       f"{max_drawdown:.1f}%",        "neg",                              ("trough: " + max_drawdown_date_str) if max_drawdown_date_str else "no drawdown yet"),
        ("top_contributor", "Top contributor",    f"{best_contrib_pp:+.1f} pp",  "pos",                              _top_contrib_meta),
        ("top_detractor",   "Top detractor",      f"{worst_contrib_pp:+.1f} pp", "neg",                              _top_detract_meta),
        ("positions_open",  "Open positions",     f"{n_open}",                   "",                                 f"{n_closed} closed alongside"),
    ]

    # v2.4: 'annualized' dropped from the default set — it duplicates the return
    # the hero chart already shows. Still selectable in the stats picker.
    HERO_STATS_DEFAULT = ["sharpe", "win_rate", "win_loss_ratio", "avg_upside"]
    _stats_default_csv = ",".join(HERO_STATS_DEFAULT)
    _stats_all_csv = ",".join(slug for (slug, *_rest) in _stats_registry)

    # Render each stat card. data-stat-default-shown="1" lets the CSS-only
    # default state apply BEFORE the JS layout-state hydration runs (so the
    # page doesn't flash all 10 then collapse to 5 on first paint).
    _stat_cards = []
    for (slug, label, val_html, val_cls, meta_html) in _stats_registry:
        is_default = "1" if slug in HERO_STATS_DEFAULT else "0"
        _stat_cards.append(
            f'<div class="stat" data-stat="{slug}" data-stat-default-shown="{is_default}">'
            f'<div class="stat-bar">'
            f'<span class="stat-grip" aria-hidden="true">&#9776;</span>'
            f'<span class="stat-bar-name">{label}</span>'
            f'<label class="stat-vis"><input type="checkbox" class="stat-vis-cb"{" checked" if is_default == "1" else ""}>'
            f'<span class="stat-vis-txt">{"Shown" if is_default == "1" else "Hidden"}</span></label>'
            f'</div>'
            f'<div class="stat-label">{label}</div>'
            f'<div class="stat-value {val_cls}">{val_html}</div>'
            f'<div class="stat-meta">{meta_html}</div>'
            f'</div>'
        )
    stats_cards_html = "\n    ".join(_stat_cards)

    data_dict = build_data_payload(returns, prices, meta, contrib, signals,
                                   prices_native, returns_native,
                                   quant_metrics=quant_metrics,
                                   ticker_news=ticker_news)
    for tkr, entry in watchlist_payload.items():
        if tkr not in data_dict:
            data_dict[tkr] = entry
    # v2.0 lazy-modal split: demo.html stays self-contained (everything inline).
    # docs/index.html ships light fields inline + a sidecar payload.json with
    # modal-only fields, fetched on requestIdleCallback after first paint.
    build_timestamp = int(time.time())
    # v2.3 since-last-look: tiny snapshot the page JS diffs against localStorage.
    _idea_tickers = [o["ticker"] for o in bigbrain_observations
                     if o.get("ownership") == "idea"]
    _pred_probs = {r["theme"]: round(float(r["probability"]), 1)
                   for r in (prediction_rows or [])}
    # v2.7: also track the value-screen membership so "since your last visit"
    # surfaces newly-qualifying value names (parallel to new Big Brain ideas).
    _value_tickers = [r["ticker"] for r in (value_rows or [])]
    last_look_json = _json_for_script({
        "build_id": build_timestamp,
        "basket_return": round(float(basket_final), 2),
        "idea_tickers": _idea_tickers,
        "value_tickers": _value_tickers,
        "predictions": _pred_probs,
    }, separators=(",", ":"))
    if demo_mode:
        data_json = _json_for_script(data_dict, separators=(",", ":"))
        heavy_url_js = "null"
    else:
        light, heavy = split_payload(data_dict)
        HEAVY_JSON.parent.mkdir(parents=True, exist_ok=True)
        HEAVY_JSON.write_text(           # fetched sidecar, not inlined -> plain dumps
            json.dumps(heavy, separators=(",", ":")), encoding="utf-8"
        )
        heavy_kb = HEAVY_JSON.stat().st_size / 1024
        print(f"Wrote {HEAVY_JSON} ({heavy_kb:.1f} KB) "
              f"-- lazy-modal sidecar, {len(heavy)} tickers")
        data_json = _json_for_script(light, separators=(",", ":"))
        heavy_url_js = _json_for_script(f"data/payload.json?v={build_timestamp}")
    # JSON-encode the Worker URL so quoting is always correct in the embedded JS,
    # and an unset URL becomes the literal `""` (falsy in the JS branch).
    news_worker_url_js = _json_for_script(NEWS_WORKER_URL or "")
    _portfolio_payload = build_portfolio_payload(basket, bench, first_purchase,
                                                 fx=fx, nasdaq=nasdaq_series)
    has_nasdaq = bool(_portfolio_payload.get("nasdaq", {}).get("values"))
    hero_legend_html = _hero_legend_html(has_nasdaq)
    portfolio_json = _json_for_script(_portfolio_payload, separators=(",", ":"))
    # T11/T12/T14/T15: aux payload for click-to-expand drill-down modals.
    # Reuses diversification_data (computed below) for the pair list -- this
    # block depends on it so we compute it inline first, then pass it.
    aux_payload = build_aux_payload(
        returns, prices, meta, universe_outlook,
        diversification_data=diversification_data,
        basket_first_date=first_purchase,
        log_tickers=log_tickers_set,
    )
    aux_json = _json_for_script(aux_payload, separators=(",", ":"))
    # v1.9 Pocket Lesson: bake the curated tip pool into the page payload.
    pocket_lessons_json = _json_for_script(POCKET_LESSONS, separators=(",", ":"), ensure_ascii=False)
    # v2.1 Quiz: 50-question pool, inline (~12 KB) so it's available immediately
    # on toolbar-button click without waiting for HEAVY fetch.
    quiz_pool_json = _json_for_script(QUIZ_POOL, separators=(",", ":"), ensure_ascii=False)

    # ---- Customizable module stack -----------------------------------------
    # Each top-level content section is wrapped as a draggable/hideable
    # "module". Order + visibility are persisted client-side (localStorage) by
    # setupLayout() in the page script, so the order defined here is only the
    # default that ships to anyone who clones the repo. Empty sections (e.g.
    # the analyst panel when there are no candidates) are dropped so we never
    # wrap a blank module.
    toolbar_html = render_toolbar(0, n_total, returns=returns, meta=meta)
    holdings_html = f'<section class="panel active" id="panel-0">{toolbar_html}{table_html}</section>'
    # Rating-moves panel sits directly under Re-entry ideas in the default
    # order -- both are analyst-signal modules, so they read as a pair.
    rating_moves_html = render_rating_moves(rating_moves or [], prior_analyst_exists,
                                            baseline_date=rating_baseline_date,
                                            baseline_seeded=rating_baseline_seeded)
    _module_defs = [
        ("bigbrain", "Big Brain says", bigbrain_html),
        ("value_screen", "Value screen", value_html),
        ("outlook", "Industry outlook", industry_html),
        ("news", "News", news_html),
        ("predictions", "Market expectations", market_expectations_html),
        ("holdings", "Holdings", holdings_html),
        ("analyst", "Re-entry ideas", analyst_html),
        ("watchlist", "Watchlist", watchlist_html),
        ("rating_moves", "Rating moves", rating_moves_html),
        ("detractors", "Exit strategy", detractors_html),
        ("diversification", "Basket diversification", diversification_html),
        ("quadrant", "Signal map", quadrant_html),
        ("attribution", "Industry attribution", attribution_html),
        ("ccy_exposure", "Currency exposure", ccy_exposure_html),
        ("regret", "Regret tracker", regret_html),
    ]
    _modules = [(mid, label, html) for (mid, label, html) in _module_defs if html and html.strip()]

    def _wrap_module(mid: str, label: str, inner: str) -> str:
        return (
            f'<div class="module" data-module="{mid}">'
            f'<div class="module-bar">'
            f'<span class="module-grip" aria-hidden="true">⋮⋮</span>'
            f'<span class="module-name">{label}</span>'
            f'<label class="module-vis"><input type="checkbox" class="module-vis-cb" checked>'
            f'<span class="module-vis-txt">Shown</span></label>'
            f'</div>{inner}</div>'
        )

    default_order_csv = ",".join(mid for (mid, _, _) in _modules)
    module_stack_html = "\n".join(_wrap_module(mid, label, html) for (mid, label, html) in _modules)

    # Top-of-page banner shown only on the public/demo build (when log.xlsx
    # isn't present). Makes "this is sample data" unmissable for visitors.
    if watchlist_only:
        demo_banner_html = (
            '<div class="demo-banner">'
            '<span class="demo-banner-tag">WATCHLIST</span>'
            '<span class="demo-banner-text">Tracking a watchlist &mdash; no positions; each ticker '
            'is shown equal-weight from the start of its price window. '
            '<a href="https://github.com/newpov/stocks-dashboard" target="_blank" rel="noopener noreferrer">'
            'Fork the repo</a> to track your own.</span>'
            '</div>'
        )
    elif demo_mode:
        demo_banner_html = (
            '<div class="demo-banner">'
            '<span class="demo-banner-tag">DEMO MODE</span>'
            '<span class="demo-banner-text">This is a sample portfolio for illustration &mdash; not real holdings. '
            '<a href="https://github.com/newpov/stocks-dashboard" target="_blank" rel="noopener noreferrer">'
            'Fork the repo</a> to run it with your own broker export.</span>'
            '</div>'
        )
    else:
        demo_banner_html = ''

    # Hero eyebrow + title adapt to watchlist-only mode (no positions / P&L).
    if watchlist_only:
        hero_eyebrow = (f'{n_open} tracked <span class="dot">&middot;</span> watchlist '
                        f'<span class="dot">&middot;</span> in {BASE_CCY}')
        hero_h1 = 'Your <em>watchlist</em>'
    else:
        hero_eyebrow = (f'{n_open} open <span class="dot">&middot;</span> {n_closed} closed '
                        f'<span class="dot">&middot;</span> first buy {first_purchase_str} '
                        f'<span class="dot">&middot;</span> in {BASE_CCY}')
        # Dynamic so a fork shows its own first-buy month (e.g. "Dec '25"),
        # not the author's hardcoded start. (v2.5 #7)
        _since_label = first_purchase.strftime("%b") + " &rsquo;" + first_purchase.strftime("%y")
        hero_h1 = f'The basket since <em>{_since_label}</em>'

    # SortableJS: either reference the vendored file (normal docs/index.html
    # build, served alongside docs/vendor/) or inline the whole library
    # (standalone demo.html — file:// users have no vendor/ adjacent).
    if sortable_inline_js:
        sortable_script_tag = f'<script>{sortable_inline_js}</script>'
    else:
        sortable_script_tag = '<script src="vendor/Sortable.min.js"></script>'

    # Build-health footer: surfaces silent yfinance failures (e.g. delisted
    # tickers returning 404) that the build keeps going through but would
    # otherwise only appear in stderr.
    if build_health:
        bh_ok      = build_health.get("succeeded", 0)
        bh_total   = build_health.get("attempted", 0)
        bh_held    = build_health.get("n_held", bh_total)
        bh_watch   = build_health.get("n_watch_only", 0)
        bh_failed  = build_health.get("failed", []) or []
        bh_retries = build_health.get("retries_recovered", 0)
        bh_seconds = build_health.get("build_seconds", 0)
        bh_status_cls = "bh-fail" if bh_failed else "bh-ok"
        bh_failed_html = (
            f' &middot; <span class="bh-fail">failed: {", ".join(bh_failed)}</span>'
            if bh_failed else ""
        )
        bh_retry_html = f" &middot; {bh_retries} retry-recovered" if bh_retries else ""
        # v1.9 D1: render breakdown so "187/187" reads as "185 held + 2 watch".
        # The basket eyebrow / position count elsewhere uses `n_held` -- this
        # makes the two numbers reconcile at a glance.
        if bh_watch > 0:
            bh_count_html = (
                f'<span class="{bh_status_cls}">{bh_ok}/{bh_total}</span> tickers '
                f'<span class="bh-breakdown">({bh_held} held + {bh_watch} watch-only)</span>'
            )
        else:
            bh_count_html = f'<span class="{bh_status_cls}">{bh_ok}/{bh_total}</span> tickers'
        build_health_html = (
            f'<div class="build-health">'
            f'Build: {bh_count_html}'
            f"{bh_retry_html} &middot; {bh_seconds}s{bh_failed_html}"
            f'</div>'
        )
    else:
        build_health_html = ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<!-- v1.9: inline SVG favicon so we silence the favicon.ico 404 without
     adding a binary file to the repo. Minimal line-chart glyph in the
     accent color -- looks reasonable at 16x16 in the tab. -->
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='6' fill='%230b0e17'/%3E%3Cpolyline points='5,22 11,17 16,20 21,11 27,6' fill='none' stroke='%23f59e0b' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E">
<title>Portfolio &middot; since {first_purchase_str}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Geist+Mono:wght@400;500&family=Geist:wght@400;500;600&family=Instrument+Serif:ital@0;1&display=swap" rel="stylesheet">
<style>
  :root {{
    /* Default — current dark + amber. Overridden by body.palette-* below. */
    --ink:#0b0e17; --ink-soft:#11151f; --surface:#161b27; --surface-2:#1d2330;
    --border:#232a3d; --text:#ece8e0; --text-2:#b4b9c4; --text-dim:#6b7185;
    --up:#34d399; --down:#f87171; --accent:#f59e0b;
    --font-display:"Instrument Serif",Georgia,serif;
    --font-ui:"Geist",system-ui,-apple-system,sans-serif;
    --font-mono:"Geist Mono","JetBrains Mono","SF Mono",Consolas,monospace;
  }}
  /* Palette A — Softer dark (lighter version of default, easier on eyes) */
  body.palette-softdark{{
    --ink:#1a1d29; --ink-soft:#22263a; --surface:#262b3a; --surface-2:#2f3548;
    --border:#3a4156; --text:#f1f5f9; --text-2:#cbd5e1; --text-dim:#7d8aa8;
    --up:#34d399; --down:#fb7185; --accent:#fbbf24;
  }}
  /* Palette B — Editorial light (FT/NYT vibe, prints well, dense-table friendly) */
  body.palette-light{{
    --ink:#fffaf0; --ink-soft:#fff5e6; --surface:#ffffff; --surface-2:#f5efe2;
    --border:#e2d8c2; --text:#1f2937; --text-2:#4a5568; --text-dim:#94816a;
    --up:#047857; --down:#b91c1c; --accent:#ad1f17;
  }}
  /* Palette C — Bloomberg amber-on-black (terminal aesthetic) */
  body.palette-bloomberg{{
    --ink:#000000; --ink-soft:#0a0a0a; --surface:#111111; --surface-2:#1a1a1a;
    --border:#2a2a2a; --text:#ffb800; --text-2:#cc9300; --text-dim:#806000;
    --up:#00ff66; --down:#ff3030; --accent:#ffb800;
  }}
  /* Palette toggle pill cluster (top-right of header) */
  /* Demo-mode banner: rendered above everything on public builds (log.xlsx
     absent → CI auto-falls-through to transactions.csv). Stays sticky to the
     top of the viewport so it's visible even while scrolling deep modules. */
  .demo-banner{{
    position:sticky;top:0;z-index:60;
    background:linear-gradient(180deg, #f59e0b 0%, #d97706 100%);
    color:#1a1a24;padding:9px 18px;
    font-family:var(--font-mono);font-size:11.5px;line-height:1.4;
    text-align:center;letter-spacing:0.02em;
    border-bottom:1px solid rgba(0,0,0,0.25);
    box-shadow:0 2px 8px rgba(0,0,0,0.25);
  }}
  .demo-banner-tag{{font-weight:700;letter-spacing:0.12em;margin-right:10px;
    padding:1.5px 7px;background:rgba(26,26,36,0.18);border-radius:3px;
    font-size:10.5px;text-transform:uppercase}}
  .demo-banner-text{{font-weight:500}}
  .demo-banner a{{color:inherit;text-decoration:underline;font-weight:600}}
  .demo-banner a:hover{{text-decoration:none}}
  @media (max-width:700px){{
    .demo-banner{{padding:8px 12px;font-size:10.5px}}
    .demo-banner-tag{{display:block;margin:0 0 4px 0}}
  }}

  /* Top-right control cluster: layout editor + palette toggle */
  .topbar{{
    position:absolute;top:18px;right:28px;z-index:30;
    display:flex;gap:8px;align-items:center;
  }}
  .layout-toggle,.layout-reset{{
    background:var(--surface);border:1px solid var(--border);color:var(--text-dim);
    padding:4px 9px;border-radius:5px;cursor:pointer;font-family:var(--font-mono);
    font-size:10px;letter-spacing:0.06em;text-transform:uppercase;transition:all 0.15s;
  }}
  .layout-toggle:hover,.layout-reset:hover{{color:var(--text);border-color:var(--text-dim)}}
  .layout-toggle.active{{background:var(--accent);color:var(--ink);
    border-color:var(--accent);font-weight:600}}
  /* v2.1: icon-only variant of the topbar buttons. Fixed-size square button
     with the SVG icon centered; tooltip slides in below on hover. Saves
     horizontal space (28px vs ~80px+ per text label) so the topbar fits
     more controls without crowding. Keeps the .layout-toggle base styling
     so .active state (accent bg) still works for toggle buttons. */
  .icon-btn{{
    width:36px;height:32px;padding:0;
    display:inline-flex;align-items:center;justify-content:center;
    position:relative;
  }}
  /* Re-assert the [hidden] override -- our .icon-btn display:inline-flex has
     higher specificity than the UA default [hidden]{{display:none}}, so the
     Reset button would stay visible without this. */
  .icon-btn[hidden]{{display:none}}
  .icon-btn svg{{width:18px;height:18px;display:block;stroke:currentColor}}
  /* Palette icon uses fill (currentColor) since it's a solid swatch
     representing the active accent. */
  .palette-cycle-btn svg{{color:var(--accent)}}
  /* Tooltip: data-tooltip drives a CSS pseudo-element. Slides in 3px on
     hover with a 150ms fade. position:fixed-ish via absolute against the
     .icon-btn -- which is already position:relative. */
  .icon-btn[data-tooltip]::after{{
    content:attr(data-tooltip);
    position:absolute;top:calc(100% + 6px);left:50%;
    transform:translate(-50%,-3px);
    background:var(--surface-2);border:1px solid var(--border);
    border-radius:4px;padding:4px 9px;
    font-family:var(--font-mono);font-size:10px;letter-spacing:0.06em;
    text-transform:uppercase;color:var(--text);white-space:nowrap;
    opacity:0;pointer-events:none;
    transition:opacity 0.15s ease,transform 0.15s ease;
    z-index:50;
  }}
  .icon-btn:hover[data-tooltip]::after,
  .icon-btn:focus-visible[data-tooltip]::after{{
    opacity:1;transform:translate(-50%,0);
  }}
  /* Desktop-view toggle: only visible on narrow viewports (where the mobile
     media queries activate). When toggled on (`body.force-desktop`), the body
     gets a min-width so the desktop layout always renders -- the page becomes
     horizontally scrollable on phones, which is the trade-off the user opts
     into when they tap this button. Wikipedia / Reddit use the same pattern. */
  .desktop-mode-btn{{display:none}}
  @media (max-width:900px){{
    .desktop-mode-btn{{display:inline-block}}
  }}
  body.force-desktop{{min-width:1100px}}
  body.force-desktop .desktop-mode-btn{{display:inline-block !important}}
  /* v2.1: when force-desktop pushes the page wider than the viewport, the
     topbar (position:absolute right:28px) lands off-screen because right is
     measured from the now-1100px container's edge. Switch to position:fixed
     so the controls stay at top-right of the VIEWPORT regardless of scroll. */
  body.force-desktop .topbar{{position:fixed;top:18px;right:28px}}
  /* Pulse halo around the Edit-layout button while the discovery hint is up.
     box-shadow keeps it paint-only — the button's layout box never grows so
     nothing nearby reflows. */
  @keyframes edit-pulse{{
    0%,100%{{box-shadow:0 0 0 0 rgba(245,158,11,0.55)}}
    50%    {{box-shadow:0 0 0 8px rgba(245,158,11,0)}}
  }}
  .layout-toggle.pulse{{
    animation:edit-pulse 1.8s ease-in-out infinite;
    border-color:var(--accent);color:var(--text);
  }}
  /* One-time tooltip; position is JS-set via editBtn.getBoundingClientRect()
     when shown, so the arrow always points at the actual Edit-layout button
     regardless of topbar contents or screen size. CSS provides the visual
     dressing; the `position:fixed` + top/left values are overwritten on show. */
  .edit-tooltip{{
    position:fixed;z-index:35;
    background:var(--surface-2);border:1px solid var(--accent);
    border-radius:6px;padding:10px 14px;max-width:260px;
    font-family:var(--font-mono);font-size:11px;line-height:1.5;color:var(--text);
    box-shadow:0 6px 16px rgba(0,0,0,0.30);cursor:pointer;
  }}
  .edit-tooltip::before{{
    content:'';position:absolute;top:-7px;left:18px;
    width:12px;height:12px;background:var(--surface-2);
    border-top:1px solid var(--accent);border-left:1px solid var(--accent);
    transform:rotate(45deg);
  }}
  .edit-tooltip strong{{color:var(--accent);font-weight:600}}
  .edit-tooltip-dismiss{{display:block;margin-top:6px;font-size:9.5px;
    color:var(--text-dim);text-transform:uppercase;letter-spacing:0.06em}}
  @media (max-width:700px){{
    .edit-tooltip{{right:8px;left:8px;max-width:none;top:48px}}
  }}
  .palette-toggle{{display:flex;gap:4px;
    font-family:var(--font-mono);font-size:10px;letter-spacing:0.06em;
  }}
  .palette-toggle button{{
    background:var(--surface);border:1px solid var(--border);color:var(--text-dim);
    padding:4px 9px;border-radius:5px;cursor:pointer;font-family:inherit;font-size:inherit;
    text-transform:uppercase;letter-spacing:inherit;transition:all 0.15s;
  }}
  .palette-toggle button:hover{{color:var(--text);border-color:var(--text-dim)}}
  .palette-toggle button.active{{background:var(--accent);color:var(--ink);
    border-color:var(--accent);font-weight:600}}

  /* v1.9 Pocket Lesson: the topbar toggle uses .layout-toggle base styles
     (already defined) plus an aria-pressed=true state for the "on" visual.
     The card itself sits as a slim block under the topbar. */
  .pocket-lesson-btn[aria-pressed="true"]{{
    background:var(--accent);color:var(--ink);border-color:var(--accent);font-weight:600;
  }}
  /* Card is collapsed-by-default (max-height 0). The .is-open class triggers
     the slide-down: margin + max-height + opacity all transition together so
     the rest of the dashboard is smoothly pushed down (or back up). We
     deliberately don't use the [hidden] attribute -- it short-circuits to
     display:none which can't transition. aria-hidden carries the
     accessibility signal instead. */
  .pocket-lesson-wrap{{
    margin:0;max-height:0;overflow:hidden;opacity:0;
    transition:max-height 0.35s ease, margin 0.35s ease, opacity 0.25s ease;
  }}
  .pocket-lesson-wrap.is-open{{
    margin:14px 0 18px;max-height:320px;opacity:1;
  }}
  @media (prefers-reduced-motion: reduce) {{
    .pocket-lesson-wrap{{transition:none}}
  }}
  .pocket-lesson-card{{
    border:1px solid var(--border);border-left:3px solid var(--accent);
    background:linear-gradient(180deg, var(--ink-soft), var(--surface));
    border-radius:10px;padding:14px 18px;
    display:flex;flex-direction:column;gap:6px;
  }}
  .pocket-lesson-head{{
    display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;
  }}
  .pocket-lesson-eyebrow{{
    font-family:var(--font-mono);font-size:9.5px;letter-spacing:0.14em;
    text-transform:uppercase;color:var(--accent);font-weight:600;
  }}
  .pocket-lesson-title{{
    font-family:var(--font-ui);font-size:14.5px;font-weight:600;color:var(--text);
  }}
  .pocket-lesson-body{{
    margin:0;font-family:var(--font-ui);font-size:13px;line-height:1.55;
    color:var(--text-dim);max-width:78ch;
  }}
  .pocket-lesson-actions{{
    display:flex;align-items:center;gap:12px;margin-top:4px;
  }}
  .pocket-lesson-next{{
    background:transparent;border:1px solid var(--border);color:var(--text-dim);
    padding:4px 11px;border-radius:5px;cursor:pointer;
    font-family:var(--font-mono);font-size:10px;letter-spacing:0.06em;text-transform:uppercase;
    transition:all 0.15s;
  }}
  .pocket-lesson-next:hover{{color:var(--text);border-color:var(--accent)}}
  .pocket-lesson-counter{{
    font-family:var(--font-mono);font-size:9.5px;letter-spacing:0.06em;
    color:var(--text-dim);opacity:0.7;
  }}
  /* v1.9 #7: category filter chips + per-tip category pill. The pill sits
     in the head row as a small badge; the filter row above the actions lets
     users narrow the pool. Active chip uses the accent color. */
  .pocket-lesson-cat-pill{{
    font-family:var(--font-mono);font-size:9px;letter-spacing:0.08em;
    text-transform:uppercase;color:var(--accent);
    border:1px solid var(--accent);border-radius:3px;padding:1px 6px;
    opacity:0.85;
  }}
  .pocket-lesson-filters{{
    display:flex;flex-wrap:wrap;gap:4px;margin-top:6px;
  }}
  .pocket-lesson-chip{{
    background:transparent;border:1px solid var(--border);color:var(--text-dim);
    padding:3px 8px;border-radius:4px;cursor:pointer;
    font-family:var(--font-mono);font-size:9.5px;letter-spacing:0.06em;text-transform:uppercase;
    transition:all 0.12s;
  }}
  .pocket-lesson-chip:hover{{color:var(--text);border-color:var(--text-dim)}}
  .pocket-lesson-chip.active{{background:var(--accent);color:var(--ink);border-color:var(--accent);font-weight:600}}

  /* v2.1 Quiz modal. Reuses the ticker modal's overlay/animation scheme so
     the open/close feel matches. Sized to be skim-friendly (max-width 640px,
     auto height up to viewport) since each question is short. The "correct
     answer" flash and score pop are CSS keyframes triggered by JS class
     toggles -- pure paint, no layout impact. */
  .quiz-modal-card{{max-width:640px;padding:32px 36px 26px}}
  .quiz-head{{display:flex;align-items:center;gap:10px;margin-bottom:14px}}
  .quiz-eyebrow{{font-family:var(--font-mono);font-size:10px;letter-spacing:0.18em;
    text-transform:uppercase;color:var(--text-dim);font-weight:600}}
  .quiz-cat-pill{{font-family:var(--font-mono);font-size:9.5px;letter-spacing:0.08em;
    text-transform:uppercase;color:var(--accent);
    border:1px solid var(--accent);border-radius:3px;padding:1px 6px;opacity:0.85}}
  .quiz-question{{font-family:var(--font-ui);font-size:16px;line-height:1.55;
    color:var(--text);margin:0 0 18px 0;font-weight:500}}
  .quiz-options{{display:flex;flex-direction:column;gap:8px;margin-bottom:14px}}
  .quiz-option{{background:var(--ink-soft);border:1px solid var(--border);
    color:var(--text);padding:11px 16px;border-radius:8px;cursor:pointer;
    font-family:var(--font-ui);font-size:13.5px;text-align:left;
    transition:background 0.12s,border-color 0.12s,color 0.12s;
    line-height:1.4}}
  .quiz-option:hover:not(:disabled){{border-color:var(--text-dim);background:var(--surface-2)}}
  .quiz-option:disabled{{cursor:default;opacity:0.7}}
  /* Reveal state: correct answer turns green & flashes; the picked-wrong
     answer turns red; other answers fade. */
  .quiz-option.correct{{background:rgba(52,211,153,0.18);border-color:var(--up);
    color:var(--up);opacity:1;animation:flashCorrect 0.65s ease-out}}
  .quiz-option.incorrect{{background:rgba(248,113,113,0.14);border-color:var(--down);
    color:var(--down);opacity:1}}
  .quiz-option.dimmed{{opacity:0.45}}
  @keyframes flashCorrect{{
    0%   {{background:rgba(52,211,153,0.55);transform:scale(1.0)}}
    35%  {{background:rgba(52,211,153,0.40);transform:scale(1.015)}}
    100% {{background:rgba(52,211,153,0.18);transform:scale(1.0)}}
  }}
  .quiz-reveal{{padding:12px 14px;background:var(--ink-soft);border:1px solid var(--border);
    border-radius:8px;margin-bottom:14px}}
  .quiz-reveal[hidden]{{display:none}}
  .quiz-reveal-verdict{{font-family:var(--font-mono);font-size:10.5px;
    letter-spacing:0.12em;text-transform:uppercase;font-weight:600;margin-bottom:6px}}
  .quiz-reveal-verdict.pos{{color:var(--up)}}
  .quiz-reveal-verdict.neg{{color:var(--down)}}
  .quiz-reveal-text{{font-family:var(--font-ui);font-size:12.5px;line-height:1.5;color:var(--text-2)}}
  .quiz-foot{{display:flex;align-items:center;justify-content:space-between;
    margin-top:6px;padding-top:14px;border-top:1px solid var(--border)}}
  .quiz-score{{font-family:var(--font-mono);font-size:11px;letter-spacing:0.06em;
    color:var(--text-dim);text-transform:uppercase}}
  .quiz-score-num{{color:var(--text);font-weight:600;font-size:12px;
    display:inline-block;margin-left:4px;transform-origin:center}}
  .quiz-score-num.pop{{animation:scorePop 0.5s cubic-bezier(0.34,1.56,0.64,1)}}
  @keyframes scorePop{{
    0%   {{transform:scale(1.0);color:var(--text)}}
    35%  {{transform:scale(1.35);color:var(--up)}}
    100% {{transform:scale(1.0);color:var(--text)}}
  }}
  .quiz-next{{background:var(--accent);border:1px solid var(--accent);color:var(--ink);
    padding:7px 16px;border-radius:6px;cursor:pointer;font-family:var(--font-mono);
    font-size:11px;letter-spacing:0.08em;text-transform:uppercase;font-weight:600;
    transition:opacity 0.15s,transform 0.1s}}
  .quiz-next:hover:not(:disabled){{transform:translateY(-1px)}}
  .quiz-next:disabled{{opacity:0.3;cursor:not-allowed}}
  @media (max-width:600px){{
    .quiz-modal-card{{padding:22px 18px 18px;max-width:none;border-radius:0;max-height:100vh}}
    .quiz-question{{font-size:14.5px}}
    .quiz-option{{font-size:12.5px;padding:10px 12px}}
  }}

  .container{{position:relative}}

  /* ---- Customizable module layout ---- */
  /* 2-col grid so a registered "pair" (outlook + news) can sit side-by-side
     *only when* both are visible and adjacent in the current order. Everything
     else spans full width via `grid-column: 1 / -1`. The .module-paired class
     is applied by setupLayout() in JS after every layout change (load, drag,
     hide-toggle, reset), so reordering or hiding one of the pair gracefully
     unpairs them back to full-width. */
  #module-stack{{display:grid;grid-template-columns:repeat(2,1fr);
    gap:26px;margin-top:28px;align-items:stretch}}
  #module-stack > .module{{grid-column:1 / -1}}
  #module-stack > .module.module-paired{{grid-column:span 1;
    display:flex;flex-direction:column;min-height:520px;max-height:620px}}
  #module-stack > .module.module-paired > section{{flex:1 1 auto;
    display:flex;flex-direction:column;overflow:hidden;min-height:0}}
  #module-stack > .module.module-paired .io-grid{{flex:1 1 auto;
    overflow-y:auto;min-height:0;max-height:none;grid-template-columns:1fr}}
  #module-stack > .module.module-paired .news-list{{flex:1 1 auto;
    min-height:0;max-height:none;overflow-y:auto}}
  #module-stack > .module > section{{margin:0}}
  .module-bar{{display:none}}
  body.edit-mode .module-bar{{
    display:flex;align-items:center;gap:10px;margin-bottom:10px;padding:7px 12px;
    background:var(--surface);border:1px dashed var(--border);border-radius:7px;
    font-family:var(--font-mono);font-size:11px;
  }}
  .module-grip{{cursor:grab;color:var(--text-dim);font-size:13px;line-height:1;
    letter-spacing:-2px;user-select:none}}
  body.edit-mode .module-grip:active{{cursor:grabbing}}
  .module-name{{font-weight:600;color:var(--text);text-transform:uppercase;letter-spacing:0.06em}}
  .module-vis{{margin-left:auto;display:flex;align-items:center;gap:6px;
    color:var(--text-dim);cursor:pointer;user-select:none}}
  .module-vis input{{cursor:pointer;accent-color:var(--accent)}}
  body.edit-mode .module{{outline:1px solid var(--border);outline-offset:6px;border-radius:4px}}
  .module-ghost{{opacity:.35}}
  .module-chosen .module-bar{{border-style:solid;border-color:var(--accent)}}
  /* Hidden modules vanish in normal view. In edit mode they collapse to the
     module-bar strip only — the content section is fully hidden so the page
     doesn't read as "glitched / broken" from full-width dimmed-but-still-
     rendered dead modules. The bar itself stays interactive (drag handle +
     visibility checkbox) so the user can restore or reorder a hidden module. */
  .module[data-hidden="true"]{{display:none}}
  body.edit-mode .module[data-hidden="true"]{{display:block}}
  body.edit-mode .module[data-hidden="true"] > section{{display:none}}
  body.edit-mode .module[data-hidden="true"] .module-bar{{
    opacity:.65;background:var(--surface-2);
  }}
  body.edit-mode .module[data-hidden="true"] .module-name{{text-decoration:line-through}}
  /* News list keeps an internal scroll cap now that it's full-width. */
  .module[data-module="news"] .news-list{{max-height:360px;overflow-y:auto}}

  @media (max-width:700px){{
    .topbar{{position:static;justify-content:flex-end;margin:8px 0 -8px;flex-wrap:wrap}}
    /* Collapse the module grid to a single column on narrow screens — pairs
       become full-width stacked, matching the legacy mobile behavior. */
    #module-stack{{grid-template-columns:1fr}}
    #module-stack > .module.module-paired{{grid-column:1 / -1;min-height:auto;max-height:none}}
    /* v2.4 mobile fix: a wide table (returns/holdings) forced the single grid
       track to its min-content, blowing the whole page past the viewport and
       clipping EVERY section (Big Brain included). min-width:0 lets the track
       shrink so the inner scrollers (.table-scroll / .ia-scroll) scroll
       instead; the hardcoded 2-col regret + diversification grids collapse to
       one column; and the news feed is capped to half the screen height. */
    #module-stack > .module{{min-width:0}}
    .regret{{grid-template-columns:1fr !important}}
    .regret-col,.div-card{{min-width:0}}
    .news-list{{max-height:50vh !important;overflow-y:auto}}
  }}
  *{{box-sizing:border-box}}
  html,body{{margin:0;padding:0}}
  body{{
    background:var(--ink);
    background-image:
      radial-gradient(ellipse 1200px 600px at 85% -8%,rgba(245,158,11,0.07),transparent 60%),
      radial-gradient(ellipse 900px 500px at 0% 0%,rgba(52,211,153,0.045),transparent 60%),
      radial-gradient(ellipse 700px 400px at 100% 100%,rgba(248,113,113,0.03),transparent 60%);
    background-attachment:fixed;color:var(--text);font-family:var(--font-ui);font-size:14px;
    line-height:1.5;min-height:100vh;-webkit-font-smoothing:antialiased;
  }}
  body.modal-open{{overflow:hidden}}
  .container{{max-width:1320px;margin:0 auto;padding:0 28px}}
  header{{padding:40px 0 24px}}
  .eyebrow{{color:var(--text-dim);font-size:10.5px;letter-spacing:0.22em;text-transform:uppercase;font-weight:500}}
  .eyebrow .dot{{color:var(--accent);margin:0 8px;opacity:0.8}}
  h1{{
    font-family:var(--font-display);font-size:clamp(38px,6.5vw,64px);font-weight:400;
    line-height:1.02;margin:14px 0 28px;letter-spacing:-0.01em;
  }}
  h1 em{{font-style:italic;color:var(--accent)}}

  /* Hero chart */
  .hero-chart-wrap{{
    background:linear-gradient(180deg,var(--surface) 0%,var(--ink-soft) 100%);
    border:1px solid var(--border);border-radius:14px;padding:22px 22px 14px;
    position:relative;margin-bottom:14px;
  }}
  .hero-chart-head{{
    display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:12px;gap:20px;flex-wrap:wrap;
  }}
  .hero-head-left{{display:flex;flex-direction:column;gap:2px}}
  .hero-title{{font-family:var(--font-ui);font-size:11px;color:var(--text-dim);letter-spacing:0.18em;text-transform:uppercase;font-weight:600}}
  .hero-sub{{font-family:var(--font-mono);font-size:11px;color:var(--text-dim);margin-top:6px}}
  /* T4 hero subtitle: lead with total + annualized perf, then the methodology
     note in a slightly dimmer / smaller secondary tier so the eye lands on
     the numbers first. */
  .hero-sub-sep{{color:var(--text-dim);opacity:0.7;margin:0 2px}}
  .hero-sub-meta{{font-size:10px;opacity:0.65;margin-left:6px}}
  /* T10 unusual-volume chips: amber-bordered pills near the hero subtitle.
     The row hides entirely when no tickers qualify (Python emits "" then). */
  .unusual-vol-row{{display:flex;flex-wrap:wrap;align-items:center;gap:6px;
    margin-top:10px;font-family:var(--font-mono);font-size:10.5px}}
  .uv-label{{color:var(--text-dim);text-transform:uppercase;letter-spacing:0.06em;
    font-size:9.5px;margin-right:2px}}
  .uv-chip{{display:inline-flex;align-items:center;gap:6px;padding:3px 8px;
    background:rgba(245,158,11,0.07);border:1px solid var(--accent);
    border-radius:14px;color:var(--text);cursor:pointer;
    font-family:var(--font-mono);font-size:10.5px;transition:background 0.15s;}}
  .uv-chip:hover{{background:rgba(245,158,11,0.18)}}
  .uv-tkr{{font-weight:600;letter-spacing:0.04em}}
  .uv-move{{font-weight:500}}
  .uv-up .uv-move{{color:var(--up)}}
  .uv-down .uv-move{{color:var(--down)}}
  .uv-vol{{color:var(--text-dim);font-size:9.5px}}
  .hero-legend{{display:flex;gap:18px;font-family:var(--font-mono);font-size:11.5px;align-items:center}}
  .leg{{display:flex;align-items:center;gap:6px;color:var(--text-2)}}
  .leg-swatch{{width:14px;height:3px;border-radius:1px}}
  .leg-swatch.basket{{background:var(--accent)}}
  .leg-swatch.spy{{background:var(--text-dim);height:1px;border-top:1px dashed var(--text-dim)}}
  .leg-swatch.nasdaq{{background:#a78bfa;height:0;border-top:2px dotted #a78bfa}}
  .leg-toggle{{background:none;border:none;padding:0;margin:0;font:inherit;color:var(--text-2);cursor:pointer}}
  .leg-toggle[aria-pressed="false"]{{opacity:0.45}}
  .leg-swatch.fx{{
    background:linear-gradient(90deg,var(--up) 0%,var(--up) 30%,var(--down) 70%,var(--down) 100%);
    height:8px;border-radius:1px;
  }}
  .hero-chart-svg-wrap{{position:relative;width:100%;height:380px}}
  .hero-chart-svg{{width:100%;height:100%;display:block}}
  /* T7: 30-day rolling alpha sparkline beneath the hero chart. */
  /* v2.7 #6: inset the sparkline plot (head + svg) to match the hero chart's
     padL=48 / padR=56 px so a calendar date lands at the same x across the
     chart and both sparklines (which now map x by date over the basket span). */
  .spark-plot{{padding-left:48px;padding-right:56px;box-sizing:border-box}}
  .alpha-sparkline-wrap{{margin-top:14px;padding-top:10px;
    border-top:1px solid var(--border)}}
  .alpha-sparkline-head{{display:flex;justify-content:space-between;align-items:baseline;
    font-family:var(--font-mono);font-size:11px;color:var(--text-dim);margin-bottom:4px}}
  .alpha-sparkline-label{{letter-spacing:0.04em}}
  .alpha-sparkline-latest{{font-weight:600;font-size:11.5px}}
  .alpha-sparkline{{width:100%;height:36px;display:block;cursor:crosshair}}
  /* v1.8 T5: drawdown sparkline. Sits flush under the alpha sparkline so the
     two share a single visual register; lighter borders so it doesn't feel
     like a fully separate section. */
  .dd-sparkline-wrap{{margin-top:8px;padding-top:6px;
    border-top:1px dashed rgba(255,255,255,0.06)}}
  .dd-sparkline-head{{display:flex;justify-content:space-between;align-items:baseline;
    font-family:var(--font-mono);font-size:11px;color:var(--text-dim);margin-bottom:3px}}
  .dd-sparkline-label{{letter-spacing:0.04em}}
  .dd-sparkline-meta{{margin-left:6px;color:var(--text-dim);opacity:0.7}}
  .dd-sparkline-latest{{font-weight:600;font-size:11.5px;color:var(--down)}}
  .dd-sparkline{{width:100%;height:30px;display:block;cursor:crosshair}}
  .dd-sparkline-fill{{fill:rgba(248,113,113,0.14)}}
  .hero-tip{{
    position:absolute;background:var(--ink);border:1px solid var(--accent);border-radius:6px;
    padding:8px 12px;font-family:var(--font-mono);font-size:11px;color:var(--text);
    pointer-events:none;white-space:nowrap;transform:translate(-50%,-100%);margin-top:-10px;
    z-index:5;box-shadow:0 4px 12px rgba(0,0,0,0.5);
  }}
  .hero-tip[hidden]{{display:none}}
  .hero-tip .tip-date{{color:var(--text-dim);font-size:10px;margin-bottom:4px}}
  .hero-tip .tip-row{{display:flex;justify-content:space-between;gap:10px}}
  .hero-tip .tip-label{{color:var(--text-dim)}}

  /* Stats */
  /* T8: stats grid is auto-fit so user-customized selection (3-10 cards)
     wraps cleanly rather than leaving empty 5-col slots. */
  .stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px;margin-bottom:14px}}
  /* T8 visibility: hide non-selected stats outside edit-mode. In edit-mode
     all 10 are visible (faded if not currently selected) so the user can
     toggle them on. */
  .stat[data-stat-hidden="true"]{{display:none}}
  body.edit-mode .stat[data-stat-hidden="true"]{{display:block;opacity:0.45}}
  body.edit-mode .stat[data-stat-hidden="true"] .stat-value{{filter:grayscale(1)}}
  .stat-bar{{display:none}}
  body.edit-mode .stat-bar{{
    display:flex;align-items:center;gap:8px;margin:-8px -12px 8px -12px;padding:5px 10px;
    background:var(--surface);border-bottom:1px dashed var(--border);
    font-family:var(--font-mono);font-size:10px;letter-spacing:0.04em;
  }}
  .stat-grip{{cursor:grab;color:var(--text-dim);font-size:11px;user-select:none}}
  body.edit-mode .stat-grip:active{{cursor:grabbing}}
  .stat-bar-name{{font-weight:600;color:var(--text);text-transform:uppercase;letter-spacing:0.06em;font-size:9.5px}}
  .stat-vis{{margin-left:auto;display:flex;align-items:center;gap:5px;color:var(--text-dim);cursor:pointer;user-select:none}}
  .stat-vis input{{cursor:pointer;accent-color:var(--accent);transform:scale(0.85)}}
  body.edit-mode .stat{{outline:1px solid var(--border);outline-offset:3px;border-radius:8px}}
  .stat-ghost{{opacity:0.35}}
  .stat-chosen .stat-bar{{border-bottom-color:var(--accent);background:var(--surface-2)}}
  .stat{{
    background:linear-gradient(180deg,var(--surface) 0%,var(--ink-soft) 100%);
    border:1px solid var(--border);border-radius:12px;padding:16px 20px;position:relative;overflow:hidden;
  }}
  .stat::before{{content:"";position:absolute;top:0;left:0;right:0;height:1px;
    background:linear-gradient(90deg,transparent,rgba(255,255,255,0.06),transparent)}}
  .stat-label{{font-size:10px;color:var(--text-dim);text-transform:uppercase;letter-spacing:0.18em;font-weight:500}}
  .stat-value{{
    font-family:var(--font-display);font-size:32px;font-weight:400;margin-top:6px;line-height:1;
    letter-spacing:-0.015em;font-variant-numeric:tabular-nums;
  }}
  .stat-meta{{font-family:var(--font-mono);font-size:10.5px;color:var(--text-dim);margin-top:6px;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
  .pos{{color:var(--up)}}
  .neg{{color:var(--down)}}
  .stat-value.dim{{color:var(--text-dim);font-size:24px}}
  .build-info{{margin-top:14px;font-family:var(--font-mono);font-size:11px;color:var(--text-dim)}}
  .build-info .build-ver{{color:var(--text-2);font-weight:600}}
  .build-health{{font-family:var(--font-mono);font-size:10px;letter-spacing:0.02em;
    color:var(--text-dim);text-align:right;padding:8px 16px;border-top:1px solid var(--border);
    margin-top:24px}}
  .build-health .bh-fail{{color:var(--down)}}
  .build-health .bh-ok{{color:var(--up)}}
  .build-health .bh-breakdown{{opacity:0.6;font-size:9.5px;margin-left:2px}}
  .build-info .live{{
    display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--up);
    margin-right:8px;box-shadow:0 0 0 0 var(--up);animation:pulse 2.4s ease-out infinite;vertical-align:1px;
  }}
  @keyframes pulse{{
    0%{{box-shadow:0 0 0 0 rgba(52,211,153,0.6)}}
    70%{{box-shadow:0 0 0 8px rgba(52,211,153,0)}}
    100%{{box-shadow:0 0 0 0 rgba(52,211,153,0)}}
  }}

  /* Contributors */
  .contributors{{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin:28px 0 8px}}
  .contrib-col{{
    background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:16px 18px;
  }}
  .contrib-col h3{{
    margin:0 0 12px;font-family:var(--font-ui);font-size:11px;color:var(--text-dim);
    text-transform:uppercase;letter-spacing:0.18em;font-weight:600;
  }}
  .contrib-table{{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}}
  .contrib-table th{{
    text-align:left;padding:6px 8px;font-size:9.5px;color:var(--text-dim);
    text-transform:uppercase;letter-spacing:0.14em;font-weight:600;border-bottom:1px solid var(--border);
  }}
  .contrib-table th.num{{text-align:right}}
  .contrib-table td{{padding:9px 8px;border-bottom:1px solid var(--border);font-size:12.5px}}
  .contrib-table tbody tr:last-child td{{border-bottom:none}}
  .contrib-table .ct-tkr{{font-family:var(--font-mono);font-weight:600;color:var(--text);font-size:12.5px}}
  .contrib-table .ct-ind{{font-family:var(--font-ui);font-size:10px;color:var(--text-dim);font-weight:400;margin-top:1px}}
  .contrib-table .num{{text-align:right;font-family:var(--font-mono);font-size:12px;color:var(--text-2)}}
  .contrib-table .ct-contrib{{font-weight:500}}
  .contrib-table tbody tr{{cursor:pointer;transition:background 0.12s}}
  .contrib-table tbody tr:hover{{background:var(--surface-2)}}

  /* Detractors panel — full-width, expanded view with exit-strategy hint */
  .detractors-section{{
    background:linear-gradient(180deg,var(--surface) 0%,var(--ink-soft) 100%);
    border:1px solid var(--border);border-radius:12px;padding:18px 20px;
    margin:28px 0 8px;
  }}
  .dt-head-row h3{{margin:0 0 4px;font-family:var(--font-display);font-size:18px;
    font-weight:400;color:var(--text);letter-spacing:-0.01em}}
  .dt-head-row h3 .muted{{color:var(--text-dim);font-size:14px;margin-left:4px}}
  .dt-head-row p.muted{{margin:0 0 14px;font-family:var(--font-ui);font-size:12px;
    color:var(--text-dim);line-height:1.5}}
  .dt-head-row strong{{color:var(--text-2);font-weight:500}}
  .dt-scroll{{width:100%;overflow-x:auto}}
  /* T17: mobile card view shown below 700px. The desktop table has 7
     columns of numerics; on phones the row collapses to a small card per
     ticker with just the actionable tags (signal / analyst / suggested
     action). Tap the card to drill into the ticker modal. */
  .dt-mobile-cards{{display:none}}
  .dt-mobile-card{{
    background:var(--surface-2);border:1px solid var(--border);
    border-radius:10px;padding:12px 14px;margin-bottom:8px;cursor:pointer;
    transition:background 0.12s;
  }}
  .dt-mobile-card:last-child{{margin-bottom:0}}
  .dt-mobile-card:hover{{background:var(--surface)}}
  .dt-mobile-head{{display:flex;justify-content:space-between;align-items:baseline;
    font-family:var(--font-mono)}}
  .dt-mobile-tkr{{font-size:14px;font-weight:600;letter-spacing:0.04em;color:var(--text)}}
  .dt-mobile-ret{{font-size:13px;font-weight:600}}
  .dt-mobile-ind{{font-family:var(--font-ui);font-size:11px;color:var(--text-dim);
    margin:2px 0 8px}}
  .dt-mobile-pills{{display:flex;flex-wrap:wrap;gap:6px}}
  .dt-mobile-pill{{padding:3px 8px;border-radius:10px;font-family:var(--font-mono);
    font-size:9.5px;letter-spacing:0.04em;text-transform:uppercase;font-weight:600;
    border:1px solid var(--border);background:var(--ink-soft)}}
  @media (max-width:700px){{
    .dt-scroll{{display:none}}
    .dt-mobile-cards{{display:block}}
  }}
  .dt-table{{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}}
  .dt-table th{{text-align:left;padding:7px 10px;font-size:9.5px;color:var(--text-dim);
    text-transform:uppercase;letter-spacing:0.14em;font-weight:600;border-bottom:1px solid var(--border)}}
  .dt-table th.num{{text-align:right}}
  .dt-table td{{padding:10px 10px;border-bottom:1px solid var(--border);font-size:12.5px;
    color:var(--text-2);vertical-align:middle}}
  .dt-table tbody tr:last-child td{{border-bottom:none}}
  .dt-table tbody tr{{cursor:pointer;transition:background 0.12s}}
  .dt-table tbody tr:hover{{background:var(--surface-2)}}
  .dt-tkr{{font-family:var(--font-mono);font-weight:600;color:var(--text);font-size:12.5px;min-width:90px}}
  .dt-ind{{font-family:var(--font-ui);font-size:10px;color:var(--text-dim);font-weight:400;margin-top:2px}}
  .dt-table .num{{text-align:right;font-family:var(--font-mono);font-size:12px}}
  .dt-table .neg.dt-ret,.dt-table .neg.dt-contrib{{color:var(--down);font-weight:500}}
  .dt-sig{{min-width:130px;line-height:1.2}}
  .dt-rec{{min-width:110px;line-height:1.3}}
  .dt-upside{{font-family:var(--font-mono);font-size:10px;color:var(--text-dim);margin-top:3px}}
  .dt-action{{min-width:140px}}
  .dt-action-pill{{font-family:var(--font-mono);font-size:10px;font-weight:600;letter-spacing:0.06em;
    padding:3px 8px;border-radius:4px;border:1px solid;white-space:nowrap;display:inline-block}}
  .dt-action-pos{{color:var(--up);border-color:var(--up);background:rgba(52,211,153,0.08)}}
  .dt-action-neutral{{color:var(--accent);border-color:var(--accent);background:rgba(245,158,11,0.08)}}
  .dt-action-neg{{color:var(--down);border-color:var(--down);background:rgba(248,113,113,0.10)}}
  /* 2× ATR suggested stop, sub-line under the action pill */
  .dt-action-stop{{font-family:var(--font-mono);font-size:10.5px;color:var(--text-2);
    margin-top:5px;line-height:1.3;letter-spacing:0.02em}}
  .dt-action-stop-label{{color:var(--text-dim);text-transform:uppercase;font-size:9px;
    letter-spacing:0.12em;font-weight:600;margin-right:2px}}
  .dt-action-stop-native{{color:var(--text-dim)}}
  .dt-action-stop-meta{{color:var(--text-dim);font-size:9.5px;margin-left:4px}}
  @media (max-width:900px){{
    .dt-table th,.dt-table td{{padding:8px 6px;font-size:11.5px}}
  }}

  /* Regret tracker (closed-position post-exit moves) */
  .regret{{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin:22px 0 8px}}
  .regret-col{{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:16px 18px 14px}}
  .regret-col h3{{margin:0 0 12px;font-family:var(--font-display);font-size:16px;font-weight:400;
    color:var(--text);letter-spacing:-0.01em}}
  .regret-col h3 .muted{{color:var(--text-dim);font-size:12px;font-family:var(--font-ui);margin-left:2px}}
  .regret-table{{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}}
  .regret-table th{{text-align:left;padding:6px 8px;border-bottom:1px solid var(--border);
    font-family:var(--font-ui);font-size:10px;text-transform:uppercase;letter-spacing:0.06em;
    color:var(--text-dim);font-weight:500}}
  .regret-table th.num{{text-align:right}}
  .regret-table td{{padding:8px 8px;border-bottom:1px solid var(--border);font-size:12.5px}}
  .regret-table tbody tr:last-child td{{border-bottom:none}}
  .regret-table .rg-tkr{{font-family:var(--font-mono);font-weight:600;color:var(--text);font-size:12.5px}}
  .regret-table .rg-ind{{font-family:var(--font-ui);font-size:10px;color:var(--text-dim);font-weight:400;margin-top:1px}}
  .regret-table .num{{text-align:right;font-family:var(--font-mono);font-size:12px;color:var(--text-2)}}
  .regret-table .rg-delta{{font-weight:500}}
  .regret-table .rg-delta.pos{{color:var(--down)}}    /* "regret" = bad-for-you = red */
  .regret-table .rg-delta.neg{{color:var(--up)}}      /* "escape" = good-for-you = green */
  .regret-table tbody tr{{cursor:pointer;transition:background 0.12s}}
  .regret-table tbody tr:hover{{background:var(--surface-2)}}

  /* Post-exit column in the main table */
  #ret-table td.post-exit.pos{{color:var(--down)}}    /* regret = red */
  #ret-table td.post-exit.neg{{color:var(--up)}}      /* escape = green */

  /* Untracked entries panel */
  /* Analyst signals + news side-by-side (watchlist shares the .wl-* classes,
     reactivated later by swapping render_watchlist back into the layout) */
  .wl-news-row{{display:grid;grid-template-columns:1.55fr 1fr;gap:14px;margin:28px 0 8px;align-items:start}}
  /* Outlook + News at top — 50/50 split, both panels stretch to match heights */
  .outlook-news-row{{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin:28px 0 8px;align-items:stretch}}
  /* Both panels cap at the same height; content overflows into internal scroll */
  .outlook-news-row > section{{display:flex;flex-direction:column;min-height:520px;max-height:620px}}
  .outlook-news-row .io-grid{{flex:1 1 auto;overflow-y:auto;min-height:0;max-height:none;
    grid-template-columns:1fr;gap:10px;padding-right:4px}}
  .outlook-news-row .news-list{{flex:1 1 auto;min-height:0;max-height:none;overflow-y:auto}}
  .watchlist-section,.news-section,.analyst-section,.rating-moves-section{{
    background:linear-gradient(180deg,var(--surface) 0%,var(--ink-soft) 100%);
    border:1px solid var(--border);border-radius:12px;padding:18px 20px;
  }}
  /* T9 rating-moves panel: compact table of target / rec changes since
     last build. Hover highlights a row; tickers in the first col are
     clickable to open the modal (handled via .ticker-clickable). */
  .rm-head{{display:flex;align-items:baseline;justify-content:space-between;
    gap:10px;margin-bottom:10px}}
  .rm-head h3{{font-family:var(--font-ui);font-size:13px;color:var(--text);
    margin:0;text-transform:uppercase;letter-spacing:0.10em;font-weight:600}}
  .rm-sub{{font-family:var(--font-mono);font-size:10px;color:var(--text-dim);
    letter-spacing:0.04em}}
  .rm-empty{{font-family:var(--font-mono);font-size:11px;color:var(--text-dim);
    font-style:italic;margin:8px 0 0}}
  /* v2.7 #4: two side-by-side columns -> Price targets | Recommendations.
     Each list is its own grid with fixed numeric columns so the $ values line
     up across rows. Columns stack on narrow screens. */
  .rm-cols{{display:grid;grid-template-columns:1fr 1fr;gap:18px 30px}}
  .rm-group-label{{font-family:var(--font-mono);font-size:10px;font-weight:700;
    text-transform:uppercase;letter-spacing:0.08em;color:var(--accent);
    padding-bottom:5px;border-bottom:1px solid var(--border);margin-bottom:2px}}
  .rm-group-label .muted{{font-weight:400;text-transform:none;letter-spacing:0}}
  .rm-list{{display:flex;flex-direction:column;gap:0}}
  .rm-row{{align-items:center;padding:7px 0;border-bottom:1px solid var(--border);
    font-family:var(--font-mono);font-size:11.5px;color:var(--text);
    transition:background 0.1s ease}}
  .rm-row:last-child{{border-bottom:none}}
  .rm-row:hover{{background:rgba(245,158,11,0.04)}}
  .rm-tkr{{font-weight:600;letter-spacing:0.03em;cursor:pointer}}
  .rm-tkr:hover{{color:var(--accent)}}
  /* Price-target rows: ticker | before | -> | after | pct | rec(context). */
  .rm-row--target{{display:grid;
    grid-template-columns:52px 74px 12px 74px 56px minmax(40px,1fr);column-gap:6px}}
  .rm-from{{color:var(--text-dim);text-align:right}}
  .rm-to{{color:var(--text);font-weight:500;text-align:right}}
  .rm-arrow{{color:var(--text-dim);font-size:11px;text-align:center}}
  .rm-pct{{font-weight:600;text-align:right}}
  .rm-pct.pos{{color:var(--up)}}
  .rm-pct.neg{{color:var(--down)}}
  .rm-row--target .rm-rec{{justify-self:end}}
  /* Recommendation rows: ticker | target(context) | rec move. */
  .rm-row--rec{{display:grid;grid-template-columns:52px 78px minmax(80px,1fr);column-gap:8px}}
  .rm-target-static{{color:var(--text-dim);text-align:right}}
  .rm-rec{{display:inline-flex;align-items:center;gap:5px;justify-content:flex-end}}
  .rm-row--rec .rm-rec{{justify-self:end}}
  .rm-rec-arrow{{font-weight:700;font-size:12px}}
  .rm-rec-from{{color:var(--text-dim)}}
  .rm-rec.rm-up{{color:var(--up)}}
  .rm-rec.rm-up .rm-rec-to{{color:var(--up);font-weight:600}}
  .rm-rec.rm-up .rm-arrow{{color:var(--up)}}
  .rm-rec.rm-down{{color:var(--down)}}
  .rm-rec.rm-down .rm-rec-to{{color:var(--down);font-weight:600}}
  .rm-rec.rm-down .rm-arrow{{color:var(--down)}}
  .rm-rec.rm-lat .rm-rec-to{{color:var(--text);font-weight:600}}
  .rm-rec-static{{opacity:0.85;font-weight:500}}
  @media (max-width:760px){{
    .rm-cols{{grid-template-columns:1fr;gap:16px}}
  }}
  /* v2.2 Big Brain discovery board -- full-width 2x2, colour-coded by card
     type. Tiers: warn=red, watch=amber, good=green, idea=blue. All theme-var
     driven. */
  .bigbrain-section{{
    background:linear-gradient(180deg,var(--surface),var(--ink-soft));
    border:1px solid var(--border);border-radius:13px;padding:18px 20px;
  }}
  .bb-head{{display:flex;align-items:baseline;gap:10px;margin-bottom:14px;flex-wrap:wrap}}
  .bb-head h3{{margin:0;font-family:var(--font-display);font-size:20px;
    font-weight:400;color:var(--text);letter-spacing:-0.01em}}
  .bb-head h3::before{{content:"\\1F9E0";margin-right:8px;font-size:17px}}
  .bb-sub{{font-family:var(--font-ui);font-size:14px;color:var(--text-2)}}
  .bb-asof{{font-family:var(--font-mono);font-size:10px;color:var(--text-dim);
    letter-spacing:.06em;text-transform:uppercase;margin-left:auto;align-self:center}}
  .bb-empty{{font-family:var(--font-ui);font-size:13px;color:var(--text-dim);
    font-style:italic;margin:4px 0 0;line-height:1.5}}
  .bb-grid{{display:grid;grid-template-columns:repeat(2,1fr);gap:14px}}
  /* v2.5 #9: couple pages + flip arrows */
  .bb-page{{display:none}}
  .bb-page.active{{display:block}}
  .bb-nav{{display:flex;align-items:center;justify-content:flex-end;gap:8px;margin-bottom:10px}}
  .bb-arrow{{cursor:pointer;background:var(--surface-2);border:1px solid var(--border);
    color:var(--text);border-radius:7px;width:28px;height:26px;font-size:16px;line-height:1;
    display:flex;align-items:center;justify-content:center;font-family:var(--font-mono);
    transition:border-color .12s,color .12s}}
  .bb-arrow:hover{{border-color:var(--accent);color:var(--accent)}}
  .bb-page-ind{{font-family:var(--font-mono);font-size:11px;color:var(--text-dim);
    min-width:34px;text-align:center}}
  .bb-card{{border:1px solid var(--border);border-radius:11px;overflow:hidden;
    background:var(--surface)}}
  .bb-card-hd{{padding:9px 14px;display:flex;align-items:center;gap:9px;
    border-bottom:1px solid var(--border)}}
  .bb-tier-warn .bb-card-hd{{background:linear-gradient(180deg,rgba(248,113,113,.20),transparent)}}
  .bb-tier-watch .bb-card-hd{{background:linear-gradient(180deg,rgba(245,158,11,.20),transparent)}}
  .bb-tier-good .bb-card-hd{{background:linear-gradient(180deg,rgba(52,211,153,.18),transparent)}}
  .bb-tier-idea .bb-card-hd{{background:linear-gradient(180deg,rgba(139,155,255,.22),transparent)}}
  .bb-ticker{{font-family:var(--font-mono);font-weight:700;font-size:15px;
    letter-spacing:.04em;color:var(--text)}}
  .bb-ticker.ticker-clickable{{cursor:pointer}}
  .bb-ticker.ticker-clickable:hover{{color:var(--accent)}}
  .bb-price{{font-family:var(--font-mono);font-size:11px;font-weight:600;
    color:var(--text-2);letter-spacing:.01em}}
  .bb-badge{{font-family:var(--font-mono);font-size:9px;letter-spacing:.08em;
    text-transform:uppercase;color:var(--text-dim);border:1px solid var(--border);
    border-radius:4px;padding:1px 5px}}
  .bb-tag{{margin-left:auto;font-family:var(--font-mono);font-size:9.5px;
    letter-spacing:.06em;text-transform:uppercase;font-weight:600;padding:3px 7px;
    border-radius:5px;color:var(--text-dim)}}
  .bb-tier-warn .bb-tag{{color:var(--down);background:rgba(248,113,113,.16)}}
  .bb-tier-watch .bb-tag{{color:var(--accent);background:rgba(245,158,11,.16)}}
  .bb-tier-good .bb-tag{{color:var(--up);background:rgba(52,211,153,.16)}}
  .bb-tier-idea .bb-tag{{color:#8b9bff;background:rgba(139,155,255,.18)}}
  .bb-card-bd{{padding:12px 15px 14px}}
  .bb-body{{font-family:var(--font-ui);font-size:13.5px;line-height:1.55;
    color:var(--text-2);margin:0 0 10px}}
  .bb-body strong{{color:var(--text)}}
  .bb-pills{{margin:0 0 2px}}
  .bb-pill{{display:inline-block;font-family:var(--font-mono);font-size:10px;
    font-weight:600;color:var(--text-dim);background:var(--surface-2);
    border:1px solid var(--border);border-radius:4px;padding:2px 7px;margin:3px 4px 0 0}}
  .bb-cite{{display:block;margin-top:10px;padding-top:10px;
    border-top:1px dashed var(--border);font-family:var(--font-ui);font-size:12px;
    color:var(--accent);text-decoration:none;line-height:1.4;
    overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
  .bb-cite:hover{{text-decoration:underline}}
  .bb-macro{{display:flex;gap:10px;align-items:baseline;margin-bottom:12px;
    padding:10px 13px;border:1px solid var(--border);border-left:3px solid #8b9bff;
    border-radius:9px;background:rgba(139,155,255,.08)}}
  .bb-macro-tag{{font-family:var(--font-mono);font-size:10px;font-weight:700;
    letter-spacing:.06em;text-transform:uppercase;color:#8b9bff;white-space:nowrap}}
  .bb-macro-body{{font-family:var(--font-ui);font-size:12.5px;color:var(--text-2);line-height:1.5}}
  .bb-memory{{margin-top:7px;font-family:var(--font-mono);font-size:10.5px;
    color:var(--text-dim);font-style:italic}}
  @media (max-width:760px){{ .bb-grid{{grid-template-columns:1fr}} }}
  /* v2.3 Market expectations -- prediction-market sentiment (Kalshi/Polymarket).
     Probability bar + since-last-build delta per tracked theme. */
  .market-expectations-section{{
    background:linear-gradient(180deg,var(--surface),var(--ink-soft));
    border:1px solid var(--border);border-radius:12px;padding:18px 20px;
  }}
  .me-head{{display:flex;align-items:baseline;gap:10px;margin-bottom:12px;flex-wrap:wrap}}
  .me-head h3{{margin:0;font-family:var(--font-display);font-size:18px;
    font-weight:400;color:var(--text);letter-spacing:-0.01em}}
  .me-head h3::before{{content:"\\1F4C8";margin-right:8px;font-size:15px}}
  .me-sub{{font-family:var(--font-mono);font-size:10px;color:var(--text-dim);
    letter-spacing:.04em;text-transform:lowercase;margin-left:auto}}
  /* v2.5 #3: cycle through the deeper theme pool */
  .me-reshuffle{{cursor:pointer;background:var(--surface-2);border:1px solid var(--border);
    color:var(--text-2);border-radius:6px;font-size:11px;padding:3px 10px;
    font-family:var(--font-mono);transition:border-color .12s,color .12s}}
  .me-reshuffle:hover{{border-color:var(--accent);color:var(--accent)}}
  .me-legend{{font-family:var(--font-ui);font-size:11px;color:var(--text-dim);
    margin:0 0 10px;line-height:1.4}}
  .me-legend b{{color:var(--text-2);font-family:var(--font-mono);font-weight:600}}
  .me-empty{{font-family:var(--font-ui);font-size:12px;color:var(--text-dim);
    font-style:italic;margin:4px 0 0}}
  .me-list{{display:flex;flex-direction:column;gap:0}}
  .me-row{{display:grid;grid-template-columns:1fr 48px 64px 120px 76px;
    gap:12px;align-items:center;padding:9px 0;border-bottom:1px solid var(--border);
    font-family:var(--font-ui);font-size:12.5px}}
  .me-row:last-child{{border-bottom:none}}
  .me-q-wrap{{display:flex;flex-direction:column;gap:1px;min-width:0}}
  .me-q{{color:var(--text);text-decoration:none;font-weight:600}}
  a.me-q:hover{{color:var(--accent);text-decoration:underline}}
  .me-qsub{{font-family:var(--font-ui);font-size:10.5px;color:var(--text-dim);
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
  .me-prob{{font-family:var(--font-mono);font-weight:700;color:var(--text);text-align:right}}
  .me-delta{{font-family:var(--font-mono);font-size:11px;font-weight:600;text-align:right}}
  .me-up{{color:var(--up)}} .me-down{{color:var(--down)}} .me-flat{{color:var(--text-dim)}}
  .me-bar{{height:8px;border-radius:5px;background:var(--surface-2);overflow:hidden}}
  .me-bar > i{{display:block;height:100%;background:var(--accent);border-radius:5px}}
  .me-src{{font-family:var(--font-mono);font-size:9px;letter-spacing:.06em;
    text-transform:uppercase;color:var(--text-dim);text-align:right}}
  @media (max-width:700px){{
    .me-row{{grid-template-columns:1fr 44px 56px;gap:8px}}
    .me-bar,.me-src{{display:none}}
  }}
  /* v2.3 Conviction-vs-signal quadrant */
  .quadrant-section{{
    background:linear-gradient(180deg,var(--surface),var(--ink-soft));
    border:1px solid var(--border);border-radius:12px;padding:18px 20px;
  }}
  .q-head{{display:flex;align-items:baseline;gap:10px;margin-bottom:6px;flex-wrap:wrap}}
  .q-head h3{{margin:0;font-family:var(--font-display);font-size:18px;font-weight:400;
    color:var(--text);letter-spacing:-0.01em}}
  .q-sub{{font-family:var(--font-mono);font-size:10px;color:var(--text-dim);
    letter-spacing:.04em;text-transform:lowercase}}
  .q-q{{font-family:var(--font-mono);font-size:9px;fill:var(--text-dim);
    text-transform:uppercase;letter-spacing:.06em}}
  .q-ax{{font-family:var(--font-mono);font-size:10px;fill:var(--text-2)}}
  .q-tkr{{font-family:var(--font-mono);font-size:9.5px;fill:var(--text);font-weight:600}}
  .q-dot{{cursor:pointer;transition:opacity .12s}}
  .q-dot:hover{{opacity:.75}}
  .q-dot.dot-up{{fill:var(--up)}} .q-dot.dot-down{{fill:var(--down)}}
  /* v2.3 "since your last visit" banner (client-side localStorage diff) */
  /* v2.4: compact inline pill (was a full-width band that read as dead space).
     The explicit display below would override the UA [hidden] rule, so restore
     it — that latent bug showed an empty orange bar on first visits. */
  #last-look{{display:inline-flex;align-items:center;gap:8px;margin:0 0 14px;max-width:100%;
    padding:5px 9px 5px 13px;border:1px solid rgba(245,158,11,.4);
    border-radius:999px;background:rgba(245,158,11,.07)}}
  #last-look[hidden]{{display:none}}
  #last-look .ll-tag{{font-family:var(--font-mono);font-size:9.5px;font-weight:700;
    letter-spacing:.06em;text-transform:uppercase;color:var(--accent);white-space:nowrap}}
  #last-look .ll-body{{font-family:var(--font-ui);font-size:12px;color:var(--text-2);
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}}
  #last-look .ll-body b{{color:var(--text)}}
  #last-look .ll-x{{background:none;border:none;color:var(--text-dim);
    font-size:15px;cursor:pointer;line-height:1;padding:0 2px}}
  #last-look .ll-x:hover{{color:var(--text)}}
  /* Full-width analyst section (re-entry ideas) below the main table */
  section.analyst-section{{margin:22px 0 8px}}

  /* Analyst panel */
  .an-head-row h3{{margin:0 0 4px;font-family:var(--font-display);font-size:18px;
    font-weight:400;color:var(--text);letter-spacing:-0.01em}}
  .an-head-row h3 .muted{{color:var(--text-dim);font-size:14px;margin-left:4px}}
  .an-head-row p.muted{{margin:0 0 14px;font-family:var(--font-ui);font-size:12px;
    color:var(--text-dim);line-height:1.5}}
  .an-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px;
    max-height:308px;overflow-y:auto;padding-right:4px}}
  .an-empty{{font-family:var(--font-ui);font-size:12px;color:var(--text-dim);
    padding:18px 4px;line-height:1.5}}
  .an-empty code{{font-family:var(--font-mono);font-size:11px;color:var(--text-2);
    background:var(--surface-2);padding:1px 5px;border-radius:3px}}
  .an-card{{
    background:var(--ink-soft);border:1px solid var(--border);border-radius:10px;
    padding:12px 14px;cursor:pointer;transition:border-color 0.15s,transform 0.15s;
    display:flex;flex-direction:column;gap:6px;
  }}
  .an-card:hover{{border-color:var(--accent);transform:translateY(-1px)}}
  .an-head{{display:flex;justify-content:space-between;align-items:flex-start;gap:6px}}
  .an-tkr{{font-family:var(--font-mono);font-size:13px;font-weight:600;color:var(--text);line-height:1.1}}
  .an-ind{{font-family:var(--font-ui);font-size:10px;color:var(--text-dim);font-weight:400;
    margin-top:3px;letter-spacing:0.02em;line-height:1.2}}
  .an-rec{{font-family:var(--font-mono);font-size:9.5px;font-weight:600;letter-spacing:0.06em;
    padding:2px 6px;border-radius:4px;border:1px solid;white-space:nowrap;line-height:1.2}}
  .an-rec-strong-buy{{color:var(--up);border-color:var(--up);background:rgba(52,211,153,0.12)}}
  .an-rec-buy{{color:var(--up);border-color:var(--up)}}
  .an-rec-hold{{color:var(--accent);border-color:var(--accent)}}
  .an-rec-sell{{color:var(--down);border-color:var(--down)}}
  .an-rec-strong-sell{{color:var(--down);border-color:var(--down);background:rgba(248,113,113,0.12)}}
  .an-rec-none{{color:var(--text-dim);border-color:var(--text-dim)}}
  .an-name{{font-family:var(--font-ui);font-size:11px;color:var(--text-2);line-height:1.3;
    overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
  .an-line{{font-family:var(--font-mono);font-size:12px;color:var(--text-2);display:flex;
    align-items:baseline;gap:6px;flex-wrap:wrap}}
  .an-cur{{color:var(--text);font-weight:500}}
  .an-dot{{color:var(--text-dim)}}
  .an-upside{{font-family:var(--font-mono);font-size:13px;font-weight:600;letter-spacing:-0.01em}}
  .an-upside.pos{{color:var(--up)}}
  .an-upside.neg{{color:var(--down)}}
  /* Technical signal pill — uses sig-pos / sig-neg / sig-neutral classes shared with the table */
  .an-signal{{font-family:var(--font-ui);font-size:10.5px;font-weight:500;
    display:flex;align-items:center;gap:6px;letter-spacing:0.01em;line-height:1.2;
    padding:4px 8px;border-radius:6px;background:var(--surface-2);
  }}
  .an-signal-dot{{width:6px;height:6px;border-radius:50%;background:var(--text-dim);flex-shrink:0}}
  .an-signal.sig-pos{{color:var(--up)}} .an-signal.sig-pos .an-signal-dot{{background:var(--up)}}
  .an-signal.sig-neg{{color:var(--down)}} .an-signal.sig-neg .an-signal-dot{{background:var(--down)}}
  .an-signal.sig-neutral{{color:var(--accent)}} .an-signal.sig-neutral .an-signal-dot{{background:var(--accent)}}
  /* Signal-row label is the flex middle child; let it ellipsis if a long signal
     name would otherwise push the RSI pill off the card. */
  .an-signal-label{{flex:0 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
  /* RSI chip, right-aligned within the signal row */
  .an-rsi{{margin-left:auto;font-family:var(--font-mono);font-size:9.5px;font-weight:600;
    letter-spacing:0.04em;padding:2px 6px;border-radius:4px;
    border:1px solid var(--border);background:var(--ink-soft);flex-shrink:0}}
  .an-rsi-hot{{color:var(--down);border-color:var(--down);background:rgba(248,113,113,0.10)}}
  .an-rsi-cold{{color:var(--up);border-color:var(--up);background:rgba(52,211,153,0.10)}}
  .an-rsi-neutral{{color:var(--text-2);border-color:var(--border)}}
  .an-rsi-dim{{color:var(--text-dim);border-color:var(--border);background:transparent}}
  .an-foot{{font-family:var(--font-mono);font-size:9.5px;color:var(--text-dim);
    text-transform:uppercase;letter-spacing:0.08em}}
  .an-range{{cursor:help;border-bottom:1px dotted var(--border)}}
  .an-range-wide{{color:var(--down)}}
  @media (max-width:900px){{
    .an-grid{{grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:8px}}
  }}
  .wl-head-row h3,.news-head h3{{
    margin:0 0 4px;font-family:var(--font-display);font-size:18px;
    font-weight:400;color:var(--text);letter-spacing:-0.01em;
  }}
  .wl-head-row h3 .muted,.news-head .muted{{color:var(--text-dim);font-size:14px;margin-left:4px}}
  .wl-head-row p.muted{{margin:0 0 14px;font-family:var(--font-ui);font-size:12px;
    color:var(--text-dim);line-height:1.5}}
  .wl-head-row code{{font-family:var(--font-mono);font-size:11px;color:var(--text-2);
    background:var(--surface-2);padding:1px 5px;border-radius:3px}}
  /* v3.0 #4: head row lays out title + pager arrows on one line */
  .wl-head-top{{display:flex;align-items:center;justify-content:space-between;gap:10px}}
  .wl-nav{{display:flex;align-items:center;gap:8px;flex:0 0 auto}}
  .wl-arrow{{cursor:pointer;background:var(--surface-2);border:1px solid var(--border);
    color:var(--text);border-radius:7px;width:28px;height:26px;font-size:16px;line-height:1;
    display:flex;align-items:center;justify-content:center;font-family:var(--font-mono);
    transition:border-color .12s,color .12s}}
  .wl-arrow:hover{{border-color:var(--accent);color:var(--accent)}}
  .wl-page-ind{{font-family:var(--font-mono);font-size:11px;color:var(--text-dim);min-width:34px;text-align:center}}
  .wl-grid{{display:grid;grid-template-columns:repeat({WATCH_COLS_DESKTOP},minmax(0,1fr));gap:10px}}
  .wl-card.wl-auto{{background:color-mix(in srgb,var(--accent) 9%,transparent);border-left:2px solid var(--accent)}}
  .wl-auto-tag{{display:inline-block;margin-left:6px;padding:1px 5px;font-size:9px;font-weight:600;border-radius:4px;background:color-mix(in srgb,var(--accent) 22%,transparent);color:var(--accent);vertical-align:middle;letter-spacing:0.02em}}
  .wl-card{{
    background:var(--ink-soft);border:1px solid var(--border);border-radius:10px;
    padding:12px 14px;cursor:pointer;transition:border-color 0.15s,transform 0.15s;
    display:flex;flex-direction:column;gap:6px;
  }}
  .wl-card:hover{{border-color:var(--accent);transform:translateY(-1px)}}
  .wl-head{{display:flex;justify-content:space-between;align-items:flex-start;gap:6px}}
  .wl-tkr{{font-family:var(--font-mono);font-size:13px;font-weight:600;color:var(--text);line-height:1.1}}
  .wl-ind{{font-family:var(--font-ui);font-size:10px;color:var(--text-dim);font-weight:400;
    margin-top:3px;letter-spacing:0.02em;line-height:1.2}}
  .wl-pct{{font-family:var(--font-mono);font-size:14px;font-weight:600;letter-spacing:-0.01em;line-height:1.1}}
  .wl-pct.pos{{color:var(--up)}}
  .wl-pct.neg{{color:var(--down)}}
  .wl-name{{font-family:var(--font-ui);font-size:11px;color:var(--text-2);line-height:1.3;
    overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
  .wl-price{{font-family:var(--font-mono);font-size:11.5px;color:var(--text-2);display:flex;
    align-items:baseline;justify-content:space-between;gap:6px}}
  .wl-latest{{color:var(--text);font-weight:500}}
  .wl-gbp{{color:var(--text-dim);font-size:10.5px}}
  .wl-spark{{height:48px;width:100%}}
  .wl-spark.pos{{color:var(--up)}}
  .wl-spark.neg{{color:var(--down)}}
  .wl-spark .sparkline{{width:100%;height:100%;display:block}}
  .wl-foot{{display:flex;justify-content:space-between;align-items:center;font-family:var(--font-mono);
    font-size:9.5px;color:var(--text-dim);text-transform:uppercase;letter-spacing:0.08em}}
  .wl-note{{color:var(--text-2);font-family:var(--font-ui);text-transform:none;letter-spacing:0;
    font-size:10.5px;text-align:right;max-width:60%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
  /* v2.7 watchlist entry-signal layer — tone classes scoped under the section to
     avoid the specificity collision seen with the value-screen .vs-pass bug. */
  .watchlist-section .wl-verdict{{display:inline-block;font-size:10.5px;font-weight:700;
    letter-spacing:0.02em;padding:2px 8px;border-radius:999px;margin:8px 0 6px;
    border:1px solid var(--text-dim);color:var(--text-dim);background:var(--surface-2)}}
  .watchlist-section .wl-v-buy{{border-color:var(--up);color:var(--up)}}
  .watchlist-section .wl-v-caution{{border-color:var(--accent);color:var(--accent)}}
  .watchlist-section .wl-v-neutral{{border-color:var(--text-dim);color:var(--text-dim)}}
  .watchlist-section .wl-chips{{display:flex;flex-wrap:wrap;gap:4px;margin:2px 0 6px}}
  .watchlist-section .wl-chip{{font-size:9.5px;font-weight:600;padding:2px 6px;border-radius:6px;
    white-space:nowrap;border:1px solid var(--border);color:var(--text-2);background:var(--ink-soft)}}
  .watchlist-section .wl-c-buy{{border-color:var(--up);color:var(--up)}}
  .watchlist-section .wl-c-caution{{border-color:var(--accent);color:var(--accent)}}
  .watchlist-section .wl-c-neutral{{color:var(--text-2)}}
  .watchlist-section .wl-cite{{font-size:10.5px;color:var(--text-dim);margin:2px 0 0;
    overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
  .watchlist-section .wl-cite a{{color:var(--text-dim);text-decoration:none}}
  .watchlist-section .wl-cite a:hover{{text-decoration:underline;color:var(--text-2)}}
  .watchlist-section .wl-cite-src{{opacity:0.8}}

  /* Industry attribution (performance attribution of open positions) */
  .attribution-section{{
    background:linear-gradient(180deg,var(--surface) 0%,var(--ink-soft) 100%);
    border:1px solid var(--border);border-radius:12px;padding:18px 20px;
    margin:14px 0 8px;
  }}
  .ia-head-row h3{{margin:0 0 4px;font-family:var(--font-display);font-size:18px;
    font-weight:400;color:var(--text);letter-spacing:-0.01em}}
  .ia-head-row h3 .muted{{color:var(--text-dim);font-size:14px;margin-left:4px}}
  .ia-head-row p.muted{{margin:0 0 14px;font-family:var(--font-ui);font-size:12px;
    color:var(--text-dim);line-height:1.5}}
  .ia-head-row strong{{color:var(--text);font-weight:500}}
  .ia-scroll{{width:100%;overflow-x:auto}}
  .ia-table{{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}}
  .ia-table th{{text-align:left;padding:7px 10px;font-size:9.5px;color:var(--text-dim);
    text-transform:uppercase;letter-spacing:0.14em;font-weight:600;border-bottom:1px solid var(--border)}}
  .ia-table th.num{{text-align:right}}
  .ia-table td{{padding:9px 10px;border-bottom:1px solid var(--border);font-size:12.5px;
    color:var(--text-2)}}
  .ia-table tbody tr:last-child td{{border-bottom:none}}
  .ia-table tbody tr{{cursor:default;transition:background 0.12s}}
  .ia-table tbody tr:hover{{background:var(--surface-2)}}
  .ia-industry{{font-family:var(--font-ui);font-weight:500;color:var(--text);min-width:160px}}
  .ia-table .num{{text-align:right;font-family:var(--font-mono);font-size:12px}}
  .ia-table .num.pos{{color:var(--up);font-weight:500}}
  .ia-table .num.neg{{color:var(--down);font-weight:500}}
  /* Bar cell — relative wrapper; axis line at 50%; fill positioned by inline style */
  .ia-bar{{position:relative;width:200px;min-width:200px;padding:9px 10px;height:30px;
    background:linear-gradient(90deg,rgba(255,255,255,0.02) 0%,transparent 50%,rgba(255,255,255,0.02) 100%)}}
  .ia-bar-axis{{position:absolute;top:4px;bottom:4px;left:50%;width:1px;background:var(--text-dim);opacity:0.4}}
  .ia-bar-fill{{position:absolute;top:8px;bottom:8px;border-radius:2px;opacity:0.7}}
  /* v2.1 #5: numeric label anchored at the axis line, opposite side from the
     bar tip. Same-sign labels form a vertical column; mono font + right- or
     left-align lets the eye scan digit count by horizontal extent. */
  .ia-bar-label{{position:absolute;top:50%;transform:translateY(-50%);
    font-family:var(--font-mono);font-size:10.5px;line-height:1;
    white-space:nowrap;font-weight:500;letter-spacing:0.02em;opacity:0.9}}
  .ia-bar-label.pos{{color:var(--up)}}
  .ia-bar-label.neg{{color:var(--down)}}
  @media (max-width:900px){{
    /* v2.5 #5: render the bar on mobile (compact) instead of display:none,
       which left an orphan "Contribution bar" header over a blank column.
       The table sits in .ia-scroll (overflow-x:auto), so the narrower bar
       just rides the horizontal scroll rather than breaking page width. */
    .ia-bar{{width:120px;min-width:120px;padding:8px 6px}}
    .ia-bar-label{{font-size:9.5px}}
    .ia-table th,.ia-table td{{padding:8px 6px;font-size:11.5px}}
  }}

  /* v2.6 Value screen — scorecard table of quality+value names near 52w low */
  .vs-head{{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin-bottom:6px}}
  .vs-head h3{{margin:0;font-family:var(--font-display);font-size:18px;font-weight:400;
    color:var(--text);letter-spacing:-0.01em}}
  .vs-head h3::before{{content:"\\1F50E";margin-right:8px;font-size:15px}}
  .vs-sub{{font-family:var(--font-mono);font-size:10px;color:var(--text-dim);
    letter-spacing:.04em;text-transform:lowercase;margin-left:auto}}
  .vs-legend{{font-family:var(--font-ui);font-size:11px;color:var(--text-dim);
    margin:0 0 10px;line-height:1.45}}
  .vs-legend b{{color:var(--text-2);font-weight:600}}
  .vs-empty{{font-family:var(--font-ui);font-size:12px;color:var(--text-dim);font-style:italic;margin:4px 0 0}}
  .vs-nav{{display:flex;align-items:center;justify-content:flex-end;gap:8px;margin-bottom:8px}}
  .vs-arrow{{cursor:pointer;background:var(--surface-2);border:1px solid var(--border);
    color:var(--text);border-radius:7px;width:28px;height:26px;font-size:16px;line-height:1;
    display:flex;align-items:center;justify-content:center;font-family:var(--font-mono);
    transition:border-color .12s,color .12s}}
  .vs-arrow:hover{{border-color:var(--accent);color:var(--accent)}}
  .vs-page-ind{{font-family:var(--font-mono);font-size:11px;color:var(--text-dim);min-width:34px;text-align:center}}
  .vs-scroll{{width:100%;overflow-x:auto}}
  .vs-table{{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums;min-width:560px}}
  .vs-table th{{text-align:left;padding:7px 9px;font-size:9.5px;color:var(--text-dim);
    letter-spacing:.06em;text-transform:uppercase;border-bottom:1px solid var(--border);font-weight:400}}
  .vs-table th.num{{text-align:right}}
  .vs-table td{{padding:8px 9px;border-bottom:1px solid var(--border);font-size:12px;color:var(--text-2)}}
  .vs-table tbody tr:last-child td{{border-bottom:none}}
  .vs-table .num{{text-align:right;font-family:var(--font-mono);font-size:11.5px}}
  /* Scoped under .vs-table so these beat the `.vs-table td` colour (0,1,1) */
  /* Metric cells: bright readable text; magnitude is encoded by the inline
     background tint (deeper = stronger vs the others shown). */
  .vs-table td.vs-cell{{color:var(--text)}}
  .vs-table .vs-pass{{color:var(--up);font-weight:500}}
  .vs-table .vs-fail{{color:var(--text-dim)}}
  .vs-table .vs-mid{{color:var(--accent);font-weight:500}}
  .vs-table .vs-near{{color:var(--accent)}}
  .vs-passcount{{font-weight:700}}
  .vs-tkr{{white-space:nowrap}}
  .vs-sym{{font-family:var(--font-mono);font-weight:700;font-size:13px;color:var(--text);letter-spacing:.02em}}
  .vs-price{{font-family:var(--font-mono);font-size:10.5px;color:var(--text-dim);margin-left:7px}}
  .vs-bb-tag{{font-family:var(--font-mono);font-size:8.5px;font-weight:700;color:#8b9bff;
    background:rgba(139,155,255,.16);border-radius:4px;padding:1px 4px;margin-left:6px;letter-spacing:.05em}}
  .vs-sector{{color:var(--text-dim);font-size:11.5px}}
  .vs-row.vs-bb{{background:rgba(139,155,255,.06)}}

  /* Basket diversification — pairwise-correlation portfolio lens */
  .div-section{{
    background:linear-gradient(180deg,var(--surface) 0%,var(--ink-soft) 100%);
    border:1px solid var(--border);border-radius:12px;padding:18px 20px;
  }}

  /* v1.9 #3: currency exposure section */
  .ccy-exposure-section{{
    background:linear-gradient(180deg,var(--surface) 0%,var(--ink-soft) 100%);
    border:1px solid var(--border);border-radius:12px;padding:18px 20px;
  }}
  .ccy-head-row{{display:flex;justify-content:space-between;align-items:baseline;gap:16px;margin-bottom:12px}}
  .ccy-head-row h3{{margin:0;font-family:var(--font-display);font-size:18px;letter-spacing:-0.01em;font-weight:600}}
  .ccy-head-row p{{margin:0;font-size:11.5px;text-align:right;max-width:520px}}
  .ccy-note{{}}
  .ccy-bar{{
    display:flex;width:100%;height:18px;border-radius:4px;overflow:hidden;
    border:1px solid var(--border);background:var(--ink-soft);
  }}
  .ccy-seg{{height:100%;transition:filter 0.15s}}
  .ccy-seg:hover{{filter:brightness(1.15)}}
  .ccy-legend{{
    display:flex;flex-wrap:wrap;gap:14px;margin-top:10px;
    font-family:var(--font-mono);font-size:11px;color:var(--text-dim);
  }}
  .ccy-legend-row{{display:flex;align-items:center;gap:6px}}
  .ccy-legend-swatch{{width:10px;height:10px;border-radius:2px;display:inline-block}}
  .ccy-legend-ccy{{color:var(--text);font-weight:600}}
  .ccy-legend-share{{color:var(--text);font-feature-settings:'tnum'}}
  .ccy-legend-n{{opacity:0.65}}
  .div-head h3{{margin:0 0 4px;font-family:var(--font-display);font-size:18px;
    font-weight:400;color:var(--text);letter-spacing:-0.01em}}
  .div-head h3 .muted{{color:var(--text-dim);font-size:14px;margin-left:4px}}
  .div-head p.muted{{margin:0 0 14px;font-family:var(--font-ui);font-size:12px;
    color:var(--text-dim);line-height:1.5}}
  .div-grid{{display:grid;grid-template-columns:1fr 1.4fr 1.4fr;gap:12px;margin-bottom:16px}}
  .div-card{{background:var(--ink-soft);border:1px solid var(--border);border-radius:10px;
    padding:14px 16px;display:flex;flex-direction:column;gap:8px}}
  .div-card-headline{{justify-content:center;align-items:flex-start}}
  .div-label{{font-family:var(--font-mono);font-size:9.5px;color:var(--text-dim);
    text-transform:uppercase;letter-spacing:0.14em;font-weight:600}}
  .div-headline{{font-family:var(--font-display);font-size:42px;font-weight:400;
    letter-spacing:-0.02em;color:var(--text);line-height:1}}
  .div-headline.pos{{color:var(--up)}}
  .div-headline.neg{{color:var(--down)}}
  .div-meta{{font-family:var(--font-mono);font-size:10px;color:var(--text-dim);
    letter-spacing:0.04em;text-transform:uppercase}}
  .div-list{{list-style:none;padding:0;margin:0;display:flex;flex-direction:column;gap:8px}}
  .div-row{{display:flex;justify-content:space-between;align-items:flex-start;gap:10px}}
  .div-pair{{flex:1 1 auto;min-width:0}}
  .div-pair-syms{{font-family:var(--font-mono);font-size:13px;font-weight:600;
    color:var(--text);letter-spacing:-0.01em}}
  .div-pair-sub{{font-family:var(--font-ui);font-size:10.5px;color:var(--text-dim);
    line-height:1.3;margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
  .div-val{{font-family:var(--font-mono);font-size:14px;font-weight:600;
    letter-spacing:-0.01em;flex-shrink:0}}
  .div-val-hot{{color:var(--down)}}
  .div-val-cool{{color:var(--up)}}
  .div-histogram{{padding-top:14px;border-top:1px solid var(--border)}}
  .div-hist-label{{font-family:var(--font-mono);font-size:9.5px;color:var(--text-dim);
    text-transform:uppercase;letter-spacing:0.14em;font-weight:600;margin-bottom:10px}}
  .div-hist-bars{{display:flex;align-items:flex-end;gap:6px;height:80px}}
  .div-hist-bar{{flex:1 1 0;background:var(--accent);border-radius:2px 2px 0 0;
    min-height:2px;transition:opacity .15s;cursor:default}}
  .div-hist-bar:hover{{opacity:.75}}
  .div-hist-xlabels{{display:flex;gap:6px;margin-top:4px}}
  .div-hist-xlabels span{{flex:1 1 0;text-align:center;font-family:var(--font-mono);
    font-size:9px;color:var(--text-dim)}}
  @media (max-width:900px){{
    .div-grid{{grid-template-columns:1fr;gap:10px}}
    .div-headline{{font-size:36px}}
  }}

  /* Industry outlook */
  .industry-section{{
    background:linear-gradient(180deg,var(--surface) 0%,var(--ink-soft) 100%);
    border:1px solid var(--border);border-radius:12px;padding:18px 20px;
    margin:14px 0 8px;
  }}
  .io-head-row h3{{margin:0 0 4px;font-family:var(--font-display);font-size:18px;
    font-weight:400;color:var(--text);letter-spacing:-0.01em}}
  .io-head-row h3 .muted{{color:var(--text-dim);font-size:14px;margin-left:4px}}
  .io-head-row p.muted{{margin:0 0 14px;font-family:var(--font-ui);font-size:12px;
    color:var(--text-dim);line-height:1.5}}
  .io-head-row code{{font-family:var(--font-mono);font-size:11px;color:var(--text-2);
    background:var(--surface-2);padding:1px 5px;border-radius:3px}}
  .io-head-row strong{{color:var(--text-2);font-weight:500}}
  .io-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px}}
  .io-card{{
    background:var(--ink-soft);border:1px solid var(--border);border-radius:10px;
    padding:14px 16px;display:flex;flex-direction:column;gap:8px;
  }}
  .io-head{{display:flex;justify-content:space-between;align-items:baseline;gap:8px}}
  .io-industry{{font-family:var(--font-ui);font-size:13px;font-weight:600;color:var(--text);
    line-height:1.2;letter-spacing:-0.005em}}
  .io-avg{{font-family:var(--font-mono);font-size:13px;font-weight:600;letter-spacing:-0.01em}}
  .io-avg.pos{{color:var(--up)}} .io-avg.neg{{color:var(--down)}}
  .io-sub{{font-family:var(--font-mono);font-size:10px;color:var(--text-dim);
    text-transform:uppercase;letter-spacing:0.08em;margin-bottom:4px}}
  .io-stocks{{display:flex;flex-direction:column;gap:6px}}
  .io-stock{{display:grid;grid-template-columns:auto auto 1fr;gap:10px;align-items:center;
    padding:5px 0;border-top:1px solid var(--border);cursor:pointer;transition:opacity 0.15s}}
  .io-stock:first-child{{border-top:none;padding-top:0}}
  .io-stock:hover{{opacity:0.75}}
  .io-tkr{{font-family:var(--font-mono);font-size:12px;font-weight:600;color:var(--text);
    min-width:60px;display:flex;align-items:center;gap:6px}}
  .io-tier{{font-family:var(--font-mono);font-size:8.5px;font-weight:600;letter-spacing:0.06em;
    padding:1px 4px;border-radius:3px;border:1px solid;line-height:1.1}}
  .io-tier-mega{{color:var(--accent);border-color:var(--accent)}}
  .io-tier-large{{color:var(--text-2);border-color:var(--border)}}
  .io-tier-mid{{color:var(--up);border-color:var(--up);background:rgba(52,211,153,0.06)}}
  .io-tier-small{{color:var(--down);border-color:var(--down);background:rgba(248,113,113,0.06)}}
  .io-ret{{font-family:var(--font-mono);font-size:11.5px;font-weight:500;text-align:right;
    min-width:55px}}
  .io-ret.pos{{color:var(--up)}} .io-ret.neg{{color:var(--down)}}
  .io-meta{{display:flex;gap:8px;align-items:center;font-family:var(--font-mono);font-size:10.5px;
    justify-content:flex-end}}
  .io-meta .an-rec{{font-size:9px;padding:1px 5px}}
  .io-up.pos{{color:var(--up)}} .io-up.neg{{color:var(--down)}}
  .io-no-cov{{color:var(--text-dim);font-style:italic;font-size:10px}}
  @media (max-width:900px){{
    .io-grid{{grid-template-columns:1fr;gap:10px}}
  }}

  /* News panel */
  .news-head{{display:flex;justify-content:space-between;align-items:baseline;gap:10px;margin-bottom:10px}}
  .news-chips{{display:flex;flex-wrap:wrap;gap:4px;margin-bottom:8px}}
  .news-chip{{
    background:var(--surface);border:1px solid var(--border);color:var(--text-dim);
    font-family:var(--font-mono);font-size:9.5px;font-weight:500;letter-spacing:0.04em;
    padding:3px 8px;border-radius:999px;cursor:pointer;transition:all 0.12s;
    text-transform:uppercase;
  }}
  .news-chip:hover{{color:var(--text);border-color:var(--text-dim)}}
  .news-chip.active{{background:var(--accent);color:var(--ink);border-color:var(--accent);font-weight:600}}
  .news-row[hidden]{{display:none}}
  .news-stale{{font-family:var(--font-mono);font-size:10px;letter-spacing:0.04em;color:var(--text-dim)}}
  .news-stale.news-live{{color:var(--up)}}
  .news-stale.news-live::before{{content:"";display:inline-block;width:6px;height:6px;border-radius:50%;
    background:var(--up);margin-right:6px;vertical-align:1px;
    box-shadow:0 0 0 0 var(--up);animation:pulse 2.4s ease-out infinite}}
  .news-list{{display:flex;flex-direction:column;gap:0;max-height:340px;overflow-y:auto}}
  .news-row{{display:block;padding:10px 0;border-bottom:1px solid var(--border);
    text-decoration:none;color:inherit;transition:background 0.12s}}
  .news-row:last-child{{border-bottom:none}}
  .news-row:hover{{background:rgba(245,158,11,0.04)}}
  .news-title{{font-family:var(--font-ui);font-size:12.5px;color:var(--text);line-height:1.35;
    font-weight:500}}
  .news-row:hover .news-title{{color:var(--accent)}}
  .news-meta{{font-family:var(--font-mono);font-size:10px;color:var(--text-dim);
    margin-top:4px;letter-spacing:0.02em}}
  .news-src{{color:var(--text-2)}}
  .news-dot{{margin:0 6px;opacity:0.5}}
  @media (max-width:900px){{
    .wl-news-row{{grid-template-columns:1fr;gap:10px}}
    .outlook-news-row{{grid-template-columns:1fr;gap:10px}}
    .outlook-news-row > section{{min-height:auto}}
    .wl-grid{{grid-template-columns:repeat({WATCH_COLS_MOBILE},minmax(0,1fr));gap:8px}}
  }}

  .untracked{{margin:32px 0 8px;padding:18px 20px;border:1px solid var(--border);
    border-radius:12px;background:var(--surface);}}
  .untracked-head h3{{margin:0 0 4px;font-family:var(--font-display);font-size:18px;
    font-weight:400;color:var(--text);letter-spacing:-0.01em}}
  .untracked-head h3 .muted{{color:var(--text-dim);font-size:14px;margin-left:4px}}
  .untracked-head p.muted{{margin:0 0 14px;font-family:var(--font-ui);font-size:12px;
    color:var(--text-dim);line-height:1.5}}
  .untracked-head code{{font-family:var(--font-mono);font-size:11px;color:var(--text-2);
    background:var(--surface-2);padding:1px 5px;border-radius:3px}}
  .untracked-table{{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}}
  .untracked-table th{{text-align:left;padding:8px 10px;border-bottom:1px solid var(--border);
    font-family:var(--font-ui);font-size:10px;text-transform:uppercase;letter-spacing:0.06em;
    color:var(--text-dim);font-weight:500}}
  .untracked-table th.num{{text-align:right}}
  .untracked-table td{{padding:9px 10px;border-bottom:1px solid var(--border);
    font-family:var(--font-ui);font-size:12.5px;color:var(--text-2)}}
  .untracked-table tbody tr:last-child td{{border-bottom:none}}
  .untracked-table .ut-date{{font-family:var(--font-mono);font-size:11.5px;color:var(--text-dim);
    white-space:nowrap;width:80px}}
  .untracked-table .ut-action{{font-family:var(--font-mono);font-size:10.5px;
    text-transform:uppercase;letter-spacing:0.05em;font-weight:500;width:90px}}
  .untracked-table .ut-action.ut-pos{{color:var(--up)}}
  .untracked-table .ut-action.ut-neg{{color:var(--down)}}
  .untracked-table .ut-name{{color:var(--text)}}
  .untracked-table .ut-price{{font-family:var(--font-mono);font-size:12px;color:var(--text-2);
    text-align:right;white-space:nowrap}}

  /* Single panel layout (tabs removed in v9 — modal handles per-ticker drill) */
  .panel{{display:block;margin-top:32px}}

  /* Toolbar */
  .toolbar{{display:flex;flex-wrap:wrap;gap:14px;align-items:center;margin-bottom:18px}}
  .search{{flex:1 1 240px;background:var(--surface);border:1px solid var(--border);color:var(--text);
    font-family:var(--font-mono);font-size:13px;padding:10px 14px;border-radius:8px;outline:none;
    transition:border-color 0.15s,background 0.15s}}
  .search:focus{{border-color:var(--accent);background:var(--surface-2)}}
  .search::placeholder{{color:var(--text-dim)}}
  .chips{{display:flex;gap:6px;flex-wrap:wrap}}
  .chip{{background:var(--surface);border:1px solid var(--border);color:var(--text-2);
    font-family:var(--font-ui);font-size:12px;font-weight:500;padding:8px 14px;border-radius:999px;
    cursor:pointer;transition:all 0.15s;letter-spacing:0.01em}}
  .chip:hover{{border-color:var(--text-dim);color:var(--text)}}
  .chip.active{{background:var(--accent);color:var(--ink);border-color:var(--accent);font-weight:600}}
  .chips-sectors{{margin-top:8px;width:100%}}
  .chip.chip-sm{{padding:5px 10px;font-size:10.5px;font-family:var(--font-mono);
    letter-spacing:0.03em;text-transform:none}}
  .chip-count{{color:var(--text-dim);font-weight:500;margin-left:4px;font-size:9.5px}}
  .chip-sm.active .chip-count{{color:var(--ink);opacity:0.7}}

  /* Table — internal vertical scroll, no horizontal scroll at desktop sizes */
  .table-scroll{{width:100%;max-height:560px;overflow-y:auto;overflow-x:auto;
    -webkit-overflow-scrolling:touch;background:var(--surface);
    border:1px solid var(--border);border-radius:12px}}
  table#ret-table{{width:100%;border-collapse:collapse;background:transparent;
    font-variant-numeric:tabular-nums;min-width:0;table-layout:auto}}
  #ret-table th,#ret-table td{{padding:9px 10px;text-align:left;border-bottom:1px solid var(--border)}}
  #ret-table th{{font-size:10px;font-weight:600;color:var(--text-dim);text-transform:uppercase;
    letter-spacing:0.14em;background:var(--ink-soft);user-select:none;position:sticky;top:0;z-index:5}}
  #ret-table th[data-col]{{cursor:pointer}}
  #ret-table th[data-col]:hover{{color:var(--text)}}
  #ret-table th.num{{text-align:right}}
  #ret-table th[data-col]::after{{content:"";display:inline-block;width:0;height:0;margin-left:6px;
    vertical-align:2px;opacity:0;transition:opacity 0.15s}}
  #ret-table th[data-col].sort-asc::after{{opacity:1;border-left:4px solid transparent;
    border-right:4px solid transparent;border-bottom:5px solid var(--accent)}}
  #ret-table th[data-col].sort-desc::after{{opacity:1;border-left:4px solid transparent;
    border-right:4px solid transparent;border-top:5px solid var(--accent)}}
  #ret-table th[data-col].sort-asc,#ret-table th[data-col].sort-desc{{color:var(--text)}}
  #ret-table td{{font-size:13px;color:var(--text-2)}}
  #ret-table td.num{{text-align:right;font-family:var(--font-mono);font-size:12.5px;color:var(--text)}}
  #ret-table td.num.pos{{color:var(--up);font-weight:500}}
  #ret-table td.num.neg{{color:var(--down);font-weight:500}}
  #ret-table td.dim{{color:var(--text-dim);font-weight:400}}
  #ret-table td.purchased{{font-size:11.5px;color:var(--text-2);letter-spacing:0.02em}}
  #ret-table td.t-signal{{font-family:var(--font-ui);min-width:110px;padding:7px 10px;line-height:1.2}}
  /* Analyst columns — target / upside / rec, slotted between Ticker and Signal */
  #ret-table td.t-target{{padding:7px 8px;min-width:70px;font-size:12px}}
  #ret-table td.t-upside{{padding:7px 6px;min-width:55px;font-size:12px}}
  #ret-table td.t-rec{{padding:7px 8px;min-width:95px;line-height:1.2}}
  #ret-table td.t-rec .an-rec{{font-size:9px;padding:1px 5px;letter-spacing:0.05em}}
  #ret-table td.t-rec-count,#ret-table .t-rec-count{{
    font-family:var(--font-mono);font-size:9.5px;color:var(--text-dim);margin-top:3px;
  }}
  #ret-table td.t-signal.dim{{font-family:var(--font-mono);text-align:left}}
  .sig-main{{font-weight:500;font-size:12px;letter-spacing:0.01em}}
  .sig-detail{{font-family:var(--font-mono);font-size:10px;color:var(--text-dim);margin-top:3px;font-weight:400;letter-spacing:-0.01em}}
  .sig-pos .sig-main{{color:var(--up)}}
  .sig-neg .sig-main{{color:var(--down)}}
  .sig-neutral .sig-main{{color:var(--accent)}}
  #ret-table td.t-ticker{{font-weight:600;font-family:var(--font-mono);font-size:13px;color:var(--text);min-width:160px}}
  .tkr-main{{line-height:1.2;display:flex;align-items:center;gap:8px}}
  .badge-weight{{
    font-family:var(--font-mono);font-size:9.5px;background:var(--accent);color:var(--ink);
    padding:1px 6px;border-radius:4px;font-weight:600;letter-spacing:0;
  }}
  .badge-ccy{{
    font-family:var(--font-mono);font-size:9.5px;background:transparent;color:var(--text-2);
    padding:1px 5px;border-radius:4px;font-weight:500;letter-spacing:0.02em;
    border:1px solid var(--border);
  }}
  .badge-closed{{
    font-family:var(--font-mono);font-size:9px;background:transparent;color:var(--text-dim);
    padding:1px 5px;border-radius:4px;font-weight:600;letter-spacing:0.08em;
    border:1px solid var(--text-dim);
  }}
  .badge-watch{{
    font-family:var(--font-mono);font-size:9px;background:transparent;color:var(--accent);
    padding:1px 5px;border-radius:4px;font-weight:600;letter-spacing:0.08em;
    border:1px solid var(--accent);
  }}
  .tkr-sub{{font-family:var(--font-ui);font-size:10.5px;color:var(--text-dim);font-weight:400;margin-top:2px;
    overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:200px}}
  .num-main{{line-height:1.2}}
  .num-sub{{font-family:var(--font-mono);font-size:10px;color:var(--text-dim);font-weight:400;margin-top:2px}}
  #ret-table td.t-spark{{padding:4px 16px;width:110px}}
  #ret-table tbody tr{{transition:background 0.12s;cursor:pointer}}
  #ret-table tbody tr:hover{{background:var(--surface-2)}}
  #ret-table tbody tr:last-child td{{border-bottom:none}}
  .sparkline{{display:block;width:90px;height:28px}}
  .sparkline.up{{color:var(--up)}}
  .sparkline.down{{color:var(--down)}}
  tbody tr.hidden{{display:none}}

  /* Row entry animation (chart-grid CSS removed in v9 along with Charts tab) */
  tbody tr{{opacity:0;animation:rowIn 0.4s ease forwards}}
  @keyframes rowIn{{from{{opacity:0;transform:translateY(6px)}}to{{opacity:1;transform:none}}}}

  /* Modal */
  .modal{{position:fixed;inset:0;z-index:100;display:flex;align-items:center;justify-content:center;
    background:rgba(8,11,18,0.78);backdrop-filter:blur(12px);padding:20px;animation:modalIn 0.2s ease}}
  .modal[hidden]{{display:none}}
  /* T11/T12/T14/T15 info-modal: lower z-index than the ticker modal so it
     visually layers UNDER any ticker modal opened on top. */
  .info-modal{{z-index:90;background:rgba(8,11,18,0.55)}}
  .info-modal .modal-card{{max-width:760px}}
  .info-modal-head{{margin-bottom:14px;padding-right:36px}}
  .info-modal-title{{font-family:var(--font-display);font-size:22px;font-weight:500;
    margin:0 0 4px;color:var(--text);letter-spacing:-0.01em}}
  .info-modal-sub{{font-family:var(--font-mono);font-size:11px;color:var(--text-dim);
    letter-spacing:0.04em}}
  .info-modal-body{{font-family:var(--font-ui);color:var(--text)}}
  /* Shared modal table style for industry / sector / pair / mover lists */
  .im-table{{width:100%;border-collapse:collapse;font-family:var(--font-mono);font-size:11.5px}}
  .im-table th{{text-align:left;padding:6px 8px;color:var(--text-dim);
    text-transform:uppercase;letter-spacing:0.06em;font-size:10px;font-weight:600;
    border-bottom:1px solid var(--border)}}
  .im-table th.num,.im-table td.num{{text-align:right}}
  .im-table td{{padding:8px;border-bottom:1px solid var(--border)}}
  .im-table tbody tr:last-child td{{border-bottom:none}}
  .im-table tbody tr.ticker-clickable{{cursor:pointer;transition:background 0.1s}}
  .im-table tbody tr.ticker-clickable:hover{{background:var(--surface-2)}}
  .im-tkr{{font-weight:600;letter-spacing:0.04em;color:var(--text)}}
  .im-tkr.ticker-clickable{{cursor:pointer}}
  .im-tkr.ticker-clickable:hover{{color:var(--accent)}}
  .im-name{{color:var(--text-dim)}}
  .im-arrow{{color:var(--text-dim);text-align:center}}
  .im-tier{{display:inline-block;font-size:8.5px;padding:1px 5px;border-radius:4px;
    margin-left:6px;text-transform:uppercase;letter-spacing:0.04em;vertical-align:1px;
    background:var(--surface-2);color:var(--text-dim);border:1px solid var(--border)}}
  .im-tier-mega{{background:rgba(245,158,11,0.18);color:var(--accent);border-color:var(--accent)}}
  .im-tier-large{{background:rgba(52,211,153,0.12);color:var(--up);border-color:var(--up)}}
  /* T15 movers: two-column layout (up | down) inside the info-modal */
  .im-movers{{display:grid;grid-template-columns:1fr 1fr;gap:18px}}
  .im-movers-h{{font-family:var(--font-mono);font-size:11px;text-transform:uppercase;
    letter-spacing:0.08em;margin:0 0 8px;font-weight:600}}
  @media (max-width:600px){{
    .im-movers{{grid-template-columns:1fr}}
  }}
  /* T11 hint chip on industry-outlook cards */
  .io-expand-hint{{color:var(--text-dim);font-size:9.5px;margin-left:6px;
    text-transform:uppercase;letter-spacing:0.06em;opacity:0.6;transition:opacity 0.2s}}
  .industry-clickable{{cursor:pointer}}
  .industry-clickable:hover .io-expand-hint{{opacity:1;color:var(--accent)}}
  /* T12 attribution rows are clickable */
  .attribution-row-clickable{{cursor:pointer;transition:background 0.12s}}
  .attribution-row-clickable:hover{{background:var(--surface-2)}}
  /* T14 histogram columns: each column wraps a variable-height bar with
     a full-height clickable area, so even one-pair buckets are easy to hit. */
  .div-hist-col{{position:relative;display:flex;align-items:flex-end;
    flex:1 1 0;height:100%;min-width:0}}
  .div-hist-col-clickable{{cursor:pointer;transition:background 0.12s}}
  .div-hist-col-clickable:hover{{background:rgba(245,158,11,0.06)}}
  .div-hist-col-clickable:hover .div-hist-bar{{filter:brightness(1.25)}}
  /* Universe-only fallback modal style */
  .im-uni-note{{margin:14px 0 0;font-size:11px;line-height:1.5}}
  .im-uni-table td{{padding:8px}}
  /* T15 hero week-click rects: pointer cursor on hover */
  .hero-week-click{{cursor:pointer}}
  @keyframes modalIn{{from{{opacity:0}}to{{opacity:1}}}}
  .modal-card{{
    background:linear-gradient(180deg,var(--surface-2) 0%,var(--surface) 100%);
    border:1px solid var(--border);border-radius:16px;width:100%;max-width:920px;max-height:92vh;
    padding:28px;position:relative;overflow:auto;box-shadow:0 30px 80px -10px rgba(0,0,0,0.6);
    animation:modalSlide 0.25s ease;
  }}
  @keyframes modalSlide{{from{{opacity:0;transform:translateY(12px) scale(0.98)}}to{{opacity:1;transform:none}}}}
  .modal-close{{position:absolute;top:14px;right:14px;background:transparent;border:1px solid var(--border);
    color:var(--text-2);width:34px;height:34px;border-radius:8px;cursor:pointer;font-size:18px;line-height:1;
    transition:all 0.15s}}
  .modal-close:hover{{border-color:var(--accent);color:var(--accent)}}
  .modal-head{{display:flex;justify-content:space-between;align-items:flex-start;gap:24px;margin-bottom:24px;padding-right:50px}}
  .modal-ticker{{font-family:var(--font-mono);font-size:32px;font-weight:600;margin:0;color:var(--text);line-height:1;display:flex;align-items:center;gap:10px}}
  .modal-ticker .badge-weight{{font-size:13px;padding:3px 9px}}
  .modal-name{{font-family:var(--font-display);font-size:24px;color:var(--text-2);margin-top:4px;font-style:italic}}
  .modal-industry{{font-family:var(--font-ui);font-size:11.5px;color:var(--text-dim);text-transform:uppercase;letter-spacing:0.15em;margin-top:10px;font-weight:500}}
  .modal-headline{{text-align:right;flex-shrink:0}}
  .modal-pct{{font-family:var(--font-display);font-size:54px;font-weight:400;line-height:1;letter-spacing:-0.015em;font-variant-numeric:tabular-nums}}
  .modal-since{{font-family:var(--font-mono);font-size:11px;color:var(--text-dim);margin-top:6px;letter-spacing:0.05em}}
  .modal-signal{{
    display:flex;align-items:center;gap:12px;margin-bottom:14px;
    padding:10px 16px;background:var(--ink-soft);border:1px solid var(--border);
    border-radius:8px;font-family:var(--font-ui);font-size:12.5px;
  }}
  .modal-signal[hidden]{{display:none}}
  .modal-signal-label{{font-size:9.5px;color:var(--text-dim);text-transform:uppercase;letter-spacing:0.18em;font-weight:600}}
  .modal-signal-text{{font-weight:600;font-size:13.5px}}
  .modal-signal-text.pos{{color:var(--up)}}
  .modal-signal-text.neg{{color:var(--down)}}
  .modal-signal-text.neutral{{color:var(--accent)}}
  .modal-signal-detail{{font-family:var(--font-mono);font-size:11.5px;color:var(--text-2);margin-left:auto;letter-spacing:-0.01em}}
  .modal-fx{{
    margin-bottom:14px;padding:10px 16px;background:var(--ink-soft);
    border:1px solid var(--border);border-radius:8px;
  }}
  .modal-fx[hidden]{{display:none}}
  .modal-fx-label{{font-size:9.5px;color:var(--text-dim);text-transform:uppercase;letter-spacing:0.18em;font-weight:600;display:block;margin-bottom:8px}}
  .modal-fx-row{{display:flex;align-items:center;gap:10px;flex-wrap:wrap;font-family:var(--font-mono);font-size:12.5px}}
  .modal-fx-part{{display:flex;align-items:baseline;gap:6px}}
  .modal-fx-part em{{font-style:normal;color:var(--text-dim);font-size:10.5px;letter-spacing:0.1em;text-transform:uppercase;font-family:var(--font-ui);font-weight:600}}
  .modal-fx-part b{{font-weight:500}}
  .modal-fx-part b.pos{{color:var(--up)}}
  .modal-fx-part b.neg{{color:var(--down)}}
  .modal-fx-sep{{color:var(--text-dim);opacity:0.6}}
  .modal-stats{{display:grid;grid-template-columns:repeat(6,1fr);gap:10px;margin-bottom:22px;
    padding:14px;background:var(--ink-soft);border:1px solid var(--border);border-radius:10px}}
  .modal-stat{{display:flex;flex-direction:column;gap:2px}}
  .modal-stat-label{{font-size:9.5px;color:var(--text-dim);text-transform:uppercase;letter-spacing:0.16em;font-weight:600}}
  .modal-stat-val{{font-family:var(--font-mono);font-size:14px;font-weight:500;color:var(--text)}}
  .modal-stat-val.pos{{color:var(--up)}}
  .modal-stat-val.neg{{color:var(--down)}}
  .modal-stat-val.warn{{color:var(--accent)}}
  .modal-stat-val.dim{{color:var(--text-dim)}}
  .modal-stat-meta{{font-family:var(--font-ui);font-size:10px;color:var(--text-dim);
    margin-top:2px;line-height:1.2;letter-spacing:0.02em}}
  /* "Quant signals" sub-row: SMA200 distance / ATR / RSI / 52w position / Volume */
  .modal-quant-head{{font-family:var(--font-mono);font-size:9.5px;color:var(--text-dim);
    text-transform:uppercase;letter-spacing:0.16em;font-weight:600;
    margin:6px 0 8px;padding-left:2px;display:flex;align-items:center;gap:10px}}
  .modal-quant-head::after{{content:"";flex:1;height:1px;background:var(--border);opacity:0.6}}
  .modal-quant-stats{{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin-bottom:22px;
    padding:14px;background:var(--ink-soft);border:1px solid var(--border);border-radius:10px}}
  /* Per-ticker "Recent news" — fed from the 7-day TTL cache in build.py */
  .modal-news{{margin-bottom:22px}}
  .modal-news[hidden]{{display:none}}
  .modal-news-head{{font-family:var(--font-mono);font-size:9.5px;color:var(--text-dim);
    text-transform:uppercase;letter-spacing:0.16em;font-weight:600;
    margin:6px 0 8px;padding-left:2px;display:flex;align-items:center;gap:10px}}
  .modal-news-head::after{{content:"";flex:1;height:1px;background:var(--border);opacity:0.6}}
  .modal-news-staleness{{order:3;flex:none;color:var(--text-dim);
    font-size:9px;letter-spacing:0.06em;text-transform:none}}
  .modal-news-list{{display:flex;flex-direction:column;gap:1px;
    background:var(--ink-soft);border:1px solid var(--border);border-radius:10px;overflow:hidden}}
  .modal-news-row{{display:flex;flex-direction:column;gap:3px;padding:10px 14px;
    text-decoration:none;color:inherit;border-top:1px solid var(--border);transition:background 0.12s}}
  .modal-news-row:first-child{{border-top:none}}
  .modal-news-row:hover{{background:var(--surface-2)}}
  .modal-news-title{{font-family:var(--font-ui);font-size:13px;line-height:1.35;color:var(--text);
    font-weight:500}}
  .modal-news-meta{{font-family:var(--font-mono);font-size:10px;color:var(--text-dim);
    display:flex;align-items:center;gap:8px;letter-spacing:0.02em}}
  .modal-news-pub{{color:var(--text-2)}}
  .modal-news-dot{{color:var(--text-dim);opacity:0.6}}
  .modal-news-when{{color:var(--text-dim)}}
  .modal-news-empty{{padding:14px;font-family:var(--font-ui);font-size:12px;
    color:var(--text-dim);font-style:italic;text-align:center}}
  .modal-chart-wrap{{position:relative;width:100%;height:340px;background:var(--ink-soft);border:1px solid var(--border);border-radius:10px;padding:16px}}
  .modal-chart{{width:100%;height:100%;display:block}}
  /* v2.1 #1: axis-label HTML overlay. Sits above the SVG at inset:16px so its
     bounding box matches the SVG's pixel rect (which fills the chart-wrap minus
     the 16px padding). Labels position via CSS percentages mapped from viewBox
     coords -- so text renders at native browser DPI, immune to the SVG's
     preserveAspectRatio="none" horizontal stretch. */
  .modal-chart-labels{{position:absolute;inset:16px;pointer-events:none;z-index:1}}
  .modal-chart-labels .y-tick{{position:absolute;transform:translate(-100%,-50%);
    font-family:var(--font-mono);font-size:10.5px;color:#6b7185;
    padding-right:8px;white-space:nowrap;line-height:1}}
  .modal-chart-labels .x-tick{{position:absolute;transform:translate(-50%,0);
    font-family:var(--font-mono);font-size:10.5px;color:#6b7185;
    padding-top:4px;white-space:nowrap;line-height:1}}
  .modal-chart-labels .txn-marker{{position:absolute;transform:translate(-50%,-50%);
    font-family:var(--font-mono);font-size:11px;font-weight:700;color:#0b0e17;
    line-height:1}}
  /* cost-basis tick: marks the active-cycle baseline date on the full path */
  .modal-chart-labels .cost-tick{{position:absolute;transform:translate(-50%,0);
    font-family:var(--font-mono);font-size:9.5px;color:#fbbf24;
    padding-top:3px;white-space:nowrap;line-height:1;letter-spacing:.04em}}
  /* v2.0 lazy-modal: spinner shown in the chart area while the sidecar
     payload is being fetched on first modal-open. Hidden once HEAVY merges
     into DATA[tkr]. Subsequent opens reuse the cache -- no spinner. */
  .modal-loading{{position:absolute;inset:16px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:12px;background:var(--ink-soft);border-radius:8px;z-index:3;pointer-events:none}}
  .modal-loading[hidden]{{display:none}}
  .modal-spinner{{width:28px;height:28px;border:2.5px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:modalSpin 0.8s linear infinite}}
  .modal-loading-text{{font-family:var(--font-mono);font-size:11px;color:var(--text-dim);letter-spacing:0.04em;text-transform:uppercase}}
  @keyframes modalSpin{{to{{transform:rotate(360deg)}}}}
  .modal-tip{{position:absolute;background:var(--ink);border:1px solid var(--accent);border-radius:6px;
    padding:8px 12px;font-family:var(--font-mono);font-size:11.5px;color:var(--text);pointer-events:none;
    white-space:nowrap;transform:translate(-50%,-100%);margin-top:-10px;z-index:2;
    box-shadow:0 4px 12px rgba(0,0,0,0.4)}}
  .modal-tip[hidden]{{display:none}}
  .modal-tip .tip-date{{color:var(--text-dim);font-size:10.5px;margin-bottom:2px}}
  .modal-tip .tip-price{{font-weight:500}}
  .modal-tip .tip-pct{{margin-left:6px;font-weight:500}}

  footer{{padding:36px 0 24px;color:var(--text-dim);font-family:var(--font-mono);font-size:10.5px;text-align:center}}

  @media (max-width:720px){{
    body{{overflow-x:hidden}}
    .container{{padding:0 14px}}
    .table-scroll{{margin:0 -14px;padding:0;border-radius:0;border-left:none;border-right:none;width:auto}}
    #ret-table{{min-width:520px}}
    #ret-table td.t-signal{{min-width:110px;padding:8px 10px}}
    .sig-detail{{display:none}}
    header{{padding:24px 0 18px}}
    h1{{margin:10px 0 22px}}
    .hero-chart-wrap{{padding:16px 14px 10px}}
    .hero-chart-svg-wrap{{height:240px}}
    .stats{{grid-template-columns:repeat(2,1fr);gap:10px}}
    .stat{{padding:12px 14px}}
    .stat-value{{font-size:28px}}
    .stat-label{{font-size:9.5px}}
    .contributors{{grid-template-columns:1fr;gap:10px;margin:18px 0 4px}}
    .contrib-col{{padding:14px}}
    .contrib-table td,.contrib-table th{{padding:7px 5px;font-size:11.5px}}
    #ret-table th,#ret-table td{{padding:9px 8px;font-size:12px}}
    #ret-table td.num{{font-size:11.5px}}
    #ret-table td.t-spark{{padding:4px 8px;width:80px}}
    .sparkline{{width:70px;height:24px}}
    #ret-table td.t-ticker{{min-width:0}}
    .tkr-sub{{max-width:120px;font-size:9.5px}}
    .badge-weight{{font-size:8.5px;padding:1px 4px}}
    #ret-table th{{font-size:9.5px;letter-spacing:0.1em}}
    .dim-mobile{{display:none}}
    .toolbar{{gap:10px}}
    .chip{{padding:7px 11px;font-size:11px}}
    .modal{{padding:0;align-items:stretch}}
    .modal-card{{border-radius:0;max-height:100vh;padding:20px 16px;border:none}}
    .modal-head{{flex-direction:column;gap:8px;padding-right:42px}}
    .modal-headline{{text-align:left}}
    .modal-pct{{font-size:42px}}
    .modal-ticker{{font-size:26px}}
    .modal-name{{font-size:18px}}
    .modal-signal{{flex-wrap:wrap;padding:10px 12px;font-size:11.5px;gap:8px}}
    .modal-signal-detail{{margin-left:0;width:100%;font-size:10.5px}}
    .modal-stats{{grid-template-columns:repeat(3,1fr);gap:10px;padding:12px}}
    .modal-quant-stats{{grid-template-columns:repeat(3,1fr);gap:10px;padding:12px}}
    .modal-stat-val{{font-size:13px}}
    .modal-chart-wrap{{height:240px;padding:10px}}
  }}
</style>
</head>
<body>
{demo_banner_html}
{defs_html}
<div class="container">

<div class="topbar">
  <!-- v2.1: icon-only topbar. Each button shows a single SVG glyph + a
       data-tooltip that slides in on hover with the human-readable name.
       Saves ~70% horizontal space vs the v2.0 text labels. -->
  <button class="layout-toggle icon-btn" id="edit-layout-btn" type="button" aria-pressed="false"
          aria-label="Edit layout" data-tooltip="Edit layout">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <line x1="4" y1="6" x2="13" y2="6"/><line x1="19" y1="6" x2="20" y2="6"/><circle cx="16" cy="6" r="2"/>
      <line x1="4" y1="12" x2="7" y2="12"/><line x1="13" y1="12" x2="20" y2="12"/><circle cx="10" cy="12" r="2"/>
      <line x1="4" y1="18" x2="15" y2="18"/><circle cx="18" cy="18" r="2"/>
    </svg>
  </button>
  <button class="layout-reset icon-btn" id="reset-layout-btn" type="button" hidden
          aria-label="Reset layout" data-tooltip="Reset layout">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <path d="M3 7v6h6"/>
      <path d="M21 17a9 9 0 0 0-15-6.7L3 13"/>
    </svg>
  </button>
  <button class="layout-toggle icon-btn desktop-mode-btn" id="desktop-mode-btn" type="button" aria-pressed="false"
          aria-label="Force desktop view" data-tooltip="Desktop view">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <rect x="2" y="3" width="20" height="14" rx="2"/>
      <line x1="8" y1="21" x2="16" y2="21"/>
      <line x1="12" y1="17" x2="12" y2="21"/>
    </svg>
  </button>
  <!-- v1.9 Pocket Lesson: quick toggle for the educational tip card.
       State persisted in localStorage as `pocketLessonOn`. -->
  <button class="layout-toggle icon-btn pocket-lesson-btn" id="pocket-lesson-btn" type="button" aria-pressed="false"
          aria-label="Pocket lesson" data-tooltip="Pocket lesson">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <path d="M12 5a3 3 0 1 0-5.997.125 4 4 0 0 0-2.526 5.77 4 4 0 0 0 .556 6.588A4 4 0 1 0 12 18Z"/>
      <path d="M12 5a3 3 0 1 1 5.997.125 4 4 0 0 1 2.526 5.77 4 4 0 0 1-.556 6.588A4 4 0 1 1 12 18Z"/>
    </svg>
  </button>
  <!-- v2.1 Quiz: opens the finance-knowledge quiz modal. State (seen set,
       monthly score) persisted in localStorage as `quizSeen` + `quizMonthly`. -->
  <button class="layout-toggle icon-btn quiz-btn" id="quiz-btn" type="button"
          aria-label="Quiz — 5 categories, 50 questions" data-tooltip="Quiz">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <circle cx="12" cy="12" r="10"/>
      <path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/>
      <line x1="12" y1="17" x2="12.01" y2="17"/>
    </svg>
  </button>
  <!-- v2.1: 4 palette buttons collapsed to a single cycling button. Click
       advances Default -> Soft Dark -> Light -> Amber -> Default. The icon
       is a filled circle in var(--accent) so the active palette is visible
       at a glance; data-tooltip carries the palette name (updated by JS). -->
  <button class="layout-toggle icon-btn palette-cycle-btn" id="palette-cycle-btn" type="button"
          data-palette="default" aria-label="Cycle color palette" data-tooltip="Default">
    <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      <circle cx="12" cy="12" r="8.5"/>
    </svg>
  </button>
</div>

<!-- v1.9 Pocket Lesson card. Sits just below the topbar. Default state is
     collapsed (no `.is-open` class). JS reads localStorage on load and adds
     the class only if the user previously enabled it. Toggle from the topbar
     button slides the card open/closed via a max-height CSS transition. -->
<section class="pocket-lesson-wrap" id="pocket-lesson-wrap" aria-hidden="true" aria-label="Pocket lesson — investment concept of the moment">
  <div class="pocket-lesson-card">
    <div class="pocket-lesson-head">
      <span class="pocket-lesson-eyebrow">Pocket lesson</span>
      <span class="pocket-lesson-title" id="pocket-lesson-title">&mdash;</span>
      <span class="pocket-lesson-cat-pill" id="pocket-lesson-cat-pill"></span>
    </div>
    <p class="pocket-lesson-body" id="pocket-lesson-body">&mdash;</p>
    <div class="pocket-lesson-filters" id="pocket-lesson-filters" role="tablist" aria-label="Filter by category">
      <!-- JS populates with one chip per category + an All chip -->
    </div>
    <div class="pocket-lesson-actions">
      <button type="button" class="pocket-lesson-next" id="pocket-lesson-next"
              title="Show a different tip">Next tip &rarr;</button>
      <span class="pocket-lesson-counter" id="pocket-lesson-counter"></span>
    </div>
  </div>
</section>

<!-- One-time discovery tooltip for the Edit-layout button. Position is
     anchored absolutely (CSS) so its place in the DOM doesn't matter for
     visual layout. JS gates its visibility on the localStorage flag
     'edit-layout-discovered'. -->
<div class="edit-tooltip" id="edit-tooltip" hidden role="status" aria-live="polite">
  Try <strong>Edit layout</strong> &mdash; drag modules, hide what you don&rsquo;t need, reset anytime.
  <span class="edit-tooltip-dismiss">click to dismiss</span>
</div>

<header>
  <div class="eyebrow">{hero_eyebrow}</div>
  <h1>{hero_h1}</h1>

  <div class="hero-chart-wrap">
    <div class="hero-chart-head">
      <div class="hero-head-left">
        <div class="hero-title">Basket vs Benchmark</div>
        <div class="hero-sub">{hero_sub_html}</div>
        {unusual_vol_html}
      </div>
      <div class="hero-legend">
        {hero_legend_html}
      </div>
    </div>
    <div class="hero-chart-svg-wrap">
      <svg class="hero-chart-svg" id="hero-chart" preserveAspectRatio="none"></svg>
      <div class="hero-tip" id="hero-tip" hidden></div>
    </div>
    {alpha_sparkline_html}
    {dd_sparkline_html}
  </div>

  <div class="stats" id="stats-grid"
       data-stats-default="{_stats_default_csv}"
       data-stats-all="{_stats_all_csv}">
    {stats_cards_html}
  </div>

  <div id="last-look" hidden></div>

  <div class="build-info">
    <span class="live"></span>last close {latest_date} &middot; rebuilt {built}{version_html}
  </div>
</header>

<div id="module-stack" data-default-order="{default_order_csv}">
{module_stack_html}
</div>

<footer>Built locally &middot; built with Claude Opus 4.8 &middot; data via yfinance &middot; TWR basket vs SPY &middot; click any row for the full chart</footer>
{build_health_html}

</div>

<!-- Modal -->
<div class="modal" id="modal" hidden role="dialog" aria-modal="true">
  <div class="modal-card" role="document">
    <button class="modal-close" aria-label="Close">&times;</button>
    <div class="modal-head">
      <div>
        <h2 class="modal-ticker"></h2>
        <div class="modal-name"></div>
        <div class="modal-industry"></div>
      </div>
      <div class="modal-headline">
        <div class="modal-pct"></div>
        <div class="modal-since"></div>
      </div>
    </div>
    <div class="modal-signal" id="modal-signal">
      <span class="modal-signal-label">Signal</span>
      <span class="modal-signal-text"></span>
      <span class="modal-signal-detail"></span>
    </div>
    <div class="modal-fx" id="modal-fx" hidden>
      <span class="modal-fx-label">FX attribution</span>
      <div class="modal-fx-row">
        <span class="modal-fx-part"><em>Stock</em> <b id="fx-stock"></b></span>
        <span class="modal-fx-sep">·</span>
        <span class="modal-fx-part"><em>FX</em> <b id="fx-fx"></b></span>
        <span class="modal-fx-sep">·</span>
        <span class="modal-fx-part"><em>Total</em> <b id="fx-total"></b></span>
      </div>
    </div>
    <div class="modal-stats">
      <div class="modal-stat"><div class="modal-stat-label">Baseline</div><div class="modal-stat-val" data-key="baseline"></div></div>
      <div class="modal-stat"><div class="modal-stat-label">Latest</div><div class="modal-stat-val" data-key="latest"></div></div>
      <div class="modal-stat"><div class="modal-stat-label">1W</div><div class="modal-stat-val" data-key="w1"></div></div>
      <div class="modal-stat"><div class="modal-stat-label">1M</div><div class="modal-stat-val" data-key="m1"></div></div>
      <div class="modal-stat"><div class="modal-stat-label">3M</div><div class="modal-stat-val" data-key="m3"></div></div>
      <div class="modal-stat"><div class="modal-stat-label">YTD</div><div class="modal-stat-val" data-key="ytd"></div></div>
    </div>
    <div class="modal-quant-head">Quant signals</div>
    <div class="modal-quant-stats">
      <div class="modal-stat"><div class="modal-stat-label">vs 200d</div>
        <div class="modal-stat-val" data-qkey="sma200_dist_pct"></div>
        <div class="modal-stat-meta" data-qmeta="sma200_dist_pct"></div></div>
      <div class="modal-stat"><div class="modal-stat-label">ATR 14d</div>
        <div class="modal-stat-val" data-qkey="atr14_gbp"></div>
        <div class="modal-stat-meta" data-qmeta="atr14_gbp"></div></div>
      <div class="modal-stat"><div class="modal-stat-label">RSI 14d</div>
        <div class="modal-stat-val" data-qkey="rsi14"></div>
        <div class="modal-stat-meta" data-qmeta="rsi14"></div></div>
      <div class="modal-stat"><div class="modal-stat-label">52w pos</div>
        <div class="modal-stat-val" data-qkey="range52w_pct"></div>
        <div class="modal-stat-meta" data-qmeta="range52w_pct"></div></div>
      <div class="modal-stat"><div class="modal-stat-label">Volume</div>
        <div class="modal-stat-val" data-qkey="vol_ratio"></div>
        <div class="modal-stat-meta" data-qmeta="vol_ratio"></div></div>
    </div>
    <div class="modal-news" id="modal-news" hidden>
      <div class="modal-news-head">Recent news <span class="modal-news-staleness"></span></div>
      <div class="modal-news-list"></div>
    </div>
    <div class="modal-chart-wrap">
      <svg class="modal-chart" viewBox="0 0 {MODAL_VB_W} {MODAL_VB_H}" preserveAspectRatio="none"></svg>
      <div class="modal-chart-labels"></div>
      <div class="modal-tip" hidden></div>
      <div class="modal-loading" hidden>
        <div class="modal-spinner"></div>
        <div class="modal-loading-text">Loading chart…</div>
      </div>
    </div>
  </div>
</div>

<!-- T11/T12/T14/T15: shared "info" modal for industry / sector / pair-list /
     weekly-movers drill-downs. Lower z-index than the ticker modal so that
     ticker modals open ON TOP of the info modal (stacked modal behavior --
     closing the ticker modal returns the user to the still-open info modal). -->
<div class="modal info-modal" id="info-modal" hidden role="dialog" aria-modal="true">
  <div class="modal-card info-modal-card" role="document">
    <button class="modal-close" id="info-modal-close" aria-label="Close">&times;</button>
    <div class="info-modal-head">
      <h2 class="info-modal-title"></h2>
      <div class="info-modal-sub muted"></div>
    </div>
    <div class="info-modal-body"></div>
  </div>
</div>

<!-- v2.1 Quiz modal. Opens via the topbar Quiz button; closed by ESC, the X
     button, or clicking the backdrop. Renders one question at a time from
     QUIZ_POOL with 3 answer buttons; on answer reveal, the correct option
     flashes green and the explanation appears below. "Next" cycles to a
     fresh question. Score + seen-set persist via localStorage. -->
<div class="modal quiz-modal" id="quiz-modal" hidden role="dialog" aria-modal="true">
  <div class="modal-card quiz-modal-card" role="document">
    <button class="modal-close" id="quiz-modal-close" aria-label="Close">&times;</button>
    <div class="quiz-head">
      <div class="quiz-eyebrow">Quick quiz</div>
      <div class="quiz-cat-pill" id="quiz-cat-pill"></div>
    </div>
    <div class="quiz-question" id="quiz-question">&mdash;</div>
    <div class="quiz-options" id="quiz-options" role="radiogroup" aria-label="Answer choices">
      <!-- JS injects 3 <button class="quiz-option"> here -->
    </div>
    <div class="quiz-reveal" id="quiz-reveal" hidden>
      <div class="quiz-reveal-verdict" id="quiz-reveal-verdict"></div>
      <div class="quiz-reveal-text" id="quiz-reveal-text"></div>
    </div>
    <div class="quiz-foot">
      <div class="quiz-score">
        This month: <span class="quiz-score-num" id="quiz-score-num">0/0</span>
      </div>
      <div class="quiz-actions">
        <button type="button" class="quiz-next" id="quiz-next" disabled
                title="Show another question">Next &rarr;</button>
      </div>
    </div>
  </div>
</div>

{sortable_script_tag}
<script>
const DATA = {data_json};
// v2.0 lazy-modal: when non-null, points at docs/data/payload.json with modal-
// only fields for every ticker (~1.5 MB). Fetched on requestIdleCallback after
// first paint; merged into DATA[tkr] on first modal-open. For demo.html this
// is null and DATA already contains every field (single-file build).
const HEAVY_URL = {heavy_url_js};

// v2.0 lazy-modal: deferred load of per-ticker modal payload. When HEAVY_URL
// is non-null (real-data build), fetch once on requestIdleCallback after first
// paint and memoise the Promise; merge into DATA[tkr] on first openModal() so
// the existing modal render code keeps reading fields off `d` unchanged.
// Demo.html sets HEAVY_URL = null and DATA already contains every field.
let _heavyPromise = null;
function loadHeavy() {{
  if (_heavyPromise) return _heavyPromise;
  if (!HEAVY_URL) {{ _heavyPromise = Promise.resolve({{}}); return _heavyPromise; }}
  _heavyPromise = fetch(HEAVY_URL)
    .then(r => r.ok ? r.json() : Promise.reject(new Error('HTTP ' + r.status)))
    .catch(err => {{
      console.warn('[lazy-modal] payload fetch failed:', err);
      _heavyPromise = null;   // allow retry on next modal-open
      return null;
    }});
  return _heavyPromise;
}}
// Prefetch on idle so the common path -- user clicks a ticker within 5-30s of
// first paint -- finds the payload already cached. requestIdleCallback waits
// until the main thread AND network are idle, then the {{timeout:4000}}
// guarantees the fetch runs even on a hyper-busy page.
if (HEAVY_URL) {{
  if (window.requestIdleCallback) {{
    window.requestIdleCallback(() => loadHeavy(), {{timeout: 4000}});
  }} else {{
    setTimeout(() => loadHeavy(), 2000);
  }}
}}

const PORTFOLIO = {portfolio_json};
// T11/T12/T14/T15: pre-shaped drill-down data for click-to-expand modals
// (industries, sectors, correlation pairs, weekly movers).
const AUX_DATA = {aux_json};

// v1.9 Pocket Lesson: array of {{title, body}} tips, baked at build time from
// the POCKET_LESSONS list in build.py. JS picks one at random on each page
// load; a Next-tip button rotates without reloading.
const POCKET_LESSONS = {pocket_lessons_json};

// v2.1 Quiz: 50-question pool, 5 categories x 10 questions. Schema:
//   {{id, category, format ("cloze"|"direct"), question, options[3], correct, explanation}}
// State (seen set, monthly score) persisted via two localStorage keys:
//   "quizSeen"    - JSON array of seen question ids
//   "quizMonthly" - JSON {{month, answered, correct}} resetting on calendar month
const QUIZ_POOL = {quiz_pool_json};
const LAST_LOOK = {last_look_json};

// ---- Helpers
function fmtMoney(v, sym) {{
  sym = sym || '{BASE_SYMBOL}';
  return sym + (v >= 1000 ? v.toLocaleString('en-GB', {{maximumFractionDigits: 2}}) : v.toFixed(2));
}}
function fmtPct(v, sign) {{
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  const s = (sign && v >= 0) ? '+' : '';
  return s + v.toFixed(2) + '%';
}}
function fmtDate(iso) {{
  const [y, m, d] = iso.split('-');
  const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  return parseInt(d) + ' ' + months[parseInt(m)-1] + ' ' + y.slice(2);
}}
// Compact form for the x-axis ticks (no day-of-month): "Oct '24". The axis spans
// months, so the day is noise and crowds/overlaps on narrow (mobile) charts.
function fmtAxisDate(iso) {{
  const [y, m] = iso.split('-');
  const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  return months[parseInt(m)-1] + " '" + y.slice(2);
}}

// ---- Hero chart (basket + SPY)
let showNasdaq = (function() {{ try {{ return localStorage.getItem('heroNasdaq') === '1'; }} catch (e) {{ return false; }} }})();
function renderHeroChart() {{
  const svg = document.getElementById('hero-chart');
  const wrap = svg.parentElement;
  const tip = document.getElementById('hero-tip');
  const W = Math.max(wrap.clientWidth, 320);
  const H = Math.max(wrap.clientHeight, 200);
  svg.setAttribute('viewBox', `0 0 ${{W}} ${{H}}`);
  svg.setAttribute('width', W);
  svg.setAttribute('height', H);

  const b = PORTFOLIO.basket;
  const s = PORTFOLIO.spy;
  const fx = PORTFOLIO.fx || {{dates:[], values:[]}};
  const nzRaw = PORTFOLIO.nasdaq || {{dates:[], values:[]}};
  const nz = showNasdaq ? nzRaw : {{dates:[], values:[]}};
  if (!b.values.length) {{ svg.innerHTML = '<text x="50%" y="50%" fill="#6b7185" font-family="Geist Mono" font-size="12" text-anchor="middle">No basket data</text>'; return; }}

  // Combined min/max across both series
  const allVals = [...b.values, ...s.values, ...nz.values, 0];
  const vmin = Math.min(...allVals);
  const vmax = Math.max(...allVals);
  const span = (vmax - vmin) || 1;
  const padL = 48, padR = 56, padT = 18, padB = 32;
  // FX band sits between the line chart and x-axis labels. Carved out of
  // the inner height so the line chart shrinks slightly instead of overlapping.
  const FX_H = fx.values.length ? 36 : 0;
  const FX_GAP = fx.values.length ? 10 : 0;
  const innerW = W - padL - padR;
  const innerH = H - padT - padB - FX_H - FX_GAP;
  const fxTop = padT + innerH + FX_GAP;
  const fxBaseY = fxTop + FX_H / 2;       // baseline = midline of FX band

  function buildPoints(series) {{
    if (!series.values.length) return {{xs:[], ys:[], dates:[], vals:[]}};
    const n = series.values.length;
    const xs = series.values.map((_, i) => padL + (n === 1 ? innerW/2 : (i/(n-1)) * innerW));
    const ys = series.values.map(v => padT + (1 - (v - vmin)/span) * innerH);
    return {{xs, ys, dates: series.dates, vals: series.values}};
  }}
  const basket = buildPoints(b);
  const spy = buildPoints(s);
  const nasdaq = buildPoints(nz);

  // Y ticks (5 levels)
  const yTicks = [];
  for (let i = 0; i <= 5; i++) {{
    const v = vmin + (i/5) * span;
    const y = padT + (1 - (v - vmin)/span) * innerH;
    yTicks.push({{v, y}});
  }}
  // X ticks (~5; fewer on narrow/mobile charts so the date labels don't collide)
  const n = basket.xs.length;
  const xTickCount = Math.min(W < 460 ? 4 : 5, n);
  const xTicks = [];
  for (let i = 0; i < xTickCount; i++) {{
    const idx = Math.round((i/(xTickCount-1)) * (n-1));
    xTicks.push({{idx, x: basket.xs[idx], date: basket.dates[idx]}});
  }}

  // Align SPY's x grid to basket's so dates line up exactly. buildPoints
  // spreads each series across innerW based on its OWN length, so a 84-point
  // SPY ended ~1% offset from an 85-point basket even though their dates
  // matched 1:1. Re-mapping SPY's xs to basket's positions at matching dates
  // fixes that — both the rendered SPY polyline and the vs-SPY area edge now
  // sit on a single consistent x grid.
  // v1.8 T2: this remap must happen BEFORE building spyPL below, otherwise the
  // SPY polyline string captures the original xs while the vs-SPY polygons
  // (built later) use the remapped xs, producing a visible gap between them.
  if (spy.dates && spy.dates.length && spy.dates.length <= basket.dates.length) {{
    const remapped = spy.dates.map(d => {{
      const idx = basket.dates.indexOf(d);
      return idx >= 0 ? basket.xs[idx] : NaN;
    }});
    if (remapped.every(v => !Number.isNaN(v))) spy.xs = remapped;
  }}
  if (nasdaq.dates && nasdaq.dates.length && nasdaq.dates.length <= basket.dates.length) {{
    const remappedN = nasdaq.dates.map(d => {{
      const idx = basket.dates.indexOf(d);
      return idx >= 0 ? basket.xs[idx] : NaN;
    }});
    if (remappedN.every(v => !Number.isNaN(v))) nasdaq.xs = remappedN;
  }}

  const basketPL = basket.xs.map((x, i) => `${{x.toFixed(1)}},${{basket.ys[i].toFixed(1)}}`).join(' ');
  const spyPL = spy.xs.map((x, i) => `${{x.toFixed(1)}},${{spy.ys[i].toFixed(1)}}`).join(' ');
  const nasdaqPL = nasdaq.xs.map((x, i) => `${{x.toFixed(1)}},${{nasdaq.ys[i].toFixed(1)}}`).join(' ');
  const nasdaqColor = '#a78bfa';
  const nasdaqEnd = nasdaq.vals.length ? nasdaq.vals[nasdaq.vals.length - 1] : 0;
  const zeroY = padT + (1 - (0 - vmin)/span) * innerH;

  const basketEnd = basket.vals[basket.vals.length - 1];
  const spyEnd = spy.vals.length ? spy.vals[spy.vals.length - 1] : 0;
  const basketColor = '#f59e0b';
  const spyColor = '#6b7185';
  const greenFill = 'rgba(52,211,153,0.22)';   // outperforming SPY
  const redFill = 'rgba(248,113,113,0.22)';    // underperforming SPY
  const lossWashFill = 'rgba(248,113,113,0.09)'; // subtle below-zero wash

  // Vs-SPY area segments: between basket and SPY lines, painted green when
  // basket is above SPY and red when below. Crossovers are split exactly via
  // linear interpolation on the difference so the segment edges land on the
  // true crossing point, not the nearest weekly tick.

  // Vs-SPY area segments: between basket and SPY lines, painted green when
  // basket > SPY and red when below. Iterate over the overlapping date range
  // (SPY can start a week later than basket since the benchmark series begins
  // from first-trading-day-after-first-purchase).
  // v1.8 T2 fix: pair basket and SPY by DATE (not by index). Previously the
  // loop paired basket.xs[startInBasket+k] with spy.ys[k], which mis-aligned
  // when basket had middle weeks SPY didn't. With the remap moved above
  // spyPL, spy.xs and basket.xs share one grid for matching dates, so the
  // polygon bottom edge follows the SPY polyline exactly.
  const vsSpySegments = [];
  if (spy.dates && spy.dates.length >= 2) {{
    const basketIdxAt = spy.dates.map(d => basket.dates.indexOf(d));
    for (let k = 0; k < spy.dates.length - 1; k++) {{
      const bi = basketIdxAt[k], bi2 = basketIdxAt[k + 1];
      if (bi < 0 || bi2 < 0) continue;
      const x1 = basket.xs[bi], x2 = basket.xs[bi2];
      const by1 = basket.ys[bi], by2 = basket.ys[bi2];
      const sy1 = spy.ys[k], sy2 = spy.ys[k + 1];
      const d1 = basket.vals[bi] - spy.vals[k];
      const d2 = basket.vals[bi2] - spy.vals[k + 1];
      if (d1 === 0 && d2 === 0) continue;
      if ((d1 >= 0) === (d2 >= 0)) {{
        const color = (d1 + d2) >= 0 ? greenFill : redFill;
        vsSpySegments.push({{
          pts: [[x1, by1], [x2, by2], [x2, sy2], [x1, sy1]],
          color,
        }});
      }} else {{
        const t = d1 / (d1 - d2);
        const crossX = x1 + t * (x2 - x1);
        const crossY = by1 + t * (by2 - by1);
        vsSpySegments.push({{
          pts: [[x1, by1], [crossX, crossY], [x1, sy1]],
          color: d1 >= 0 ? greenFill : redFill,
        }});
        vsSpySegments.push({{
          pts: [[crossX, crossY], [x2, by2], [x2, sy2]],
          color: d2 >= 0 ? greenFill : redFill,
        }});
      }}
    }}
    // v1.8.1 B6: basket can extend past SPY's last date (weekend builds where
    // European tickers traded after SPY's Friday close, or holidays). Without
    // a forward-fill the shade has a visible gap at the right edge. Extend a
    // final segment from SPY's last paired basket-index up to basket's end,
    // holding SPY's last value flat. The dashed SPY polyline itself stays
    // visually honest (stops at its real last data point) -- only the
    // comparison shading is forward-filled.
    const lastSpyK = spy.dates.length - 1;
    const lastSpyBi = basketIdxAt[lastSpyK];
    if (lastSpyBi >= 0 && lastSpyBi < basket.xs.length - 1) {{
      const sxL = basket.xs[lastSpyBi];
      const syL = spy.ys[lastSpyK];
      const spyTailVal = spy.vals[lastSpyK];
      for (let bi = lastSpyBi; bi < basket.xs.length - 1; bi++) {{
        const x1 = basket.xs[bi], x2 = basket.xs[bi + 1];
        const by1 = basket.ys[bi], by2 = basket.ys[bi + 1];
        const d1 = basket.vals[bi] - spyTailVal;
        const d2 = basket.vals[bi + 1] - spyTailVal;
        if (d1 === 0 && d2 === 0) continue;
        if ((d1 >= 0) === (d2 >= 0)) {{
          const color = (d1 + d2) >= 0 ? greenFill : redFill;
          vsSpySegments.push({{
            pts: [[x1, by1], [x2, by2], [x2, syL], [x1, syL]],
            color,
          }});
        }} else {{
          const t = d1 / (d1 - d2);
          const crossX = x1 + t * (x2 - x1);
          const crossY = by1 + t * (by2 - by1);
          vsSpySegments.push({{
            pts: [[x1, by1], [crossX, crossY], [x1, syL]],
            color: d1 >= 0 ? greenFill : redFill,
          }});
          vsSpySegments.push({{
            pts: [[crossX, crossY], [x2, by2], [x2, syL]],
            color: d2 >= 0 ? greenFill : redFill,
          }});
        }}
      }}
    }}
  }}

  // v2.7 #2: UK fiscal-year-start markers (~6 Apr each year). Map a calendar
  // date to x by interpolating between the basket's index-placed weekly points
  // so the marker lands exactly on the chart's own x grid (not a parallel one).
  function xForDate(targetMs) {{
    const ds = basket.dates;
    if (!ds.length) return null;
    const d0 = Date.parse(ds[0] + 'T00:00:00');
    const dN = Date.parse(ds[ds.length - 1] + 'T00:00:00');
    if (targetMs < d0 || targetMs > dN) return null;   // outside range -> skip
    for (let i = 1; i < ds.length; i++) {{
      const di = Date.parse(ds[i] + 'T00:00:00');
      if (targetMs <= di) {{
        const dprev = Date.parse(ds[i - 1] + 'T00:00:00');
        const f = (targetMs - dprev) / Math.max(di - dprev, 1);
        return basket.xs[i - 1] + f * (basket.xs[i] - basket.xs[i - 1]);
      }}
    }}
    return basket.xs[basket.xs.length - 1];
  }}
  let fyHtml = '';
  if (basket.dates.length) {{
    const yr0 = new Date(basket.dates[0] + 'T00:00:00').getFullYear();
    const yr1 = new Date(basket.dates[basket.dates.length - 1] + 'T00:00:00').getFullYear();
    for (let yr = yr0; yr <= yr1; yr++) {{
      const fyMs = Date.parse(yr + '-04-06T00:00:00');   // UK tax year starts 6 Apr
      const fxx = xForDate(fyMs);
      if (fxx == null) continue;
      const lbl = `FY${{String(yr % 100).padStart(2, '0')}}/${{String((yr + 1) % 100).padStart(2, '0')}}`;
      fyHtml += `<line x1="${{fxx.toFixed(1)}}" y1="${{padT}}" x2="${{fxx.toFixed(1)}}" y2="${{(padT + innerH).toFixed(1)}}" stroke="#6b7185" stroke-width="0.8" stroke-dasharray="2 4" opacity="0.45"/>`;
      fyHtml += `<text x="${{(fxx + 3).toFixed(1)}}" y="${{(padT + 9).toFixed(1)}}" fill="#6b7185" font-size="8.5" font-family="Geist Mono, monospace" opacity="0.85">${{lbl}}</text>`;
    }}
  }}
  // v2.7: last-COMPLETED UK fiscal year return (6 Apr -> 6 Apr), shown top-left.
  let fyStatHtml = '';
  if (basket.dates.length) {{
    const valForDate = (targetMs) => {{
      const ds = basket.dates;
      const d0 = Date.parse(ds[0] + 'T00:00:00');
      const dN = Date.parse(ds[ds.length - 1] + 'T00:00:00');
      if (targetMs < d0 || targetMs > dN) return null;
      for (let i = 1; i < ds.length; i++) {{
        const di = Date.parse(ds[i] + 'T00:00:00');
        if (targetMs <= di) {{
          const dp = Date.parse(ds[i - 1] + 'T00:00:00');
          const f = (targetMs - dp) / Math.max(di - dp, 1);
          return basket.vals[i - 1] + f * (basket.vals[i] - basket.vals[i - 1]);
        }}
      }}
      return basket.vals[basket.vals.length - 1];
    }};
    const today = new Date();
    const fyEndYr = (today >= new Date(today.getFullYear(), 3, 6))
                    ? today.getFullYear() : today.getFullYear() - 1;
    const vS = valForDate(Date.parse((fyEndYr - 1) + '-04-06T00:00:00'));
    const vE = valForDate(Date.parse(fyEndYr + '-04-06T00:00:00'));
    if (vS !== null && vE !== null) {{
      const fyRet = ((1 + vE / 100) / (1 + vS / 100) - 1) * 100;
      const col = fyRet >= 0 ? '#34d399' : '#f87171';
      const lbl = `FY${{String((fyEndYr - 1) % 100).padStart(2, '0')}}/${{String(fyEndYr % 100).padStart(2, '0')}}`;
      fyStatHtml = `<text x="${{(padL + 5).toFixed(1)}}" y="${{(padT + 11).toFixed(1)}}" font-family="Geist Mono, monospace" font-size="11" font-weight="600"><tspan fill="#6b7185">${{lbl}} </tspan><tspan fill="${{col}}">${{fyRet >= 0 ? '+' : ''}}${{fyRet.toFixed(1)}}%</tspan><title>Basket return over the last completed UK fiscal year (6 Apr ${{fyEndYr - 1}} to 6 Apr ${{fyEndYr}})</title></text>`;
    }}
  }}

  let html = '';
  // Y grid (with axis labels)
  html += yTicks.map(t =>
    `<line x1="${{padL}}" y1="${{t.y.toFixed(1)}}" x2="${{padL + innerW}}" y2="${{t.y.toFixed(1)}}" stroke="rgba(255,255,255,0.04)" stroke-width="1"/>` +
    `<text x="${{padL - 8}}" y="${{(t.y + 3.5).toFixed(1)}}" fill="#6b7185" font-size="10" font-family="Geist Mono, monospace" text-anchor="end">${{t.v >= 0 ? '+' : ''}}${{t.v.toFixed(0)}}%</text>`
  ).join('');
  // Loss-zone wash: subtle red rectangle from the zero line down to the chart
  // floor so "below baseline" reads at a glance, even before reading any number.
  const chartBottom = padT + innerH;
  if (zeroY < chartBottom) {{
    html += `<rect x="${{padL}}" y="${{zeroY.toFixed(1)}}" width="${{innerW.toFixed(1)}}" height="${{(chartBottom - zeroY).toFixed(1)}}" fill="${{lossWashFill}}"/>`;
  }}
  // Vs-SPY area segments (paint *before* the zero line + lines so they stay crisp).
  html += vsSpySegments.map(seg =>
    `<polygon points="${{seg.pts.map(p => p[0].toFixed(1) + ',' + p[1].toFixed(1)).join(' ')}}" fill="${{seg.color}}"/>`
  ).join('');
  // Zero line — bumped from 0.18 to 0.34 alpha + 1.2 stroke so it's clearly
  // the "Oct '24 baseline" reference, not just another gridline.
  html += `<line x1="${{padL}}" y1="${{zeroY.toFixed(1)}}" x2="${{padL + innerW}}" y2="${{zeroY.toFixed(1)}}" stroke="rgba(255,255,255,0.34)" stroke-width="1.2" stroke-dasharray="4 3"/>`;
  // X labels — positioned below the FX band (or below the line chart when no FX)
  const xLabelY = padT + innerH + FX_GAP + FX_H + 16;
  html += xTicks.map(t =>
    `<text x="${{t.x.toFixed(1)}}" y="${{xLabelY.toFixed(1)}}" fill="#6b7185" font-size="10" font-family="Geist Mono, monospace" text-anchor="middle">${{fmtAxisDate(t.date)}}</text>`
  ).join('');
  // v2.7 #2: UK FY-start vertical markers (drawn under the data lines).
  html += fyHtml;
  // SPY line (dashed)
  if (spy.xs.length) {{
    html += `<polyline points="${{spyPL}}" fill="none" stroke="${{spyColor}}" stroke-width="1.4" stroke-dasharray="4 3" stroke-linejoin="round"/>`;
  }}
  // v3.0 #3: Nasdaq (QQQ) overlay — dotted, drawn under the basket line so the
  // basket stays visually dominant. Only present when the legend toggle is on.
  if (nasdaq.xs.length) {{
    html += `<polyline points="${{nasdaqPL}}" fill="none" stroke="${{nasdaqColor}}" stroke-width="1.4" stroke-dasharray="1 3" stroke-linejoin="round"/>`;
  }}
  // Basket line
  html += `<polyline points="${{basketPL}}" fill="none" stroke="${{basketColor}}" stroke-width="2.2" stroke-linejoin="round" stroke-linecap="round"/>`;
  // v2.7: last-completed fiscal-year return (top-left, on top of the line).
  html += fyStatHtml;

  // End labels (basket + SPY) + vs-SPY delta badge
  html += `<text x="${{(padL + innerW + 6).toFixed(1)}}" y="${{(basket.ys[n-1] + 4).toFixed(1)}}" fill="${{basketColor}}" font-size="11" font-family="Geist Mono, monospace" font-weight="500">${{basketEnd >= 0 ? '+' : ''}}${{basketEnd.toFixed(1)}}%</text>`;
  if (spy.ys.length) {{
    html += `<text x="${{(padL + innerW + 6).toFixed(1)}}" y="${{(spy.ys[spy.ys.length-1] + 4).toFixed(1)}}" fill="${{spyColor}}" font-size="11" font-family="Geist Mono, monospace">${{spyEnd >= 0 ? '+' : ''}}${{spyEnd.toFixed(1)}}%</text>`;
    // Vs-SPY delta in percentage points, positioned BELOW both end labels
    // (use the max y = the lower of the two lines, plus 14px offset).
    const vsDelta = basketEnd - spyEnd;
    const vsColor = vsDelta >= 0 ? '#34d399' : '#f87171';
    const vsY = Math.max(basket.ys[n-1], spy.ys[spy.ys.length-1]) + 16;
    // Δ is the "delta" — implies "basket minus SPY" without spelling it out.
    // Keeps the badge within the chart's right padding (~50px).
    html += `<text x="${{(padL + innerW + 6).toFixed(1)}}" y="${{vsY.toFixed(1)}}" fill="${{vsColor}}" font-size="10.5" font-family="Geist Mono, monospace" font-weight="600">&#916; ${{vsDelta >= 0 ? '+' : ''}}${{vsDelta.toFixed(1)}}pp</text>`;
  }}
  if (nasdaq.ys.length) {{
    html += `<text x="${{(padL + innerW + 6).toFixed(1)}}" y="${{(nasdaq.ys[nasdaq.ys.length-1] + 4).toFixed(1)}}" fill="${{nasdaqColor}}" font-size="11" font-family="Geist Mono, monospace">${{nasdaqEnd >= 0 ? '+' : ''}}${{nasdaqEnd.toFixed(1)}}%</text>`;
  }}

  // FX bar band — weekly GBP/USD rate, centered on the baseline (first value).
  // Up bar = stronger GBP, down bar = weaker GBP. Color follows --up / --down.
  // Baseline + delta arrays declared at outer scope so the hover handler below
  // can resolve the FX value at the hovered week index.
  const fxBaseline = fx.values.length ? (fx.values[0] || 1) : 1;
  const fxDeltas = fx.values.map(v => (v / fxBaseline - 1) * 100);
  if (fx.values.length) {{
    const fxMaxAbs = Math.max(0.5, ...fxDeltas.map(Math.abs));         // floor to avoid tiny bars
    const fxMin = Math.min(...fx.values);
    const fxMax = Math.max(...fx.values);
    const fxN = fx.values.length;
    const slotW = innerW / fxN;
    const barW = Math.max(2, slotW * 0.55);
    const fxXs = fx.values.map((_, i) => padL + ((fxN === 1 ? innerW/2 : (i/(fxN-1)) * innerW)));
    const fxBars = fxDeltas.map((d, i) => {{
      const half = (Math.abs(d) / fxMaxAbs) * (FX_H / 2 - 1);
      const y = d >= 0 ? fxBaseY - half : fxBaseY;
      const h = half;
      const fill = d >= 0 ? '#34d399' : '#f87171';
      return `<rect x="${{(fxXs[i] - barW/2).toFixed(1)}}" y="${{y.toFixed(1)}}" width="${{barW.toFixed(1)}}" height="${{h.toFixed(1)}}" fill="${{fill}}" opacity="0.55" data-i="${{i}}"/>`;
    }}).join('');
    // Baseline midline + label (shows the reference value the bars deviate from)
    html += `<line x1="${{padL}}" y1="${{fxBaseY.toFixed(1)}}" x2="${{padL + innerW}}" y2="${{fxBaseY.toFixed(1)}}" stroke="rgba(255,255,255,0.10)" stroke-width="0.7"/>`;
    html += fxBars;
    // Left axis: label + the actual baseline rate in $/£ so bar magnitudes are interpretable
    html += `<text x="${{padL - 8}}" y="${{(fxBaseY - 1).toFixed(1)}}" fill="#6b7185" font-size="9" font-family="Geist Mono, monospace" text-anchor="end">GBP/$</text>`;
    html += `<text x="${{padL - 8}}" y="${{(fxBaseY + 9).toFixed(1)}}" fill="#6b7185" font-size="8.5" font-family="Geist Mono, monospace" text-anchor="end">ref $${{fxBaseline.toFixed(3)}}</text>`;
    // Range labels at top/bottom of the FX strip — show $min and $max
    html += `<text x="${{(padL + innerW + 6).toFixed(1)}}" y="${{(fxTop + 4).toFixed(1)}}" fill="#34d399" font-size="8.5" font-family="Geist Mono, monospace">$${{fxMax.toFixed(3)}}</text>`;
    html += `<text x="${{(padL + innerW + 6).toFixed(1)}}" y="${{(fxTop + FX_H - 1).toFixed(1)}}" fill="#f87171" font-size="8.5" font-family="Geist Mono, monospace">$${{fxMin.toFixed(3)}}</text>`;
    // Current value (slightly larger) at end of baseline
    const fxEnd = fx.values[fx.values.length - 1];
    const fxEndDelta = fxDeltas[fxDeltas.length - 1];
    const fxColor = fxEndDelta >= 0 ? '#34d399' : '#f87171';
    html += `<text x="${{(padL + innerW + 6).toFixed(1)}}" y="${{(fxBaseY + 3).toFixed(1)}}" fill="${{fxColor}}" font-size="10" font-family="Geist Mono, monospace" font-weight="500">$${{fxEnd.toFixed(3)}}</text>`;
  }}

  // Crosshair + hover dots. pointer-events="none" so they don't intercept
  // clicks on the T15 week-click rects layered above them in DOM order.
  html += `<line class="hero-cross" x1="0" y1="${{padT}}" x2="0" y2="${{padT + innerH}}" stroke="${{basketColor}}" stroke-width="0.8" stroke-dasharray="2 3" opacity="0" pointer-events="none"/>`;
  html += `<circle class="hero-dot-basket" cx="0" cy="0" r="4" fill="${{basketColor}}" opacity="0" pointer-events="none"/>`;
  html += `<circle class="hero-dot-spy" cx="0" cy="0" r="3.5" fill="${{spyColor}}" opacity="0" pointer-events="none"/>`;

  // T15: per-week transparent click rects. One <rect> per weekly point spans
  // half the gap before + after that point so clicks land naturally near the
  // visible value. data-week-end carries the week-ending date used to look
  // up AUX_DATA.weekly_movers in the click handler. pointer-events="all" is
  // explicit because transparent-fill SVG rects can be hit-test-skipped by
  // some browsers in edge cases.
  if (basket.xs.length > 1) {{
    const halfStep = (basket.xs[1] - basket.xs[0]) / 2;
    for (let i = 0; i < basket.xs.length; i++) {{
      const cx = basket.xs[i];
      const left  = Math.max(padL, cx - halfStep);
      const right = Math.min(padL + innerW, cx + halfStep);
      const w = right - left;
      if (w <= 0) continue;
      html += `<rect class="hero-week-click" data-week-end="${{basket.dates[i]}}" `
            + `x="${{left.toFixed(1)}}" y="${{padT}}" width="${{w.toFixed(1)}}" `
            + `height="${{innerH.toFixed(1)}}" fill="transparent" pointer-events="all" />`;
    }}
  }}

  svg.innerHTML = html;

  // Hover handler
  svg.onmousemove = (e) => {{
    const rect = svg.getBoundingClientRect();
    const xpx = (e.clientX - rect.left) * (W / rect.width);
    // Find nearest basket index
    let bestI = 0, bestDist = Infinity;
    for (let i = 0; i < basket.xs.length; i++) {{
      const d = Math.abs(basket.xs[i] - xpx);
      if (d < bestDist) {{ bestDist = d; bestI = i; }}
    }}
    const bx = basket.xs[bestI], by = basket.ys[bestI];
    const bv = basket.vals[bestI];
    // Find SPY value at same date (by index — they're both weekly, aligned closely)
    let sv = null, sy = 0;
    if (spy.dates.length) {{
      const target = basket.dates[bestI];
      let sIdx = spy.dates.indexOf(target);
      if (sIdx < 0) {{
        // Approximate by index ratio
        sIdx = Math.min(Math.round(bestI * (spy.dates.length-1) / Math.max(basket.dates.length-1, 1)), spy.dates.length - 1);
      }}
      sv = spy.vals[sIdx];
      sy = spy.ys[sIdx];
    }}
    const cross = svg.querySelector('.hero-cross');
    const dotB = svg.querySelector('.hero-dot-basket');
    const dotS = svg.querySelector('.hero-dot-spy');
    cross.setAttribute('x1', bx); cross.setAttribute('x2', bx); cross.setAttribute('opacity', '0.7');
    dotB.setAttribute('cx', bx); dotB.setAttribute('cy', by); dotB.setAttribute('opacity', '1');
    if (sv !== null) {{
      dotS.setAttribute('cx', bx); dotS.setAttribute('cy', sy); dotS.setAttribute('opacity', '1');
    }}
    const tipX = (bx / W) * rect.width;
    const tipY = (by / H) * rect.height;
    tip.style.left = tipX + 'px';
    tip.style.top = tipY + 'px';
    // Match FX value at the same week index when possible. FX series may be
    // shorter (no fx data for very first weeks); fall back to nearest index.
    let fxAtVal = null, fxAtDelta = null;
    if (fx.values.length) {{
      const fxTarget = basket.dates[bestI];
      let fxIdx = fx.dates.indexOf(fxTarget);
      if (fxIdx < 0) fxIdx = Math.min(bestI, fx.values.length - 1);
      fxAtVal = fx.values[fxIdx];
      fxAtDelta = fxDeltas[fxIdx];
    }}
    tip.innerHTML =
      `<div class="tip-date">${{fmtDate(basket.dates[bestI])}}</div>` +
      `<div class="tip-row"><span class="tip-label">Basket</span><span class="${{bv >= 0 ? 'pos' : 'neg'}}">${{bv >= 0 ? '+' : ''}}${{bv.toFixed(2)}}%</span></div>` +
      (sv !== null ? `<div class="tip-row"><span class="tip-label">SPY</span><span class="${{sv >= 0 ? 'pos' : 'neg'}}">${{sv >= 0 ? '+' : ''}}${{sv.toFixed(2)}}%</span></div>` : '') +
      (fxAtVal !== null ? `<div class="tip-row"><span class="tip-label">GBP/USD</span><span class="${{fxAtDelta >= 0 ? 'pos' : 'neg'}}">$${{fxAtVal.toFixed(3)}} (${{fxAtDelta >= 0 ? '+' : ''}}${{fxAtDelta.toFixed(2)}}%)</span></div>` : '');
    tip.removeAttribute('hidden');
  }};
  svg.onmouseleave = () => {{
    tip.setAttribute('hidden', '');
    const c = svg.querySelector('.hero-cross');
    if (c) c.setAttribute('opacity', '0');
    svg.querySelectorAll('circle').forEach(d => d.setAttribute('opacity', '0'));
  }};
}}

renderHeroChart();
window.addEventListener('resize', renderHeroChart);
// v3.0 #3: Nasdaq overlay legend toggle (default off, persisted in localStorage).
(function() {{
  const btn = document.querySelector('.hero-legend .leg-toggle[data-series="nasdaq"]');
  if (!btn) return;
  btn.setAttribute('aria-pressed', showNasdaq ? 'true' : 'false');
  btn.addEventListener('click', () => {{
    showNasdaq = !showNasdaq;
    try {{ localStorage.setItem('heroNasdaq', showNasdaq ? '1' : '0'); }} catch (e) {{}}
    btn.setAttribute('aria-pressed', showNasdaq ? 'true' : 'false');
    renderHeroChart();
  }});
}})();

// ---- Stagger animations
document.querySelectorAll('#ret-table tbody tr').forEach((row, i) => {{
  row.style.animationDelay = Math.min(i * 7, 700) + 'ms';
}});
document.querySelectorAll('.card').forEach((c, i) => {{
  c.style.animationDelay = Math.min(i * 5, 600) + 'ms';
}});

// ---- Sort
let sortState = {{col: -1, asc: false}};
document.querySelectorAll('#ret-table th[data-col]').forEach(th => {{
  th.addEventListener('click', (e) => {{
    e.stopPropagation();
    const col = +th.dataset.col;
    const numeric = th.dataset.num === '1';
    sortState.asc = (sortState.col === col) ? !sortState.asc : false;
    sortState.col = col;
    const tbody = document.querySelector('#ret-table tbody');
    const rows = [...tbody.querySelectorAll('tr')];
    rows.sort((a, b) => {{
      const ac = a.cells[col], bc = b.cells[col];
      const av = numeric ? parseFloat(ac.dataset.v ?? ac.textContent.replace(/[^-\\d.]/g,'')) : ac.textContent;
      const bv = numeric ? parseFloat(bc.dataset.v ?? bc.textContent.replace(/[^-\\d.]/g,'')) : bc.textContent;
      return (av < bv ? -1 : av > bv ? 1 : 0) * (sortState.asc ? 1 : -1);
    }});
    rows.forEach(r => tbody.appendChild(r));
    document.querySelectorAll('#ret-table th').forEach(x => x.classList.remove('sort-asc', 'sort-desc'));
    th.classList.add(sortState.asc ? 'sort-asc' : 'sort-desc');
  }});
}});
document.querySelector('#ret-table th[data-col="9"]')?.click();

// ---- Filtering
const TOTALS = Object.fromEntries(Object.entries(DATA).map(([t, d]) => [t, d.total]));
const WEIGHTS = Object.fromEntries(Object.entries(DATA).map(([t, d]) => [t, d.weight]));

function applyFilter(panel) {{
  const search = (panel.querySelector('.search')?.value || '').trim().toUpperCase();
  // Status filter and sector filter live in two separate `.chips` rows so
  // each axis has its own active chip. Read both.
  const activeStatus = panel.querySelector('.chips:not(.chips-sectors) .chip.active');
  const mode = activeStatus ? activeStatus.dataset.filter : 'all';
  const activeSector = panel.querySelector('.chips-sectors .chip.active');
  const sector = activeSector ? activeSector.dataset.sector : '*';
  const sorted = Object.entries(TOTALS).sort((a, b) => b[1] - a[1]);
  let allowed = null;
  if (mode === 'top10') allowed = new Set(sorted.slice(0, 10).map(([t]) => t));
  else if (mode === 'bottom10') allowed = new Set(sorted.slice(-10).map(([t]) => t));

  const items = panel.querySelectorAll('#ret-table tbody tr');
  items.forEach(el => {{
    const t = el.dataset.ticker;
    const total = parseFloat(el.dataset.total);
    const weight = parseFloat(el.dataset.weight) || 0;
    const rowSector = el.dataset.sector || '';
    let show = true;
    if (search && !t.includes(search)) show = false;
    if (mode === 'basket' && weight <= 0) show = false;
    if (mode === 'closed' && weight > 0) show = false;
    if (mode === 'winners' && total < 0) show = false;
    if (mode === 'losers' && total >= 0) show = false;
    if (allowed && !allowed.has(t)) show = false;
    if (sector !== '*' && rowSector !== sector) show = false;
    el.classList.toggle('hidden', !show);
  }});
}}

document.querySelectorAll('.panel').forEach(panel => {{
  panel.querySelectorAll('.chip').forEach(chip => {{
    chip.addEventListener('click', (e) => {{
      e.stopPropagation();
      const group = chip.closest('.chips');
      group.querySelectorAll('.chip').forEach(c => c.classList.remove('active'));
      chip.classList.add('active');
      applyFilter(panel);
      // v2.1 #3: Apply default sort for the new status-filter mode. Closed
      // positions default to Signal (col 4) so like-signal rows group together
      // for fast triage ("which closed names exited on Strong uptrend?"); every
      // other mode falls back to Since-baseline (col 9), the original default.
      // Only fires for status chips -- sector-filter chips keep current sort.
      if (!group.classList.contains('chips-sectors')) {{
        const newMode = chip.dataset.filter;
        const defaultCol = (newMode === 'closed') ? 4 : 9;
        if (sortState.col !== defaultCol) {{
          const targetTh = document.querySelector(`#ret-table th[data-col="${{defaultCol}}"]`);
          if (targetTh) targetTh.click();
        }}
      }}
      // v1.8.1 B5: reset scroll to top when filter changes -- otherwise the
      // user is mid-list in "Open" and switching to "Closed" leaves them at
      // an arbitrary scroll position in a now-different dataset.
      const scrollWrap = panel.querySelector('.table-scroll');
      if (scrollWrap) scrollWrap.scrollTop = 0;
    }});
  }});
  const s = panel.querySelector('.search');
  if (s) {{
    s.addEventListener('input', () => {{
      applyFilter(panel);
      const scrollWrap = panel.querySelector('.table-scroll');
      if (scrollWrap) scrollWrap.scrollTop = 0;
    }});
    s.addEventListener('click', (e) => e.stopPropagation());
  }}
}});

// Default to showing only Open positions — that's the actionable view for
// most sessions. User can still click All to see closed too.
document.querySelector('.chip[data-filter="basket"]')?.click();

// ---- Modal
const modal = document.getElementById('modal');
const modalSvg = modal.querySelector('.modal-chart');
const modalTip = modal.querySelector('.modal-tip');
const modalChartWrap = modal.querySelector('.modal-chart-wrap');

let currentTicker = null;
let chartPoints = null;

async function openModal(ticker) {{
  let d = DATA[ticker];
  if (!d) return;
  // v2.0 lazy-modal: if HEAVY hasn't been fetched/merged into this ticker yet,
  // open the modal with a light-only header + chart-area spinner, then await
  // the payload. On the COMMON path (prefetch completed during idle), this
  // resolves synchronously and the user sees no delay. On the cold path
  // (~400-650ms on 4G), the spinner makes the wait feel intentional.
  if (HEAVY_URL && !d.__hydrated) {{
    modal.querySelector('.modal-ticker').textContent = ticker;
    modal.querySelector('.modal-name').textContent = d.name || ticker;
    modal.querySelector('.modal-industry').textContent = d.industry || d.sector || '';
    const _pctEarly = modal.querySelector('.modal-pct');
    _pctEarly.textContent = fmtPct(d.total, true);
    _pctEarly.className = 'modal-pct ' + (d.total >= 0 ? 'pos' : 'neg');
    const loadingEl = modal.querySelector('.modal-loading');
    if (loadingEl) loadingEl.hidden = false;
    modal.removeAttribute('hidden');
    document.body.classList.add('modal-open');
    const heavy = await loadHeavy();
    // Only mark hydrated when the fetch SUCCEEDED. loadHeavy() returns null
    // only on fetch failure (e.g. a GitHub Pages redeploy window); leaving
    // __hydrated unset in that case lets reopening the modal retry, instead
    // of poisoning the ticker with a permanent light-only (blank) modal.
    if (heavy) {{
      if (heavy[ticker]) Object.assign(DATA[ticker], heavy[ticker]);
      DATA[ticker].__hydrated = true;   // payload may simply lack this ticker -- fine
    }}
    d = DATA[ticker];
    if (loadingEl) loadingEl.hidden = true;
  }}
  const tickerEl = modal.querySelector('.modal-ticker');
  const ccyBadge = (d.currency && d.currency !== '{BASE_CCY}') ?
    ` <span class="badge-ccy">${{d.currency}}</span>` : '';
  let weightBadge;
  if (d.status === 'open') {{
    weightBadge = ` <span class="badge-weight" title="${{d.shares_held}} units (1 unit per trade entry)">${{d.shares_held}} u</span>`;
  }} else if (d.status === 'watch') {{
    weightBadge = ' <span class="badge-watch" title="On watchlist, not held">WATCH</span>';
  }} else {{
    weightBadge = ' <span class="badge-closed">CLOSED</span>';
  }}
  tickerEl.innerHTML = ticker + ccyBadge + weightBadge;
  modal.querySelector('.modal-name').textContent = d.name || ticker;
  modal.querySelector('.modal-industry').textContent = d.industry || d.sector || '';
  const pct = modal.querySelector('.modal-pct');
  pct.textContent = fmtPct(d.total, true);
  pct.className = 'modal-pct ' + (d.total >= 0 ? 'pos' : 'neg');
  // v1.8.1 B4: closed positions show the hold window, not "since [buy]".
  // The % is buy-avg→sell-avg, so the label should reflect that range so
  // users don't read it as a current-date return.
  let sinceLabel;
  if (d.status === 'watch') {{
    sinceLabel = 'last 12 months';
  }} else if (d.status === 'closed' && d.last_action_date) {{
    sinceLabel = 'between ' + fmtDate(d.baseline_date) + ' and ' + fmtDate(d.last_action_date);
  }} else {{
    sinceLabel = 'since ' + fmtDate(d.baseline_date);
  }}
  modal.querySelector('.modal-since').textContent = sinceLabel;
  // Signal line
  const sigEl = modal.querySelector('#modal-signal');
  if (d.signal) {{
    sigEl.querySelector('.modal-signal-text').textContent = d.signal;
    sigEl.querySelector('.modal-signal-text').className = 'modal-signal-text ' + (d.signal_tone || 'neutral');
    sigEl.querySelector('.modal-signal-detail').textContent = d.signal_detail || '';
    sigEl.removeAttribute('hidden');
  }} else {{
    sigEl.setAttribute('hidden', '');
  }}
  // FX attribution line — only for non-base-currency stocks
  const fxEl = modal.querySelector('#modal-fx');
  if (d.currency && d.currency !== '{BASE_CCY}' && d.native_total !== null && d.fx_change !== null) {{
    const stockB = fxEl.querySelector('#fx-stock');
    const fxB = fxEl.querySelector('#fx-fx');
    const totalB = fxEl.querySelector('#fx-total');
    stockB.textContent = fmtPct(d.native_total, true) + ' (' + d.currency + ')';
    stockB.className = d.native_total >= 0 ? 'pos' : 'neg';
    fxB.textContent = fmtPct(d.fx_change, true);
    fxB.className = d.fx_change >= 0 ? 'pos' : 'neg';
    totalB.textContent = fmtPct(d.total, true) + ' ({BASE_CCY})';
    totalB.className = d.total >= 0 ? 'pos' : 'neg';
    fxEl.removeAttribute('hidden');
  }} else {{
    fxEl.setAttribute('hidden', '');
  }}
  const vals = {{
    baseline: fmtMoney(d.baseline),
    latest: fmtMoney(d.latest),
    w1: fmtPct(d.w1, true),
    m1: fmtPct(d.m1, true),
    m3: fmtPct(d.m3, true),
    ytd: fmtPct(d.ytd, true),
  }};
  modal.querySelectorAll('.modal-stat-val[data-key]').forEach(el => {{
    const k = el.dataset.key;
    el.textContent = vals[k];
    el.className = 'modal-stat-val';
    if (k.match(/^(w1|m1|m3|ytd)$/) && d[k] !== null && d[k] !== undefined && !Number.isNaN(d[k])) {{
      el.classList.add(d[k] >= 0 ? 'pos' : 'neg');
    }}
  }});
  // ---- Quant signals sub-row -----------------------------------------
  // SMA200 distance / ATR / RSI / 52w position / Volume. Falls back to
  // dim "—" when a metric couldn't be computed (e.g. <200 days history,
  // or no FX rate to convert ATR into base currency).
  const q = d.quant || {{}};
  const isNum = (v) => v !== null && v !== undefined && !Number.isNaN(v);
  const sma = isNum(q.sma200_dist_pct)
    ? ((q.sma200_dist_pct >= 0 ? '+' : '') + q.sma200_dist_pct.toFixed(1) + '%') : '—';
  const atr = isNum(q.atr14_gbp) ? ('{BASE_SYMBOL}' + q.atr14_gbp.toFixed(2)) : '—';
  const atrMeta = isNum(q.atr14_pct) ? (q.atr14_pct.toFixed(1) + '% of price') : '';
  const rsi = isNum(q.rsi14) ? q.rsi14.toFixed(0) : '—';
  const rsiMeta = isNum(q.rsi14)
    ? (q.rsi14 >= 70 ? 'overbought' : q.rsi14 <= 30 ? 'oversold' : 'neutral') : '';
  const rng = isNum(q.range52w_pct) ? (q.range52w_pct.toFixed(0) + '%') : '—';
  const rngMeta = isNum(q.range52w_pct)
    ? (q.range52w_pct >= 75 ? 'near high' : q.range52w_pct <= 25 ? 'near low' : 'mid-range') : '';
  const vol = isNum(q.vol_ratio) ? (q.vol_ratio.toFixed(1) + '×') : '—';
  const volMeta = isNum(q.vol_ratio) ? (q.vol_ratio >= 1.0 ? 'above avg' : 'below avg') : '';
  const qMap = {{
    sma200_dist_pct: {{ val: sma, meta: '' }},
    atr14_gbp:       {{ val: atr, meta: atrMeta }},
    rsi14:           {{ val: rsi, meta: rsiMeta }},
    range52w_pct:    {{ val: rng, meta: rngMeta }},
    vol_ratio:       {{ val: vol, meta: volMeta }},
  }};
  // v2.1 #2: value-aware tooltip explaining each quant signal. Built from the
  // numeric value + threshold zone so the user sees "RSI 81 — overbought" not
  // just an abstract definition. Interpretation strings are generated here in
  // JS rather than baked per-ticker into HEAVY (saves ~5 KB × 187 tickers).
  function quantTitle(k, v) {{
    if (!isNum(v)) return 'No data — typically <200 days of price history or missing FX rate.';
    if (k === 'sma200_dist_pct') {{
      const dir = v >= 0 ? 'above' : 'below';
      return `Price is ${{Math.abs(v).toFixed(1)}}% ${{dir}} the 200-day moving average. ` +
             `Long-term trend ${{v >= 0 ? 'up' : 'down'}}; persistent moves below the SMA200 often signal sustained weakness.`;
    }}
    if (k === 'atr14_gbp') {{
      return `Average True Range over 14 days = {BASE_SYMBOL}${{v.toFixed(2)}}. ` +
             `Typical daily price swing in absolute terms. A common stop-loss buffer is 2× ATR below entry for long positions.`;
    }}
    if (k === 'rsi14') {{
      const zone = v >= 70 ? 'Overbought (>70) — strong recent buying; momentum may exhaust soon.' :
                   v <= 30 ? 'Oversold (<30) — heavy recent selling; price may rebound.' :
                   'Neutral (30–70) — no clear momentum signal.';
      return `RSI(14) = ${{v.toFixed(0)}}. ${{zone}}`;
    }}
    if (k === 'range52w_pct') {{
      const zone = v >= 75 ? 'Near 52-week high — potential resistance or strong momentum signal.' :
                   v <= 25 ? 'Near 52-week low — potential support level or value zone.' :
                   'Mid-range.';
      return `At ${{v.toFixed(0)}}% of the 52-week price range (0% = year low, 100% = year high). ${{zone}}`;
    }}
    if (k === 'vol_ratio') {{
      const zone = v >= 2.0 ? 'Unusual — > 2× typical, often news-driven.' :
                   v >= 1.3 ? 'Elevated — above-average activity.' :
                   v >= 0.7 ? 'Roughly typical.' :
                   'Quiet — below-average activity.';
      return `Recent volume = ${{v.toFixed(1)}}× the 50-day average. ${{zone}}`;
    }}
    return '';
  }}
  modal.querySelectorAll('[data-qkey]').forEach(el => {{
    const k = el.dataset.qkey;
    const entry = qMap[k];
    if (!entry) return;
    el.textContent = entry.val;
    el.className = 'modal-stat-val';
    const v = q[k];
    el.setAttribute('title', quantTitle(k, v));
    if (!isNum(v)) {{ el.classList.add('dim'); return; }}
    if (k === 'sma200_dist_pct') el.classList.add(v >= 0 ? 'pos' : 'neg');
    else if (k === 'rsi14') {{
      if (v >= 70) el.classList.add('neg');
      else if (v <= 30) el.classList.add('pos');
    }} else if (k === 'range52w_pct') {{
      if (v >= 75) el.classList.add('pos');
      else if (v <= 25) el.classList.add('neg');
    }} else if (k === 'vol_ratio') {{
      el.classList.add(v >= 1.0 ? 'pos' : 'neg');
    }}
    // atr14_gbp stays neutral — it's a magnitude, not a direction.
  }});
  modal.querySelectorAll('[data-qmeta]').forEach(el => {{
    const k = el.dataset.qmeta;
    el.textContent = (qMap[k] && qMap[k].meta) || '';
  }});
  // ---- Per-ticker recent news ----------------------------------------
  // Shown only when the build-time cache had items for this ticker. Empty
  // section is hidden entirely so the modal doesn't carry a dead row.
  const newsBox = document.getElementById('modal-news');
  const newsList = newsBox.querySelector('.modal-news-list');
  const newsStale = newsBox.querySelector('.modal-news-staleness');
  const items = Array.isArray(d.news) ? d.news : [];
  if (items.length) {{
    newsList.innerHTML = items.map(it => {{
      const safeTitle = escapeNewsHtml(it.title || '');
      const safeLink = safeUrl(it.link);
      const safePub = escapeNewsHtml(it.publisher || '');
      const when = it.published ? relativeNewsTime(new Date(it.published)) : '';
      const safeWhen = escapeNewsHtml(when);
      return `<a class="modal-news-row" href="${{safeLink}}" target="_blank" rel="noopener noreferrer">`
        + `<div class="modal-news-title">${{safeTitle}}</div>`
        + `<div class="modal-news-meta">`
        + (safePub ? `<span class="modal-news-pub">${{safePub}}</span>` : '')
        + (safePub && safeWhen ? `<span class="modal-news-dot">&middot;</span>` : '')
        + (safeWhen ? `<span class="modal-news-when">${{safeWhen}}</span>` : '')
        + `</div></a>`;
    }}).join('');
    newsStale.textContent = 'cached weekly';
    newsBox.removeAttribute('hidden');
  }} else {{
    newsList.innerHTML = '';
    newsStale.textContent = '';
    newsBox.setAttribute('hidden', '');
  }}
  modal.removeAttribute('hidden');
  document.body.classList.add('modal-open');
  // v1.8.1 B5: reset scroll so each new ticker opens at the top, not at the
  // previous ticker's last scroll position.
  const mc = modal.querySelector('.modal-card');
  if (mc) mc.scrollTop = 0;
  requestAnimationFrame(() => renderBigChart(ticker));
}}

function closeModal() {{
  modal.setAttribute('hidden', '');
  document.body.classList.remove('modal-open');
  modalTip.setAttribute('hidden', '');
}}

// v1.8 T1: viewBox coordinate space — server pre-renders the polyline + tick
// geometry into `d.chart`, we just draw it. preserveAspectRatio="none" makes
// the browser scale the viewBox to the modal's actual pixel size, so no
// per-open recalc + no resize handler are needed.
const MODAL_VB_W = {MODAL_VB_W};
const MODAL_VB_H = {MODAL_VB_H};
const MODAL_VB_PAD_L = {MODAL_VB_PAD_L};
const MODAL_VB_PAD_T = {MODAL_VB_PAD_T};
const MODAL_VB_INNER_W = MODAL_VB_W - MODAL_VB_PAD_L - {MODAL_VB_PAD_R};
const MODAL_VB_INNER_H = MODAL_VB_H - MODAL_VB_PAD_T - {MODAL_VB_PAD_B};

// T1: derive per-point viewBox coords from `n` + index. Mirrors _modal_polyline_d.
function _modalX(i, n) {{
  if (n <= 1) return MODAL_VB_PAD_L + MODAL_VB_INNER_W / 2;
  return MODAL_VB_PAD_L + (i / (n - 1)) * MODAL_VB_INNER_W;
}}
function _modalY(v, vmin, vmax) {{
  const span = Math.max(vmax - vmin, 1e-9);
  return MODAL_VB_PAD_T + (1 - (v - vmin) / span) * MODAL_VB_INNER_H;
}}

function renderBigChart(ticker) {{
  currentTicker = ticker;
  // v2.1 #1: HTML overlay for axis labels (escapes the SVG viewBox stretch).
  // Cleared first so prior-ticker labels don't bleed into the loading state.
  const labelsEl = modal.querySelector('.modal-chart-labels');
  if (labelsEl) labelsEl.innerHTML = '';
  const d = DATA[ticker];
  if (!d || !d.chart || !d.chart.points) {{
    modalSvg.innerHTML = '';
    chartPoints = null;
    return;
  }}
  const chart = d.chart;
  const rebased = d.rebased || d.prices.map(p => (p / d.baseline - 1) * 100);
  const dates = d.dates;
  const prices = d.prices;
  const n = chart.n;
  const xs = new Array(n);
  const ys = new Array(n);
  for (let i = 0; i < n; i++) {{
    xs[i] = _modalX(i, n);
    ys[i] = _modalY(rebased[i], chart.vmin, chart.vmax);
  }}
  const isUp = d.total >= 0;
  const color = isUp ? '#34d399' : '#f87171';
  const gradId = isUp ? 'grad-up-lg' : 'grad-down-lg';
  const labelY = MODAL_VB_PAD_T + MODAL_VB_INNER_H;
  // Area path = polyline + corner anchors. Cheap to rebuild from `chart.points`.
  const firstX = xs[0], lastX = xs[n - 1];
  const areaD = `M ${{firstX.toFixed(1)}},${{labelY.toFixed(1)}} L ${{chart.points.replaceAll(' ', ' L ')}} L ${{lastX.toFixed(1)}},${{labelY.toFixed(1)}} Z`;

  // Build SVG from precomputed geometry. v2.1 #1: text moved out of SVG into
  // HTML overlay (`labelsHtml`) so it renders at native browser DPI without
  // inheriting the SVG's non-uniform viewBox stretch. The SVG keeps grid
  // lines, the zero baseline, the polyline, gradients, and crosshair geometry.
  let html = '';
  const labelsHtml = [];
  // Y-axis: SVG line for the grid, HTML span for the percentage label.
  const yTickXPct = ((MODAL_VB_PAD_L - 4) / MODAL_VB_W * 100).toFixed(3);
  for (const t of chart.y_ticks) {{
    html += `<line x1="${{MODAL_VB_PAD_L}}" y1="${{t.y.toFixed(1)}}" x2="${{(MODAL_VB_PAD_L + MODAL_VB_INNER_W).toFixed(1)}}" y2="${{t.y.toFixed(1)}}" stroke="rgba(255,255,255,0.04)" stroke-width="1"/>`;
    const yPct = (t.y / MODAL_VB_H * 100).toFixed(3);
    const sign = t.v >= 0 ? '+' : '';
    labelsHtml.push(`<span class="y-tick" style="left:${{yTickXPct}}%;top:${{yPct}}%">${{sign}}${{t.v.toFixed(0)}}%</span>`);
  }}
  html += `<line x1="${{MODAL_VB_PAD_L}}" y1="${{chart.zero_y.toFixed(1)}}" x2="${{(MODAL_VB_PAD_L + MODAL_VB_INNER_W).toFixed(1)}}" y2="${{chart.zero_y.toFixed(1)}}" stroke="rgba(255,255,255,0.18)" stroke-width="0.8" stroke-dasharray="3 3"/>`;
  // X-axis: pure HTML, no SVG text. Position is just below the chart area
  // (labelY = chart's bottom edge, +12 viewBox-units of breathing room).
  const xLabelTopPct = ((labelY + 12) / MODAL_VB_H * 100).toFixed(3);
  for (const idx of chart.x_tick_idx) {{
    const x = _modalX(idx, n);
    const xPct = (x / MODAL_VB_W * 100).toFixed(3);
    labelsHtml.push(`<span class="x-tick" style="left:${{xPct}}%;top:${{xLabelTopPct}}%">${{fmtDate(dates[idx])}}</span>`);
  }}
  html += `<path d="${{areaD}}" fill="url(#${{gradId}})"/>`;
  // v1.9 #2: per-segment color on the modal polyline. Render each
  // same-sign run in its own color so below-baseline periods are visibly red
  // even when the position's overall total is positive. Falls back to the
  // single-color polyline if segments are missing (defensive for any payload
  // older than v1.9).
  if (Array.isArray(chart.segments) && chart.segments.length > 0) {{
    for (const seg of chart.segments) {{
      const segColor = seg.above ? '#34d399' : '#f87171';
      html += `<polyline points="${{seg.pts}}" fill="none" stroke="${{segColor}}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
    }}
  }} else {{
    html += `<polyline points="${{chart.points}}" fill="none" stroke="${{color}}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
  }}
  html += `<line class="crosshair" x1="0" y1="${{MODAL_VB_PAD_T}}" x2="0" y2="${{labelY}}" stroke="${{color}}" stroke-width="0.8" stroke-dasharray="2 3" opacity="0"/>`;
  html += `<circle class="dot" cx="0" cy="0" r="6" fill="${{color}}" stroke="${{color}}" opacity="0"/>`;

  // Transaction markers (buy/sell dots) — only if the per-stock transactions are available
  if (d.transactions && d.transactions.length > 0) {{
    // De-dup: many rapid trades on a long axis snap to the same weekly bucket
    // and would stack into one illegible blob. Keep one marker per (week, side).
    const seenMarkers = new Set();
    for (const t of d.transactions) {{
      const txnTime = new Date(t.date).getTime();
      let bestIdx = 0, bestDiff = Infinity;
      for (let i = 0; i < dates.length; i++) {{
        const diff = Math.abs(new Date(dates[i]).getTime() - txnTime);
        if (diff < bestDiff) {{ bestDiff = diff; bestIdx = i; }}
      }}
      const seenKey = bestIdx + '|' + t.action;
      if (seenMarkers.has(seenKey)) continue;
      seenMarkers.add(seenKey);
      const mx = xs[bestIdx];
      const isBuy = t.action === 'BUY';
      const mColor = isBuy ? '#34d399' : '#f87171';
      const label = isBuy ? 'B' : 'S';
      const ly = labelY - 8;
      html += `<line x1="${{mx.toFixed(1)}}" y1="${{MODAL_VB_PAD_T}}" x2="${{mx.toFixed(1)}}" y2="${{labelY.toFixed(1)}}" stroke="${{mColor}}" stroke-width="0.7" stroke-dasharray="3 2" opacity="0.45"/>`;
      html += `<circle cx="${{mx.toFixed(1)}}" cy="${{ly.toFixed(1)}}" r="9" fill="${{mColor}}" stroke="#0b0e17" stroke-width="0.5"/>`;
      // v2.1 #1: the B/S character moves to the HTML overlay so it renders
      // legibly inside the circle. The circle itself stays in SVG and stretches
      // slightly into an ellipse, but at r=9 the distortion is barely visible.
      const mxPct = (mx / MODAL_VB_W * 100).toFixed(3);
      const lyPct = (ly / MODAL_VB_H * 100).toFixed(3);
      labelsHtml.push(`<span class="txn-marker" style="left:${{mxPct}}%;top:${{lyPct}}%">${{label}}</span>`);
    }}
  }}

  // Cost-basis tick: a faint amber vertical at the active-cycle baseline date,
  // marking where the headline % is anchored on the now-fuller path. Only drawn
  // when there is pre-baseline history (otherwise it just sits on the left edge).
  if (d.baseline_date && dates.length > 1) {{
    const bTime = new Date(d.baseline_date).getTime();
    let bIdx = 0, bDiff = Infinity;
    for (let i = 0; i < dates.length; i++) {{
      const diff = Math.abs(new Date(dates[i]).getTime() - bTime);
      if (diff < bDiff) {{ bDiff = diff; bIdx = i; }}
    }}
    if (bIdx > 0) {{
      const cx = xs[bIdx];
      html += `<line x1="${{cx.toFixed(1)}}" y1="${{MODAL_VB_PAD_T}}" x2="${{cx.toFixed(1)}}" y2="${{labelY.toFixed(1)}}" stroke="#fbbf24" stroke-width="0.9" stroke-dasharray="2 3" opacity="0.55"/>`;
      const cxPct = (cx / MODAL_VB_W * 100).toFixed(3);
      const cyPct = ((MODAL_VB_PAD_T - 2) / MODAL_VB_H * 100).toFixed(3);
      labelsHtml.push(`<span class="cost-tick" style="left:${{cxPct}}%;top:${{cyPct}}%">cost</span>`);
    }}
  }}

  modalSvg.innerHTML = html;
  if (labelsEl) labelsEl.innerHTML = labelsHtml.join('');
  chartPoints = {{xs, ys, dates, prices, rebased, color}};
}}

modalSvg.addEventListener('mousemove', (e) => {{
  if (!chartPoints) return;
  const rect = modalSvg.getBoundingClientRect();
  const x = (e.clientX - rect.left) * (modalSvg.viewBox.baseVal.width / rect.width);
  let bestI = 0, bestDist = Infinity;
  for (let i = 0; i < chartPoints.xs.length; i++) {{
    const d = Math.abs(chartPoints.xs[i] - x);
    if (d < bestDist) {{ bestDist = d; bestI = i; }}
  }}
  const px = chartPoints.xs[bestI], py = chartPoints.ys[bestI];
  const cross = modalSvg.querySelector('.crosshair');
  const dot = modalSvg.querySelector('.dot');
  cross.setAttribute('x1', px); cross.setAttribute('x2', px); cross.setAttribute('opacity', '0.7');
  dot.setAttribute('cx', px); dot.setAttribute('cy', py); dot.setAttribute('opacity', '1');
  const tipX = (px / modalSvg.viewBox.baseVal.width) * rect.width;
  const tipY = (py / modalSvg.viewBox.baseVal.height) * rect.height;
  const wrapRect = modalChartWrap.getBoundingClientRect();
  modalTip.style.left = (tipX + (rect.left - wrapRect.left)) + 'px';
  modalTip.style.top = (tipY + (rect.top - wrapRect.top)) + 'px';
  const reb = chartPoints.rebased[bestI];
  modalTip.innerHTML = `<div class="tip-date">${{fmtDate(chartPoints.dates[bestI])}}</div>` +
                       `<div><span class="tip-price">${{fmtMoney(chartPoints.prices[bestI])}}</span>` +
                       `<span class="tip-pct ${{reb >= 0 ? 'pos' : 'neg'}}">${{fmtPct(reb, true)}}</span></div>`;
  modalTip.removeAttribute('hidden');
}});
modalSvg.addEventListener('mouseleave', () => {{
  modalTip.setAttribute('hidden', '');
  const c = modalSvg.querySelector('.crosshair');
  const d = modalSvg.querySelector('.dot');
  if (c) c.setAttribute('opacity', '0');
  if (d) d.setAttribute('opacity', '0');
}});

modal.querySelector('.modal-close').addEventListener('click', closeModal);
modal.addEventListener('click', (e) => {{ if (e.target === modal) closeModal(); }});
// Stack-aware ESC: ticker modal takes priority (it's visually on top).
// If both modals are open, the FIRST Escape closes ticker only -- the info
// modal stays visible underneath. A second Escape closes info. This single
// handler replaces both the old ticker-only handler and the separate info
// handler in the click-to-expand block below.
document.addEventListener('keydown', (e) => {{
  if (e.key !== 'Escape') return;
  if (!modal.hasAttribute('hidden')) {{ closeModal(); return; }}
  if (typeof infoModal !== 'undefined' && infoModal && !infoModal.hasAttribute('hidden')) {{
    closeInfoModal();
  }}
}});
// T1: resize handler removed — viewBox="0 0 1000 600" + preserveAspectRatio="none"
// makes the SVG scale natively. No re-render needed on window resize.

document.querySelectorAll('#ret-table tbody tr').forEach(row => {{
  row.addEventListener('click', () => openModal(row.dataset.ticker));
}});
document.querySelectorAll('.contrib-table tbody tr, .regret-table tbody tr, .dt-table tbody tr').forEach(row => {{
  row.addEventListener('click', () => openModal(row.dataset.ticker));
}});
document.querySelectorAll('.wl-card, .an-card').forEach(card => {{
  card.addEventListener('click', () => openModal(card.dataset.ticker));
}});
document.querySelectorAll('.io-stock').forEach(row => {{
  row.addEventListener('click', () => openModal(row.dataset.ticker));
}});
// v2.5 #9: Big Brain "couple" pages -> arrows flip between alternate sets.
document.querySelectorAll('.bigbrain-section[data-bb-pages]').forEach(sec => {{
  const pages = Array.from(sec.querySelectorAll('.bb-page'));
  if (pages.length < 2) return;
  const ind = sec.querySelector('.bb-page-cur');
  let cur = 0;
  const show = i => {{
    cur = (i + pages.length) % pages.length;
    pages.forEach((p, idx) => p.classList.toggle('active', idx === cur));
    if (ind) ind.textContent = (cur + 1);
  }};
  const prev = sec.querySelector('.bb-prev');
  const next = sec.querySelector('.bb-next');
  if (prev) prev.addEventListener('click', () => show(cur - 1));
  if (next) next.addEventListener('click', () => show(cur + 1));
}});
// v2.5 #3: Market expectations "Reshuffle" -> slide a window over the deeper
// theme pool, wrapping around (no repeats until the whole pool is exhausted).
document.querySelectorAll('.market-expectations-section').forEach(sec => {{
  const list = sec.querySelector('.me-list');
  if (!list) return;
  const win = parseInt(list.dataset.meWindow || '0', 10);
  const rows = Array.from(list.querySelectorAll('.me-row'));
  if (!win || rows.length <= win) return;
  let start = 0;
  const paint = () => rows.forEach((r, i) => {{
    r.style.display = (i >= start && i < start + win) ? '' : 'none';
  }});
  paint();
  const btn = sec.querySelector('.me-reshuffle');
  if (btn) btn.addEventListener('click', () => {{
    start += win;
    if (start >= rows.length) start = 0;
    paint();
  }});
}});
// v2.6 Value screen: page the scorecard rows in fixed windows with flip arrows.
document.querySelectorAll('.value-screen-section[data-vs-pages]').forEach(sec => {{
  const table = sec.querySelector('.vs-table');
  const size = parseInt((table && table.dataset.vsPage) || '0', 10);
  const rows = Array.from(sec.querySelectorAll('.vs-row'));
  if (!size || rows.length <= size) return;
  const npages = Math.ceil(rows.length / size);
  const ind = sec.querySelector('.vs-page-cur');
  let cur = 0;
  const paint = () => {{
    rows.forEach((r, i) => {{
      r.style.display = (i >= cur * size && i < (cur + 1) * size) ? '' : 'none';
    }});
    if (ind) ind.textContent = (cur + 1);
  }};
  paint();
  const prev = sec.querySelector('.vs-prev');
  const next = sec.querySelector('.vs-next');
  if (prev) prev.addEventListener('click', () => {{ cur = (cur - 1 + npages) % npages; paint(); }});
  if (next) next.addEventListener('click', () => {{ cur = (cur + 1) % npages; paint(); }});
}});
// v3.0 #4: watchlist arrow pager. Page size = however many cards fill the row
// (read live from the grid's column count), so page 1 fills before page 2 and
// it re-flows on resize. The nav hides itself when one row already holds all.
document.querySelectorAll('.watchlist-section[data-wl-pageable]').forEach(sec => {{
  const grid = sec.querySelector('.wl-grid');
  const cards = Array.from(sec.querySelectorAll('.wl-card'));
  const nav = sec.querySelector('.wl-nav');
  const ind = sec.querySelector('.wl-page-cur');
  const tot = sec.querySelector('.wl-page-total');
  if (!grid || cards.length === 0) return;
  let cur = 0;
  const cols = () => {{
    const tpl = getComputedStyle(grid).gridTemplateColumns;
    return Math.max(1, tpl.split(' ').filter(Boolean).length);
  }};
  const npages = () => Math.max(1, Math.ceil(cards.length / cols()));
  const paint = () => {{
    const size = cols(), np = npages();
    if (cur >= np) cur = np - 1;
    if (cur < 0) cur = 0;
    cards.forEach((c, i) => {{
      c.style.display = (i >= cur * size && i < (cur + 1) * size) ? '' : 'none';
    }});
    if (nav) nav.style.display = np > 1 ? '' : 'none';
    if (ind) ind.textContent = (cur + 1);
    if (tot) tot.textContent = np;
  }};
  paint();
  const prev = sec.querySelector('.wl-prev');
  const next = sec.querySelector('.wl-next');
  if (prev) prev.addEventListener('click', () => {{ const np = npages(); cur = (cur - 1 + np) % np; paint(); }});
  if (next) next.addEventListener('click', () => {{ const np = npages(); cur = (cur + 1) % np; paint(); }});
  let rt; window.addEventListener('resize', () => {{ clearTimeout(rt); rt = setTimeout(paint, 150); }});
}});
// T9/T10: generic ticker-clickable handler. Anything bearing the class +
// data-ticker opens the modal. Used by the rating-moves panel ticker spans
// and the unusual-volume hero chips. Cheaper than registering per-section
// selectors and stays correct if future sections use the same convention.
document.querySelectorAll('.ticker-clickable[data-ticker]').forEach(el => {{
  el.addEventListener('click', (e) => {{
    e.preventDefault();
    e.stopPropagation();
    openModal(el.dataset.ticker);
  }});
}});

// ============================================================================
// T11/T12/T14/T15: info-modal stack for click-to-expand drill-downs.
//
// Architecture: separate modal element (#info-modal) at lower z-index than
// the existing ticker modal. When a user clicks an industry / sector /
// pair-bucket / weekly-point, openInfoModal() shows the info-modal with the
// relevant content. Tickers within the info-modal also have .ticker-clickable
// -- their handler opens the existing ticker modal ON TOP of the info-modal
// (true stacking; closing the ticker modal returns to the info-modal).
//
// ESC key + backdrop click are routed via stack priority: if the ticker
// modal is open, those gestures close it first; only when it's already
// closed do they affect the info-modal.
// ============================================================================
const infoModal = document.getElementById('info-modal');
const infoModalTitle = infoModal.querySelector('.info-modal-title');
const infoModalSub = infoModal.querySelector('.info-modal-sub');
const infoModalBody = infoModal.querySelector('.info-modal-body');
const infoModalClose = document.getElementById('info-modal-close');

function openInfoModal(title, sub, contentHtml) {{
  infoModalTitle.innerHTML = title || '';
  infoModalSub.innerHTML = sub || '';
  infoModalBody.innerHTML = contentHtml || '';
  infoModal.removeAttribute('hidden');
  document.body.classList.add('modal-open');
  // v1.8.1 B5: reset scroll to top on new content so switching from one
  // drill-down to another always starts at the title, not mid-list.
  const ic = infoModal.querySelector('.modal-card');
  if (ic) ic.scrollTop = 0;
  // Wire any ticker-clickable spans inside the new content -- they were
  // injected after the initial DOMContentLoaded handlers ran, so attach now.
  // Use _safeOpenTicker so universe-only tickers get a sensible fallback
  // instead of silently failing.
  infoModalBody.querySelectorAll('.ticker-clickable[data-ticker]').forEach(el => {{
    el.addEventListener('click', (e) => {{
      e.preventDefault();
      e.stopPropagation();
      _safeOpenTicker(el.dataset.ticker);
    }});
  }});
}}
function closeInfoModal() {{
  infoModal.setAttribute('hidden', '');
  // Only release modal-open if the ticker modal isn't itself still open.
  if (modal.hasAttribute('hidden')) document.body.classList.remove('modal-open');
}}

infoModalClose.addEventListener('click', closeInfoModal);
infoModal.addEventListener('click', (e) => {{ if (e.target === infoModal) closeInfoModal(); }});
// ESC handling is centralized in the stack-aware handler near closeModal()
// above -- the consolidated handler closes the topmost open modal first.

// ----- Helpers for assembling info-modal content -----
function _capTierBadge(tier) {{
  if (!tier) return '';
  return `<span class="im-tier im-tier-${{tier.toLowerCase()}}">${{tier}}</span>`;
}}
function _pctSpan(v) {{
  if (v === null || v === undefined || Number.isNaN(v)) return '<span class="muted">&mdash;</span>';
  const cls = v >= 0 ? 'pos' : 'neg';
  return `<span class="${{cls}}">${{v >= 0 ? '+' : ''}}${{v.toFixed(1)}}%</span>`;
}}
// Universe-aware ticker opener. If DATA has the ticker (held / watch-listed),
// opens the existing rich ticker modal. Otherwise shows a "universe only"
// fallback info-modal with the limited fields we have for that ticker
// (industry, name, 12mo return, cap tier). This prevents silent no-ops when
// users click on universe-only tickers in the industry outlook breakdown.
function _safeOpenTicker(ticker) {{
  if (DATA[ticker]) {{ openModal(ticker); return; }}
  let entry = null, industry = '';
  const inds = AUX_DATA.industries || {{}};
  for (const ind in inds) {{
    const m = inds[ind].find(e => e.ticker === ticker);
    if (m) {{ entry = m; industry = ind; break; }}
  }}
  if (entry) {{
    const tierBadge = entry.cap_tier ? ' ' + _capTierBadge(entry.cap_tier) : '';
    const body = (
      `<table class="im-table im-uni-table"><tbody>`
      + `<tr><td class="muted">Name</td><td>${{escapeNewsHtml(entry.name)}}${{tierBadge}}</td></tr>`
      + `<tr><td class="muted">Industry</td><td>${{escapeNewsHtml(industry)}}</td></tr>`
      + `<tr><td class="muted">12-month return</td><td class="num">${{_pctSpan(entry.return_12mo)}}</td></tr>`
      + `</tbody></table>`
      + `<p class="muted im-uni-note">Limited data: this ticker is in the reference `
      + `<code>universe.csv</code> but not in your basket or watchlist. Add it to `
      + `<code>log.xlsx</code> or <code>watchlist.csv</code> for full chart, modal stats, and news.</p>`
    );
    openInfoModal(`${{escapeNewsHtml(ticker)}} &middot; universe only`, escapeNewsHtml(industry), body);
  }} else {{
    openInfoModal(escapeNewsHtml(ticker), 'no detail data',
      `<p class="muted">No detail data is loaded for this ticker.</p>`);
  }}
}}

// T11: industry-card click -> info-modal listing every ticker in that industry.
document.querySelectorAll('.industry-clickable[data-industry]').forEach(card => {{
  card.addEventListener('click', (e) => {{
    // If the user clicked an inner stock row whose ticker has detail DATA
    // (i.e. it's a held / watchlisted name with full OHLCV), let the inner
    // handler's openModal() win -- don't ALSO open the industry modal.
    // But if it's a universe-only ticker (no DATA), the inner openModal call
    // would silently fail; fall through to open the industry overview info
    // modal as the next-best drill-down.
    const inner = e.target.closest && e.target.closest('.io-stock');
    if (inner && inner.dataset.ticker && DATA[inner.dataset.ticker]) return;
    const ind = card.dataset.industry;
    const entries = (AUX_DATA.industries && AUX_DATA.industries[ind]) || [];
    const rows = entries.map(en => (
      `<tr class="ticker-clickable" data-ticker="${{en.ticker}}">`
      + `<td class="im-tkr">${{en.ticker}}${{_capTierBadge(en.cap_tier)}}</td>`
      + `<td class="im-name">${{escapeNewsHtml(en.name)}}</td>`
      + `<td class="num im-ret">${{_pctSpan(en.return_12mo)}}</td>`
      + `</tr>`
    )).join('');
    const sub = `${{entries.length}} tracked ticker${{entries.length === 1 ? '' : 's'}} &middot; sorted by 12-mo return`;
    const body = entries.length
      ? `<table class="im-table"><thead><tr><th>Ticker</th><th>Name</th><th class="num">12-mo</th></tr></thead><tbody>${{rows}}</tbody></table>`
      : `<p class="muted">No tracked tickers in this industry.</p>`;
    openInfoModal(`Industry &middot; ${{escapeNewsHtml(ind)}}`, sub, body);
  }});
}});

// T12: attribution-row click -> info-modal listing every open position in
// that industry (matched by the same industry-or-sector-fallback key).
document.querySelectorAll('.attribution-row-clickable[data-industry]').forEach(row => {{
  row.addEventListener('click', () => {{
    const key = row.dataset.industry;
    const positions = (AUX_DATA.sectors && AUX_DATA.sectors[key]) || [];
    const rows = positions.map(p => (
      `<tr class="ticker-clickable" data-ticker="${{p.ticker}}">`
      + `<td class="im-tkr">${{p.ticker}}</td>`
      + `<td class="im-name">${{escapeNewsHtml(p.name)}}</td>`
      + `<td class="num">${{p.weight_pct.toFixed(1)}}%</td>`
      + `<td class="num">${{_pctSpan(p.total_pct)}}</td>`
      + `<td class="num">${{_pctSpan(p.contribution_pp)}}</td>`
      + `</tr>`
    )).join('');
    const sub = `${{positions.length}} open position${{positions.length === 1 ? '' : 's'}} &middot; sorted by contribution`;
    const body = positions.length
      ? `<table class="im-table"><thead><tr><th>Ticker</th><th>Name</th><th class="num">Weight</th><th class="num">Return</th><th class="num">Contrib</th></tr></thead><tbody>${{rows}}</tbody></table>`
      : `<p class="muted">No open positions found for this industry.</p>`;
    openInfoModal(`Industry attribution &middot; ${{key}}`, sub, body);
  }});
}});

// T14: histogram-column click -> info-modal listing every pair whose
// correlation falls within the clicked bucket. The pairs list comes from
// AUX_DATA.pairs (pre-sorted by abs(corr) desc); we filter to [lo, hi]
// inline. Click target is the full-height column wrapper, so even tiny
// bars are easy to hit.
document.querySelectorAll('.div-hist-col-clickable[data-bucket-lo]').forEach(bar => {{
  bar.addEventListener('click', () => {{
    const lo = parseFloat(bar.dataset.bucketLo);
    const hi = parseFloat(bar.dataset.bucketHi);
    const pairs = (AUX_DATA.pairs || []).filter(p => p.corr >= lo && p.corr <= hi);
    const rows = pairs.map(p => {{
      const cls = p.corr >= 0.6 ? 'neg' : (p.corr <= 0 ? 'pos' : '');
      return (
        `<tr>`
        + `<td><span class="ticker-clickable im-tkr" data-ticker="${{p.a}}">${{p.a}}</span></td>`
        + `<td><span class="im-arrow">&harr;</span></td>`
        + `<td><span class="ticker-clickable im-tkr" data-ticker="${{p.b}}">${{p.b}}</span></td>`
        + `<td class="num ${{cls}}">${{p.corr >= 0 ? '+' : ''}}${{p.corr.toFixed(2)}}</td>`
        + `</tr>`
      );
    }}).join('');
    const sub = `${{pairs.length}} pair${{pairs.length === 1 ? '' : 's'}} in range &middot; click any ticker for detail`;
    const body = pairs.length
      ? `<table class="im-table"><thead><tr><th>A</th><th></th><th>B</th><th class="num">&rho;</th></tr></thead><tbody>${{rows}}</tbody></table>`
      : `<p class="muted">No pairs found in this bucket.</p>`;
    openInfoModal(`Correlation bucket &middot; ${{lo.toFixed(2)}} to ${{hi.toFixed(2)}}`, sub, body);
  }});
}});

// T15: hero-week-click rect -> info-modal of that week's top/bottom movers
// (basket-wide, in base currency). Delegated off the hero SVG since the
// rects are inserted dynamically by renderHeroChart (which fires multiple
// times across responsive resizes).
document.getElementById('hero-chart').addEventListener('click', (e) => {{
  const rect = e.target.closest && e.target.closest('.hero-week-click');
  if (!rect) return;
  const dateKey = rect.dataset.weekEnd;
  const wk = AUX_DATA.weekly_movers && AUX_DATA.weekly_movers[dateKey];
  if (!wk) {{
    openInfoModal(`Week ending ${{dateKey}}`,
      'no movers recorded for this week',
      '<p class="muted">No movement data available for this week.</p>');
    return;
  }}
  const mkRow = (m) => (
    `<tr class="ticker-clickable" data-ticker="${{m.ticker}}">`
    + `<td class="im-tkr">${{m.ticker}}</td>`
    + `<td class="num">${{_pctSpan(m.pct)}}</td>`
    + `</tr>`
  );
  const up   = (wk.up   || []).map(mkRow).join('');
  const down = (wk.down || []).map(mkRow).join('');
  const body = (
    `<div class="im-movers">`
    + `<div class="im-movers-col">`
    +   `<h4 class="im-movers-h pos">Top movers up</h4>`
    +   (up   ? `<table class="im-table"><tbody>${{up}}</tbody></table>`   : `<p class="muted">none</p>`)
    + `</div>`
    + `<div class="im-movers-col">`
    +   `<h4 class="im-movers-h neg">Top movers down</h4>`
    +   (down ? `<table class="im-table"><tbody>${{down}}</tbody></table>` : `<p class="muted">none</p>`)
    + `</div>`
    + `</div>`
  );
  openInfoModal(`Week ending ${{dateKey}}`,
    'top movers across your held tickers',
    body);
}});

// ---- Palette toggle ---------------------------------------------------
// Body class controls which set of CSS variables wins. Persist the choice
// across visits via localStorage so the page remembers the user's preference.
// Desktop-view override: lets users on narrow viewports force the full
// desktop layout (page becomes horizontally scrollable). Persisted via
// localStorage so the choice survives reloads. Mirrors the palette toggle's
// state-management pattern.
(function setupDesktopMode() {{
  const btn = document.getElementById('desktop-mode-btn');
  if (!btn) return;
  const KEY = 'stocks-dashboard-force-desktop';
  function apply(forced) {{
    document.body.classList.toggle('force-desktop', forced);
    btn.classList.toggle('active', forced);
    btn.setAttribute('aria-pressed', forced ? 'true' : 'false');
    btn.textContent = forced ? 'Mobile view' : 'Desktop view';
  }}
  let saved = false;
  try {{ saved = localStorage.getItem(KEY) === '1'; }} catch (e) {{}}
  apply(saved);
  btn.addEventListener('click', () => {{
    const next = !document.body.classList.contains('force-desktop');
    apply(next);
    try {{ localStorage.setItem(KEY, next ? '1' : '0'); }} catch (e) {{}}
  }});
}})();

// v1.9 Pocket Lesson: random tip on load + Next-tip rotation + topbar toggle.
// State: `pocketLessonOn` in localStorage ('1' / '0'). Default is OFF -- the
// card is collapsed until the user opens it from the topbar button. Once
// they toggle, the choice persists.
// The card transitions in/out via the .is-open class (max-height + margin +
// opacity), so the rest of the page smoothly slides to make/yield space.
(function setupPocketLesson() {{
  const STORAGE_KEY = 'pocketLessonOn';
  const CAT_KEY = 'pocketLessonCategory';
  const btn = document.getElementById('pocket-lesson-btn');
  const wrap = document.getElementById('pocket-lesson-wrap');
  const titleEl = document.getElementById('pocket-lesson-title');
  const bodyEl = document.getElementById('pocket-lesson-body');
  const counterEl = document.getElementById('pocket-lesson-counter');
  const nextBtn = document.getElementById('pocket-lesson-next');
  const catPillEl = document.getElementById('pocket-lesson-cat-pill');
  const filtersEl = document.getElementById('pocket-lesson-filters');
  if (!btn || !wrap || !Array.isArray(POCKET_LESSONS) || POCKET_LESSONS.length === 0) return;

  // Active category filter ('*' means all). Persisted so the user's choice
  // survives reloads alongside the visibility state.
  let activeCategory = '*';
  try {{ activeCategory = localStorage.getItem(CAT_KEY) || '*'; }} catch (e) {{}}
  // Track currentIdx so Next can avoid showing the same tip twice in a row.
  let currentIdx = -1;

  function eligibleIndices() {{
    if (activeCategory === '*') return POCKET_LESSONS.map((_, i) => i);
    const out = [];
    for (let i = 0; i < POCKET_LESSONS.length; i++) {{
      if (POCKET_LESSONS[i].category === activeCategory) out.push(i);
    }}
    return out.length ? out : POCKET_LESSONS.map((_, i) => i);   // fallback: all
  }}
  function pickRandomTip() {{
    const pool = eligibleIndices();
    if (pool.length === 1) return pool[0];
    let idx;
    do {{ idx = pool[Math.floor(Math.random() * pool.length)]; }}
    while (idx === currentIdx);
    return idx;
  }}
  function renderTip(idx) {{
    const tip = POCKET_LESSONS[idx];
    if (!tip) return;
    currentIdx = idx;
    titleEl.textContent = tip.title || '';
    bodyEl.textContent = tip.body || '';
    catPillEl.textContent = tip.category || '';
    const pool = eligibleIndices();
    const posInPool = pool.indexOf(idx) + 1;
    counterEl.textContent = activeCategory === '*'
      ? `Tip ${{idx + 1}} of ${{POCKET_LESSONS.length}}`
      : `${{posInPool}} of ${{pool.length}} in ${{activeCategory}}`;
  }}

  // Build the filter chips dynamically from the categories actually present.
  function buildChips() {{
    const cats = new Set();
    for (const l of POCKET_LESSONS) if (l.category) cats.add(l.category);
    // Order: All first, then the rest alphabetically -- stable across rebuilds.
    const ordered = ['*'].concat(Array.from(cats).sort());
    filtersEl.innerHTML = ordered.map(c => {{
      const label = c === '*' ? `All ${{POCKET_LESSONS.length}}` : c;
      const cls = c === activeCategory ? 'pocket-lesson-chip active' : 'pocket-lesson-chip';
      return `<button type="button" class="${{cls}}" data-cat="${{c}}">${{label}}</button>`;
    }}).join('');
    filtersEl.querySelectorAll('.pocket-lesson-chip').forEach(chip => {{
      chip.addEventListener('click', () => {{
        activeCategory = chip.dataset.cat;
        try {{ localStorage.setItem(CAT_KEY, activeCategory); }} catch (e) {{}}
        filtersEl.querySelectorAll('.pocket-lesson-chip').forEach(c =>
          c.classList.toggle('active', c.dataset.cat === activeCategory));
        renderTip(pickRandomTip());
      }});
    }});
  }}
  buildChips();

  function setEnabled(on, opts) {{
    opts = opts || {{}};
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    wrap.classList.toggle('is-open', on);
    wrap.setAttribute('aria-hidden', on ? 'false' : 'true');
    if (on && currentIdx < 0) renderTip(pickRandomTip());
    if (!opts.silent) {{
      try {{ localStorage.setItem(STORAGE_KEY, on ? '1' : '0'); }} catch (e) {{}}
    }}
  }}

  let initial = false;
  try {{
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved === '1') initial = true;
  }} catch (e) {{}}
  setEnabled(initial, {{silent: true}});

  btn.addEventListener('click', () => setEnabled(btn.getAttribute('aria-pressed') !== 'true'));
  nextBtn.addEventListener('click', () => renderTip(pickRandomTip()));
}})();

// v2.1: palette toggle collapsed from 4 buttons into 1 cycling button. Each
// click advances through the ORDER list; label updates to the current palette
// name. Persistence key + body-class scheme unchanged from v1.x for backwards
// compat -- localStorage values "default" / "softdark" / "light" / "bloomberg"
// still apply correctly.
(function setupPalette() {{
  const PALETTE_KEY = 'stocks-dashboard-palette';
  const ORDER = ['default', 'softdark', 'light', 'bloomberg'];
  const LABELS = {{default: 'Default', softdark: 'Soft Dark', light: 'Light', bloomberg: 'Amber'}};
  const btn = document.getElementById('palette-cycle-btn');
  if (!btn) return;
  function apply(name) {{
    document.body.classList.remove('palette-softdark','palette-light','palette-bloomberg');
    if (name && name !== 'default') document.body.classList.add('palette-' + name);
    // v2.1: button is icon-only; the human-readable name now lives in the
    // data-tooltip attribute (which drives the CSS hover tooltip).
    btn.dataset.tooltip = LABELS[name] || 'Default';
    btn.dataset.palette = name;
    try {{ localStorage.setItem(PALETTE_KEY, name); }} catch (e) {{ /* private mode */ }}
  }}
  const saved = (() => {{ try {{ return localStorage.getItem(PALETTE_KEY); }} catch (e) {{ return null; }} }})();
  apply(ORDER.includes(saved) ? saved : 'default');
  btn.addEventListener('click', () => {{
    const current = btn.dataset.palette || 'default';
    const next = ORDER[(ORDER.indexOf(current) + 1) % ORDER.length];
    apply(next);
  }});
}})();

// v2.1 Quiz feature. Opens via topbar Quiz button -> presents one question
// at a time from QUIZ_POOL, with a 3-option choice. Picks the next question
// uniformly at random from the "unseen" pool; recycles the pool when 90%+
// has been seen. Monthly score (answered + correct) resets on each new
// calendar month, tracked via the "month" key in quizMonthly.
//
// localStorage schema:
//   "quizSeen"    : JSON array of seen question ids
//   "quizMonthly" : JSON {{month: "YYYY-MM", answered: N, correct: M}}
//
// UX: correct answer gets a green flash + the explanation reveals; wrong
// answer turns the picked button red + reveals correct in green + explanation.
// "Next" button enables once the user has picked an answer.
(function setupQuiz() {{
  const openBtn = document.getElementById('quiz-btn');
  const modal = document.getElementById('quiz-modal');
  if (!openBtn || !modal || !Array.isArray(QUIZ_POOL) || QUIZ_POOL.length === 0) return;
  const closeBtn = document.getElementById('quiz-modal-close');
  const catPill = document.getElementById('quiz-cat-pill');
  const qEl = document.getElementById('quiz-question');
  const optsEl = document.getElementById('quiz-options');
  const revealEl = document.getElementById('quiz-reveal');
  const verdictEl = document.getElementById('quiz-reveal-verdict');
  const explainEl = document.getElementById('quiz-reveal-text');
  const scoreEl = document.getElementById('quiz-score-num');
  const nextBtn = document.getElementById('quiz-next');

  const SEEN_KEY = 'quizSeen';
  const MONTHLY_KEY = 'quizMonthly';

  // ---- State engine (load / save / pick / record) -----------------------
  function nowMonth() {{
    const d = new Date();
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0');
  }}
  function loadState() {{
    let seen = [], monthly = {{month: nowMonth(), answered: 0, correct: 0}};
    try {{
      const s = JSON.parse(localStorage.getItem(SEEN_KEY) || '[]');
      if (Array.isArray(s)) seen = s.filter(x => typeof x === 'number');
      const m = JSON.parse(localStorage.getItem(MONTHLY_KEY) || 'null');
      if (m && typeof m === 'object' && typeof m.month === 'string') {{
        monthly = {{month: m.month, answered: m.answered|0, correct: m.correct|0}};
      }}
    }} catch (e) {{ /* private mode or corrupted entries */ }}
    // Monthly auto-reset on calendar month flip
    if (monthly.month !== nowMonth()) {{
      monthly = {{month: nowMonth(), answered: 0, correct: 0}};
    }}
    return {{seen, monthly}};
  }}
  function saveState(state) {{
    try {{
      localStorage.setItem(SEEN_KEY, JSON.stringify(state.seen));
      localStorage.setItem(MONTHLY_KEY, JSON.stringify(state.monthly));
    }} catch (e) {{ /* private mode -- silently degrade */ }}
  }}
  let lastShownId = null;
  function pickNext(state) {{
    // Recycle seen-set when 90%+ has been seen so the experience never dead-ends.
    if (state.seen.length >= Math.floor(QUIZ_POOL.length * 0.9)) {{
      state.seen = [];
    }}
    const unseen = QUIZ_POOL.filter(q => !state.seen.includes(q.id));
    let pool = unseen.length > 0 ? unseen : QUIZ_POOL;
    // Never show the same question twice in a row (across opens + Next clicks).
    // `seen` only grows on ANSWER, so without this an open-without-answering
    // re-rolls the same pool and can repeat the question you just saw.
    if (pool.length > 1 && lastShownId !== null) {{
      const filtered = pool.filter(q => q.id !== lastShownId);
      if (filtered.length > 0) pool = filtered;
    }}
    const q = pool[Math.floor(Math.random() * pool.length)];
    lastShownId = q.id;
    return q;
  }}

  let state = loadState();
  let currentQ = null;
  let answered = false;

  // ---- Rendering --------------------------------------------------------
  function updateScore(animate) {{
    scoreEl.textContent = state.monthly.correct + '/' + state.monthly.answered;
    if (animate) {{
      scoreEl.classList.remove('pop');
      // Force reflow so the keyframe restarts. Tiny perf cost; fires once per answer.
      void scoreEl.offsetWidth;
      scoreEl.classList.add('pop');
    }}
  }}
  function renderQuestion(q) {{
    currentQ = q;
    answered = false;
    catPill.textContent = q.category;
    qEl.textContent = q.question;
    revealEl.hidden = true;
    nextBtn.disabled = true;
    optsEl.innerHTML = '';
    q.options.forEach((opt, idx) => {{
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'quiz-option';
      b.textContent = opt;
      b.setAttribute('role', 'radio');
      b.addEventListener('click', () => handleAnswer(idx));
      optsEl.appendChild(b);
    }});
  }}
  function handleAnswer(picked) {{
    if (answered) return;
    answered = true;
    const correct = currentQ.correct;
    const isCorrect = picked === correct;
    // Disable all + mark states
    Array.from(optsEl.children).forEach((btn, idx) => {{
      btn.disabled = true;
      if (idx === correct) {{
        btn.classList.add('correct');
      }} else if (idx === picked) {{
        btn.classList.add('incorrect');
      }} else {{
        btn.classList.add('dimmed');
      }}
    }});
    // Reveal explanation
    verdictEl.textContent = isCorrect ? 'Correct' : 'Not quite';
    verdictEl.className = 'quiz-reveal-verdict ' + (isCorrect ? 'pos' : 'neg');
    explainEl.textContent = currentQ.explanation;
    revealEl.hidden = false;
    nextBtn.disabled = false;
    // Persist state
    if (!state.seen.includes(currentQ.id)) state.seen.push(currentQ.id);
    state.monthly.answered++;
    if (isCorrect) state.monthly.correct++;
    saveState(state);
    updateScore(isCorrect);
  }}
  function openQuiz() {{
    state = loadState();   // re-read in case another tab updated
    updateScore(false);
    renderQuestion(pickNext(state));
    modal.removeAttribute('hidden');
    document.body.classList.add('modal-open');
  }}
  function closeQuiz() {{
    modal.setAttribute('hidden', '');
    document.body.classList.remove('modal-open');
  }}

  // ---- Wiring ----------------------------------------------------------
  openBtn.addEventListener('click', openQuiz);
  closeBtn.addEventListener('click', closeQuiz);
  nextBtn.addEventListener('click', () => renderQuestion(pickNext(state)));
  // Backdrop click closes (the modal's .modal pseudo-element acts as backdrop)
  modal.addEventListener('click', (e) => {{ if (e.target === modal) closeQuiz(); }});
  // ESC closes (only when the quiz modal is the topmost modal)
  document.addEventListener('keydown', (e) => {{
    if (e.key === 'Escape' && !modal.hasAttribute('hidden')) closeQuiz();
  }});
}})();

// ---- Customizable module layout ---------------------------------------
// Sections below the hero are wrapped as draggable/hideable "modules".
// Order + hidden state persist in localStorage so each visitor's layout
// survives rebuilds (build.py only ships the default order). A new section
// the author adds later slots into its default position automatically, so
// people who cloned + customized never silently lose newly-shipped sections.
(function setupLastLook() {{
  const KEY = 'stocks-dashboard-lastlook', DKEY = KEY + '-dismissed';
  const el = document.getElementById('last-look');
  if (!el || typeof LAST_LOOK === 'undefined' || !LAST_LOOK) return;
  let prev = null;
  try {{ prev = JSON.parse(localStorage.getItem(KEY) || 'null'); }} catch (e) {{}}
  const store = () => {{ try {{ localStorage.setItem(KEY, JSON.stringify(LAST_LOOK)); }} catch (e) {{}} }};
  if (!prev) {{ store(); return; }}                       // first visit
  if (prev.build_id === LAST_LOOK.build_id) return;       // already saw this build
  let dismissed = null;
  try {{ dismissed = localStorage.getItem(DKEY); }} catch (e) {{}}
  if (dismissed === String(LAST_LOOK.build_id)) {{ store(); return; }}
  const items = [];
  const dB = LAST_LOOK.basket_return - (prev.basket_return || 0);
  if (Math.abs(dB) >= 0.1) items.push(`basket <b>${{dB >= 0 ? '+' : ''}}${{dB.toFixed(1)}}pp</b>`);
  const prevIdeas = prev.idea_tickers || [];
  const newIdeas = (LAST_LOOK.idea_tickers || []).filter(t => !prevIdeas.includes(t));
  if (newIdeas.length) items.push(`<b>${{newIdeas.length}}</b> new idea${{newIdeas.length > 1 ? 's' : ''}}: ${{newIdeas.join(', ')}}`);
  const prevValue = prev.value_tickers || [];
  const newValue = (LAST_LOOK.value_tickers || []).filter(t => !prevValue.includes(t));
  if (newValue.length) items.push(`<b>${{newValue.length}}</b> new value pick${{newValue.length > 1 ? 's' : ''}}: ${{newValue.join(', ')}}`);
  const pm = [], pp = LAST_LOOK.predictions || {{}}, ppp = prev.predictions || {{}};
  for (const k in pp) {{ if (k in ppp) {{ const d = pp[k] - ppp[k]; if (Math.abs(d) >= 3) pm.push(`${{k}} ${{d >= 0 ? '+' : ''}}${{d.toFixed(0)}}pp`); }} }}
  if (pm.length) items.push(pm.join(' &middot; '));
  store();
  if (!items.length) return;
  el.innerHTML = '<span class="ll-tag">Since your last visit</span>'
    + '<span class="ll-body">' + items.join(' &middot; ') + '</span>'
    + '<button class="ll-x" aria-label="Dismiss">&times;</button>';
  el.hidden = false;
  el.querySelector('.ll-x').addEventListener('click', () => {{
    el.hidden = true;
    try {{ localStorage.setItem(DKEY, String(LAST_LOOK.build_id)); }} catch (e) {{}}
  }});
}})();

(function setupLayout() {{
  const KEY = 'stocks-dashboard-layout-v1';
  const stack = document.getElementById('module-stack');
  if (!stack) return;
  const editBtn = document.getElementById('edit-layout-btn');
  const resetBtn = document.getElementById('reset-layout-btn');
  const defaultOrder = (stack.dataset.defaultOrder || '').split(',').filter(Boolean);

  const mods = () => Array.from(stack.querySelectorAll(':scope > .module'));
  const modById = (id) => stack.querySelector(':scope > .module[data-module="' + id + '"]');

  // Modules that should pair side-by-side when they're adjacent and both
  // visible. Re-derived from the live DOM after every layout change so the
  // pairing is a pure function of the current order + hidden state — no
  // separate persistence needed.
  const PAIR_MEMBERS = ['outlook', 'news'];
  function applyPairing() {{
    stack.querySelectorAll('.module-paired').forEach(el => el.classList.remove('module-paired'));
    const a = modById(PAIR_MEMBERS[0]);
    const b = modById(PAIR_MEMBERS[1]);
    if (!a || !b) return;
    if (a.dataset.hidden === 'true' || b.dataset.hidden === 'true') return;
    const children = Array.from(stack.children);
    const aIdx = children.indexOf(a);
    const bIdx = children.indexOf(b);
    if (aIdx < 0 || bIdx < 0) return;
    if (Math.abs(aIdx - bIdx) === 1) {{
      a.classList.add('module-paired');
      b.classList.add('module-paired');
    }}
  }}

  function load() {{
    try {{
      const s = JSON.parse(localStorage.getItem(KEY) || 'null');
      if (!s || !Array.isArray(s.order)) return null;
      return {{ order: s.order, hidden: Array.isArray(s.hidden) ? s.hidden : [] }};
    }} catch (e) {{ return null; }}
  }}
  function save() {{
    const order = mods().map(el => el.dataset.module);
    const hidden = mods().filter(el => el.dataset.hidden === 'true').map(el => el.dataset.module);
    try {{ localStorage.setItem(KEY, JSON.stringify({{ order, hidden }})); }} catch (e) {{}}
  }}

  function applyOrder(savedOrder) {{
    const present = new Set(mods().map(el => el.dataset.module));
    const result = savedOrder.filter(id => present.has(id));
    const placed = new Set(result);
    // Slot any module missing from the saved order (newly shipped) into the
    // position it occupies in the default order.
    defaultOrder.forEach(id => {{
      if (!present.has(id) || placed.has(id)) return;
      const di = defaultOrder.indexOf(id);
      let after = null;
      for (let i = di - 1; i >= 0; i--) {{
        if (placed.has(defaultOrder[i])) {{ after = defaultOrder[i]; break; }}
      }}
      if (after === null) result.unshift(id);
      else result.splice(result.indexOf(after) + 1, 0, id);
      placed.add(id);
    }});
    result.forEach(id => {{ const el = modById(id); if (el) stack.appendChild(el); }});
  }}
  function applyHidden(hiddenArr) {{
    const h = new Set(hiddenArr);
    mods().forEach(el => {{
      const hide = h.has(el.dataset.module);
      el.dataset.hidden = hide ? 'true' : 'false';
      const cb = el.querySelector('.module-vis-cb');
      if (cb) cb.checked = !hide;
      const txt = el.querySelector('.module-vis-txt');
      if (txt) txt.textContent = hide ? 'Hidden' : 'Shown';
    }});
  }}

  const state = load();
  if (state) {{ applyOrder(state.order); applyHidden(state.hidden); }}
  else {{ applyHidden([]); }}
  applyPairing();

  let sortable = null;
  function enterEdit() {{
    document.body.classList.add('edit-mode');
    // v2.1 icon-button fix: update the data-tooltip (which the CSS hover
    // tooltip reads) instead of textContent (which would wipe the SVG icon).
    if (editBtn) {{ editBtn.dataset.tooltip = 'Done editing'; editBtn.classList.add('active'); editBtn.setAttribute('aria-pressed', 'true'); }}
    if (resetBtn) resetBtn.hidden = false;
    if (window.Sortable && !sortable) {{
      sortable = window.Sortable.create(stack, {{
        handle: '.module-grip', draggable: '.module', animation: 150,
        ghostClass: 'module-ghost', chosenClass: 'module-chosen',
        onEnd: () => {{ save(); applyPairing(); }},
      }});
    }}
  }}
  function exitEdit() {{
    document.body.classList.remove('edit-mode');
    if (editBtn) {{ editBtn.dataset.tooltip = 'Edit layout'; editBtn.classList.remove('active'); editBtn.setAttribute('aria-pressed', 'false'); }}
    if (resetBtn) resetBtn.hidden = true;
    if (sortable) {{ sortable.destroy(); sortable = null; }}
  }}
  if (editBtn) editBtn.addEventListener('click', () => {{
    if (document.body.classList.contains('edit-mode')) exitEdit(); else enterEdit();
  }});

  // One-time discovery hint: pulse the edit button + show a tooltip on the
  // first ever page load. Gated on a localStorage flag so returning visitors
  // never see it again. Auto-dismisses after 8s OR on any click anywhere.
  //
  // Tooltip position is computed from the button's actual viewport rect so
  // the arrow lines up regardless of topbar contents -- a prior version
  // hardcoded `right:24px` which (incorrectly) ended up pointing at the
  // palette buttons because the topbar is left-aligned within its container.
  (function maybeShowDiscoveryHint() {{
    if (!editBtn) return;
    const DISCOVERED_KEY = 'edit-layout-discovered';
    try {{ if (localStorage.getItem(DISCOVERED_KEY)) return; }}
    catch (e) {{ return; }}  // privacy mode: skip rather than crash
    const tip = document.getElementById('edit-tooltip');
    editBtn.classList.add('pulse');
    if (tip) {{
      // Anchor the tooltip's top-left ~8px below + aligned with the button's
      // left edge. The CSS arrow sits at left:18px, so it points up at the
      // button. Slight left-shift (10px) puts the arrow under the button's
      // center rather than its leftmost pixel for a softer visual anchor.
      const r = editBtn.getBoundingClientRect();
      tip.style.top  = (r.bottom + 8) + 'px';
      tip.style.left = Math.max(8, r.left - 10) + 'px';
      tip.style.right = 'auto';
      tip.hidden = false;
    }}
    let dismissed = false;
    function dismiss() {{
      if (dismissed) return;
      dismissed = true;
      editBtn.classList.remove('pulse');
      if (tip) tip.hidden = true;
      try {{ localStorage.setItem(DISCOVERED_KEY, '1'); }} catch (e) {{}}
    }}
    // Dismiss triggers: clicking anywhere, clicking the tooltip itself,
    // or 8s timeout (whichever comes first).
    document.addEventListener('click', dismiss, {{ once: true, capture: true }});
    if (tip) tip.addEventListener('click', dismiss, {{ once: true }});
    setTimeout(dismiss, 8000);
  }})();

  stack.addEventListener('change', (e) => {{
    const cb = e.target.closest && e.target.closest('.module-vis-cb');
    if (!cb) return;
    const mod = cb.closest('.module');
    if (!mod) return;
    const hide = !cb.checked;
    mod.dataset.hidden = hide ? 'true' : 'false';
    const txt = mod.querySelector('.module-vis-txt');
    if (txt) txt.textContent = hide ? 'Hidden' : 'Shown';
    save();
    applyPairing();
  }});

  if (resetBtn) resetBtn.addEventListener('click', () => {{
    try {{ localStorage.removeItem(KEY); }} catch (e) {{}}
    defaultOrder.forEach(id => {{ const el = modById(id); if (el) stack.appendChild(el); }});
    applyHidden([]);
    applyPairing();
  }});
}})();

// T8: Hero stats picker. Independent of setupLayout to keep responsibilities
// isolated (modules vs stats can be edited / reset / persisted independently).
// localStorage schema: {{"selected": ["slug1", "slug2", ...]}} -- an ordered
// array of currently-visible stat slugs. Anything not in `selected` is hidden
// outside edit-mode; in edit-mode all 10 stats are visible (greyed if hidden)
// so the user can toggle them on. Sortable drag in edit-mode reorders.
(function setupStats() {{
  const KEY = 'stocks-dashboard-stats-v1';
  const grid = document.getElementById('stats-grid');
  if (!grid) return;
  const defaultSelected = (grid.dataset.statsDefault || '').split(',').filter(Boolean);
  const allSlugs = (grid.dataset.statsAll || '').split(',').filter(Boolean);

  const cards = () => Array.from(grid.querySelectorAll(':scope > .stat'));

  function load() {{
    try {{
      const raw = localStorage.getItem(KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      if (parsed && Array.isArray(parsed.selected)) {{
        // Drop unknown slugs (forward-compat for future stats added/removed).
        return parsed.selected.filter(s => allSlugs.includes(s));
      }}
    }} catch (e) {{}}
    return null;
  }}
  function save(selected) {{
    try {{ localStorage.setItem(KEY, JSON.stringify({{ selected: selected }})); }}
    catch (e) {{}}
  }}
  function apply(selected) {{
    const orderMap = {{}};
    selected.forEach((s, i) => {{ orderMap[s] = i; }});
    cards().forEach(card => {{
      const slug = card.dataset.stat;
      const isShown = selected.includes(slug);
      card.dataset.statHidden = isShown ? 'false' : 'true';
      // CSS `order` reorders without touching DOM, plays nice with Sortable.
      card.style.order = isShown ? String(orderMap[slug]) : '99';
      const cb = card.querySelector('.stat-vis-cb');
      if (cb) cb.checked = isShown;
      const txt = card.querySelector('.stat-vis-txt');
      if (txt) txt.textContent = isShown ? 'Shown' : 'Hidden';
    }});
  }}

  apply(load() || defaultSelected);

  grid.addEventListener('change', (e) => {{
    const cb = e.target.closest && e.target.closest('.stat-vis-cb');
    if (!cb) return;
    const card = cb.closest('.stat');
    if (!card) return;
    const slug = card.dataset.stat;
    const cur = load() || defaultSelected.slice();
    const next = cb.checked
      ? (cur.includes(slug) ? cur : cur.concat([slug]))
      : cur.filter(s => s !== slug);
    apply(next);
    save(next);
  }});

  // Drag-to-reorder available in edit-mode only. SortableJS reads visual
  // order via card position; we then derive `selected` from the new order.
  let sortable = null;
  function attachSortable() {{
    if (!window.Sortable || sortable) return;
    sortable = window.Sortable.create(grid, {{
      handle: '.stat-grip', draggable: '.stat', animation: 150,
      ghostClass: 'stat-ghost', chosenClass: 'stat-chosen',
      onEnd: () => {{
        // After drag, DOM order reflects user intent. Re-derive selected list
        // from the new visual order, keeping only currently-shown cards.
        const order = cards()
          .filter(c => c.dataset.statHidden !== 'true')
          .map(c => c.dataset.stat);
        save(order);
        apply(order);
      }},
    }});
  }}
  function detachSortable() {{
    if (sortable) {{ sortable.destroy(); sortable = null; }}
  }}
  // Observe body class changes (edit-mode toggle) instead of hooking the
  // edit button click directly -- avoids ordering coupling with setupLayout.
  const onEditChange = () => {{
    if (document.body.classList.contains('edit-mode')) attachSortable();
    else detachSortable();
  }};
  new MutationObserver(onEditChange).observe(document.body,
    {{ attributes: true, attributeFilter: ['class'] }});
  onEditChange();

  // Reset button also clears stats state (one button, both layers).
  const resetBtn = document.getElementById('reset-layout-btn');
  if (resetBtn) {{
    resetBtn.addEventListener('click', () => {{
      try {{ localStorage.removeItem(KEY); }} catch (e) {{}}
      apply(defaultSelected);
    }});
  }}
}})();

// T7 cosmetic: alpha sparkline hover. Shows date + value of the nearest
// point in the header pill; vertical crosshair + dot mark the position.
// Defensive about missing element so this is a no-op when the sparkline
// itself was suppressed (less than 5 weeks of data).
(function setupAlphaHover() {{
  const wrap = document.getElementById('alpha-sparkline-wrap');
  const svg  = document.getElementById('alpha-sparkline-svg');
  const latestEl = document.getElementById('alpha-sparkline-latest');
  if (!wrap || !svg || !latestEl) return;
  const dates  = (svg.dataset.dates  || '').split(',').filter(Boolean);
  const values = (svg.dataset.values || '').split(',').map(parseFloat);
  if (!dates.length || dates.length !== values.length) return;
  const cross = svg.querySelector('.alpha-cross');
  const dot   = svg.querySelector('.alpha-dot');
  const vb = svg.viewBox.baseVal;   // {{x, y, width, height}}
  const padX = 0, padY = 4;
  const vmin = Math.min(...values), vmax = Math.max(...values);
  const vrange = Math.max(vmax - vmin, 1e-9);
  const n = values.length;
  // v2.7 #6: map x by DATE over the shared [domainStart,domainEnd] span (same as
  // the build side) so hover crosshairs land on the calendar-correct point.
  const domStart = Date.parse((svg.dataset.domainStart || '') + 'T00:00:00');
  const domEnd   = Date.parse((svg.dataset.domainEnd   || '') + 'T00:00:00');
  const useDomain = !Number.isNaN(domStart) && !Number.isNaN(domEnd) && domEnd > domStart;
  const domSpan = Math.max(domEnd - domStart, 1);
  const dateMs = dates.map(d => Date.parse(d + 'T00:00:00'));
  const xAt = (i) => {{
    if (!useDomain) return padX + i / Math.max(n - 1, 1) * (vb.width - 2 * padX);
    let f = (dateMs[i] - domStart) / domSpan; f = f < 0 ? 0 : (f > 1 ? 1 : f);
    return padX + f * (vb.width - 2 * padX);
  }};
  const yAt = (v) => padY + (vmax - v) / vrange * (vb.height - 2 * padY);
  const defaultText = latestEl.dataset.defaultText;
  const defaultCls = latestEl.classList.contains('neg') ? 'neg' : 'pos';

  function relMonth(dateStr) {{
    // Compact date for the head pill, e.g. "12 Mar 25".
    const d = new Date(dateStr + 'T00:00:00');
    if (isNaN(d.getTime())) return dateStr;
    const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    return `${{d.getDate()}} ${{months[d.getMonth()]}} ${{String(d.getFullYear()).slice(-2)}}`;
  }}

  svg.addEventListener('mousemove', (e) => {{
    const rect = svg.getBoundingClientRect();
    const vbX = (e.clientX - rect.left) / rect.width * vb.width;
    // Nearest index in the dataset
    let best = 0, bestDist = Infinity;
    for (let i = 0; i < n; i++) {{
      const d = Math.abs(xAt(i) - vbX);
      if (d < bestDist) {{ bestDist = d; best = i; }}
    }}
    const v = values[best];
    cross.setAttribute('x1', xAt(best));
    cross.setAttribute('x2', xAt(best));
    cross.setAttribute('opacity', '0.6');
    dot.setAttribute('cx', xAt(best));
    dot.setAttribute('cy', yAt(v));
    dot.setAttribute('opacity', '1');
    latestEl.textContent = `${{relMonth(dates[best])}} · ${{v >= 0 ? '+' : ''}}${{v.toFixed(1)}} pp`;
    latestEl.classList.remove('pos', 'neg');
    latestEl.classList.add(v >= 0 ? 'pos' : 'neg');
  }});

  svg.addEventListener('mouseleave', () => {{
    cross.setAttribute('opacity', '0');
    dot.setAttribute('opacity', '0');
    latestEl.textContent = defaultText;
    latestEl.classList.remove('pos', 'neg');
    latestEl.classList.add(defaultCls);
  }});
}})();

// v1.9 #1: Drawdown sparkline hover. Mirrors setupAlphaHover() but drawdown
// values are bounded [min_dd, 0] (never above peak), so the head pill renders
// a plain "-X.X%" with no sign-flip styling -- always var(--down).
(function setupDrawdownHover() {{
  const wrap = document.getElementById('dd-sparkline-wrap');
  const svg  = document.getElementById('dd-sparkline-svg');
  const latestEl = document.getElementById('dd-sparkline-latest');
  if (!wrap || !svg || !latestEl) return;
  const dates  = (svg.dataset.dates  || '').split(',').filter(Boolean);
  const values = (svg.dataset.values || '').split(',').map(parseFloat);
  if (!dates.length || dates.length !== values.length) return;
  const cross = svg.querySelector('.dd-cross');
  const dot   = svg.querySelector('.dd-dot');
  const vb = svg.viewBox.baseVal;
  const padX = 0, padY = 3;
  // Match the build-side _dx / _dy mapping (DH-2*padY active height, value
  // floor = min(values + [0])). Important: values are <= 0, so y=padY is the
  // 0% top of the chart and y=DH-padY is the worst drawdown.
  const ddMin = Math.min(...values, 0);
  const ddRange = Math.max(Math.abs(ddMin), 1e-9);
  const n = values.length;
  // v2.7 #6: date-domain x mapping (mirror of the alpha sparkline + build side).
  const domStart = Date.parse((svg.dataset.domainStart || '') + 'T00:00:00');
  const domEnd   = Date.parse((svg.dataset.domainEnd   || '') + 'T00:00:00');
  const useDomain = !Number.isNaN(domStart) && !Number.isNaN(domEnd) && domEnd > domStart;
  const domSpan = Math.max(domEnd - domStart, 1);
  const dateMs = dates.map(d => Date.parse(d + 'T00:00:00'));
  const xAt = (i) => {{
    if (!useDomain) return padX + i / Math.max(n - 1, 1) * (vb.width - 2 * padX);
    let f = (dateMs[i] - domStart) / domSpan; f = f < 0 ? 0 : (f > 1 ? 1 : f);
    return padX + f * (vb.width - 2 * padX);
  }};
  const yAt = (v) => padY + (-v) / ddRange * (vb.height - 2 * padY);
  const defaultText = latestEl.dataset.defaultText;

  function relMonth(dateStr) {{
    const d = new Date(dateStr + 'T00:00:00');
    if (isNaN(d.getTime())) return dateStr;
    const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    return `${{d.getDate()}} ${{months[d.getMonth()]}} ${{String(d.getFullYear()).slice(-2)}}`;
  }}

  svg.addEventListener('mousemove', (e) => {{
    const rect = svg.getBoundingClientRect();
    const vbX = (e.clientX - rect.left) / rect.width * vb.width;
    let best = 0, bestDist = Infinity;
    for (let i = 0; i < n; i++) {{
      const d = Math.abs(xAt(i) - vbX);
      if (d < bestDist) {{ bestDist = d; best = i; }}
    }}
    const v = values[best];
    cross.setAttribute('x1', xAt(best));
    cross.setAttribute('x2', xAt(best));
    cross.setAttribute('opacity', '0.6');
    dot.setAttribute('cx', xAt(best));
    dot.setAttribute('cy', yAt(v));
    dot.setAttribute('opacity', '1');
    latestEl.textContent = `${{relMonth(dates[best])}} · ${{v.toFixed(1)}}%`;
  }});

  svg.addEventListener('mouseleave', () => {{
    cross.setAttribute('opacity', '0');
    dot.setAttribute('opacity', '0');
    latestEl.textContent = defaultText;
  }});
}})();

// ---- Live news refresh via Cloudflare Worker --------------------------
// The static news box is rendered server-side at build time as a fallback.
// If NEWS_WORKER_URL is set, fetch fresh items on page load and swap them in.
// On any failure (Worker down, network blip, CORS issue), the static fallback
// stays untouched — silent graceful degradation.
const NEWS_WORKER_URL = {news_worker_url_js};

async function refreshNewsFromWorker() {{
  if (!NEWS_WORKER_URL) return;
  try {{
    const resp = await fetch(NEWS_WORKER_URL, {{cache: 'no-cache'}});
    if (!resp.ok) return;
    const data = await resp.json();
    if (!data || !Array.isArray(data.items) || data.items.length === 0) return;
    renderLiveNews(data.items, data.fetched_at);
  }} catch (e) {{ /* keep fallback */ }}
}}

function renderLiveNews(items, fetchedAt) {{
  const list = document.querySelector('.news-list');
  if (!list) return;
  list.innerHTML = items.map(it => {{
    const when = relativeNewsTime(new Date(it.published));
    return `<a class="news-row" data-source="${{escapeNewsHtml(it.source)}}" href="${{safeUrl(it.link)}}" target="_blank" rel="noopener noreferrer">`
      + `<div class="news-title">${{escapeNewsHtml(it.title)}}</div>`
      + `<div class="news-meta"><span class="news-src">${{escapeNewsHtml(it.source)}}</span>`
      + `<span class="news-dot">·</span><span class="news-when">${{escapeNewsHtml(when)}}</span></div>`
      + `</a>`;
  }}).join('');
  // Rebuild source chips from the live data — sources can change as feeds
  // are tuned on the Worker side, so don't trust the static fallback's list.
  const sources = [...new Set(items.map(it => it.source))];
  rebuildNewsChips(sources);
  const stale = document.querySelector('.news-stale');
  if (stale && fetchedAt) {{
    const t = new Date(fetchedAt);
    const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    const pad = n => String(n).padStart(2,'0');
    stale.textContent = `live · ${{pad(t.getUTCDate())}} ${{months[t.getUTCMonth()]}} ${{pad(t.getUTCHours())}}:${{pad(t.getUTCMinutes())}} UTC`;
    stale.classList.add('news-live');
  }}
}}

function rebuildNewsChips(sources) {{
  const chipBar = document.querySelector('.news-chips');
  if (!chipBar) return;
  const saved = (() => {{ try {{ return localStorage.getItem('stocks-dashboard-news-source'); }} catch (e) {{ return null; }} }})();
  const current = saved || '*';
  const html = ['<button class="news-chip" data-src="*">All</button>']
    .concat(sources.map(s => `<button class="news-chip" data-src="${{escapeNewsHtml(s)}}">${{escapeNewsHtml(s)}}</button>`))
    .join('');
  chipBar.innerHTML = html;
  applyNewsFilter(current);
  chipBar.querySelectorAll('.news-chip').forEach(btn => {{
    btn.addEventListener('click', () => applyNewsFilter(btn.dataset.src));
  }});
}}

function applyNewsFilter(src) {{
  const chipBar = document.querySelector('.news-chips');
  if (!chipBar) return;
  const rows = [...document.querySelectorAll('.news-row')];
  // Resilience: a saved/selected source that matches NO current rows (stale
  // localStorage, or the live worker's feed set changed) would hide every row
  // and leave the panel empty. Fall back to "All" so news never renders blank.
  if (src !== '*' && !rows.some(r => r.dataset.source === src)) src = '*';
  chipBar.querySelectorAll('.news-chip').forEach(b => {{
    b.classList.toggle('active', b.dataset.src === src);
  }});
  rows.forEach(row => {{
    const match = src === '*' || row.dataset.source === src;
    if (match) row.removeAttribute('hidden'); else row.setAttribute('hidden', '');
  }});
  try {{ localStorage.setItem('stocks-dashboard-news-source', src); }} catch (e) {{ /* ignore */ }}
}}

// Wire the chip cluster on the static fallback so it works before the Worker
// fetch (or if the Worker is unreachable). renderLiveNews() rebuilds it later.
document.querySelectorAll('.news-chips .news-chip').forEach(btn => {{
  btn.addEventListener('click', () => applyNewsFilter(btn.dataset.src));
}});
// Apply any saved filter to the static content on initial paint.
(function applySavedNewsFilter() {{
  let saved = null;
  try {{ saved = localStorage.getItem('stocks-dashboard-news-source'); }} catch (e) {{}}
  if (saved && saved !== '*') applyNewsFilter(saved);
}})();

function relativeNewsTime(date) {{
  const secs = Math.floor((Date.now() - date.getTime()) / 1000);
  if (secs < 60) return 'just now';
  if (secs < 3600) return `${{Math.floor(secs / 60)}}m ago`;
  if (secs < 86400) return `${{Math.floor(secs / 3600)}}h ago`;
  const days = Math.floor(secs / 86400);
  if (days < 7) return `${{days}}d ago`;
  return date.toLocaleDateString('en-GB', {{day: '2-digit', month: 'short'}});
}}

function escapeNewsHtml(s) {{
  const d = document.createElement('div');
  d.textContent = String(s == null ? '' : s);
  return d.innerHTML;
}}

// Only allow http(s) hrefs from untrusted feed/worker links — a javascript:/data:
// URL has no <>&" to entity-escape, so escaping alone leaves it clickable.
function safeUrl(u) {{
  const s = String(u == null ? '' : u).trim();
  return /^https?:\\/\\//i.test(s) ? escapeNewsHtml(s) : '#';
}}

refreshNewsFromWorker();
</script>

</body>
</html>
"""


def snapshot_latest_date(path: Path = BASKET_SNAPSHOT_CSV):
    """Latest transaction date in the committed snapshot (what CI publishes from),
    or None if the file is absent/unreadable."""
    if not path.exists():
        return None
    try:
        return pd.to_datetime(pd.read_csv(path)["date"], errors="coerce").max()
    except Exception:
        return None


def snapshot_is_behind_log(snapshot_date, log_date) -> bool:
    """True when log.xlsx has a newer trade than the committed snapshot — i.e. the
    published dashboard is stale relative to the author's real ledger. Pure helper
    so it's testable without the private log. (Both args may be None → False.)"""
    if snapshot_date is None or log_date is None:
        return False
    return pd.Timestamp(log_date).normalize() > pd.Timestamp(snapshot_date).normalize()


def validate_build_invariants(basket, prices_native, positions) -> None:
    """Cheap build-time invariants for the real publish build. Raise SystemExit
    (non-zero) so CI skips the commit and the last-good page stays live."""
    import math
    if prices_native is None or prices_native.empty:
        raise SystemExit("BUILD INVARIANT: no market prices fetched")
    if basket is None or len(basket) == 0:
        raise SystemExit("BUILD INVARIANT: empty basket MTM series")
    last = float(basket.iloc[-1])
    if math.isnan(last) or math.isinf(last):
        raise SystemExit("BUILD INVARIANT: basket MTM last value is not finite")
    if positions is None or positions.empty:
        raise SystemExit("BUILD INVARIANT: no positions")


def resolve_basket_source(demo: bool, watchlist_only: bool, from_snapshot: bool,
                          log_exists: bool, snapshot_exists: bool) -> str:
    """Decide which basket source a non-demo build renders from.

    Returns one of: "watchlist", "log", "snapshot", "csv".
    Forker safety: the committed snapshot is only used when log.xlsx is present
    (author) or --from-snapshot is passed (CI); a plain clone falls through to
    its own transactions.csv.
    """
    if watchlist_only:
        return "watchlist"
    if demo:
        return "csv"
    if log_exists:
        return "log"          # author: regenerate snapshot from log, then render it
    if from_snapshot and snapshot_exists:
        return "snapshot"     # CI: render the committed snapshot
    return "csv"              # forker / sample


def _is_sample_build(demo: bool, source: str) -> bool:
    """True when the page should show the DEMO/sample banner and self-inline its
    data (the bundled transactions.csv sample or an explicit --demo). The real
    author build ("log") and the CI publish build ("snapshot") are NOT sample
    builds — they render the real basket, so they get the sidecar payload and no
    banner. ("watchlist" has its own banner.) This is the guard that the v2.8 CI
    publish initially got wrong (log.xlsx absent in CI was mistaken for demo)."""
    return demo or source == "csv"


def main(demo: bool = False, watchlist_only: bool = False,
         from_snapshot: bool = False) -> None:
    # Source selection (see resolve_basket_source for forker safety):
    #   --demo            -> sample transactions.csv (self-contained demo.html)
    #   log.xlsx present  -> AUTHOR build: read the private log ONLY to regenerate
    #                        the public basket.snapshot.csv, then render FROM the
    #                        snapshot so local preview == what CI publishes.
    #   --from-snapshot   -> CI build: render the committed basket.snapshot.csv
    #                        (log.xlsx absent). Forkers (no log, no flag) fall
    #                        through to their own transactions.csv.
    t0 = time.time()
    source = resolve_basket_source(
        demo=demo, watchlist_only=watchlist_only, from_snapshot=from_snapshot,
        log_exists=LOG_XLSX.exists(), snapshot_exists=BASKET_SNAPSHOT_CSV.exists())
    untracked = pd.DataFrame()
    if source == "watchlist":
        print("Watchlist-only mode: ignoring log.xlsx / transactions.csv")
        transactions = pd.DataFrame(columns=["ticker", "date", "action", "shares"])
    elif source == "log":
        print(f"Loading transactions from {LOG_XLSX} (regenerating snapshot)")
        real_txns, untracked = load_transactions_from_log()
        # Drift check BEFORE we overwrite the committed snapshot: did the author
        # trade since the snapshot CI publishes from was last regenerated? (The
        # published page is only as current as basket.snapshot.csv.)
        _snap_was = snapshot_latest_date()
        _log_latest = (pd.to_datetime(real_txns["date"], errors="coerce").max()
                       if not real_txns.empty else None)
        if snapshot_is_behind_log(_snap_was, _log_latest):
            print(f"  NOTE: committed snapshot was behind log.xlsx "
                  f"({_snap_was.date()} -> {pd.Timestamp(_log_latest).date()}); regenerating now. "
                  f"Commit basket.snapshot.csv + push so CI republishes the update.",
                  file=sys.stderr)
        if not untracked.empty:
            print(f"  {len(untracked)} untracked manual-fund rows "
                  f"(no ticker/ISIN) — excluded from snapshot")
        write_basket_snapshot(real_txns)
        # Privacy guard on the freshly-written artifact BEFORE it can be committed
        # (CI runs the same check, but only after the author has already pushed).
        from sanity_check import assert_snapshot_is_clean
        try:
            assert_snapshot_is_clean(pd.read_csv(BASKET_SNAPSHOT_CSV))
        except AssertionError as e:
            raise SystemExit(f"ABORT: regenerated snapshot failed privacy guard ({e}) "
                             f"— not safe to commit")
        transactions = load_transactions_from_snapshot()
        untracked = pd.DataFrame()   # manual-fund rows are not in the snapshot
    elif source == "snapshot":
        print(f"Loading transactions from {BASKET_SNAPSHOT_CSV} (--from-snapshot)")
        transactions = load_transactions_from_snapshot()
    else:
        print(f"Loading transactions from {TRANSACTIONS_CSV}")
        transactions = load_transactions()
    if not watchlist_only:
        n_txns = len(transactions)
        n_buys = int((transactions.action == "BUY").sum())
        n_sells = int((transactions.action == "SELL").sum())
        print(f"  {n_txns} transactions ({n_buys} buys, {n_sells} sells) across "
              f"{transactions.ticker.nunique()} tickers")

    watchlist = load_watchlist()
    if not watchlist.empty:
        print(f"Watchlist: {len(watchlist)} ticker(s) from {WATCHLIST_CSV.name}")

    txn_tickers = set(transactions.ticker.unique().tolist())
    watch_tickers = set(watchlist.ticker.tolist()) if not watchlist.empty else set()
    ticker_list = sorted(txn_tickers | watch_tickers)
    print(f"Pulling {len(ticker_list)} tickers from yfinance (native currencies)"
          f" — {len(txn_tickers)} held + {len(watch_tickers - txn_tickers)} watch-only...")
    # One yfinance batch returns full OHLCV; we cache it for ATR/Volume metrics
    # and derive the close-only frame the rest of the pipeline already consumes.
    ohlcv_native, ohlcv_failed, retries_recovered = download_ohlcv(ticker_list)
    if ohlcv_native.empty:
        prices_native = pd.DataFrame()
    else:
        prices_native = ohlcv_native.xs("Close", axis=1, level=1).copy()
    print(f"Got native prices: {prices_native.shape[0]} rows x {prices_native.shape[1]} tickers")

    print(f"Pulling benchmark {BENCHMARK}...")
    bench_native = download_benchmark()
    print(f"Pulling Nasdaq overlay {BENCHMARK2}...")
    nasdaq_native = download_benchmark(BENCHMARK2)

    CACHE_PARQUET.parent.mkdir(parents=True, exist_ok=True)
    try:
        prices_native.to_parquet(CACHE_PARQUET)
        if not ohlcv_native.empty:
            try:
                ohlcv_native.to_parquet(OHLCV_CACHE)
                print(f"Cached OHLCV to {OHLCV_CACHE}")
            except Exception as e:
                print(f"WARN couldn't cache OHLCV: {e}", file=sys.stderr)
        if not bench_native.empty:
            bench_native.to_frame().to_parquet(BENCHMARK_CACHE)
        if not nasdaq_native.empty:
            nasdaq_native.to_frame().to_parquet(BENCHMARK2_CACHE)
        print(f"Cached native prices to {CACHE_PARQUET}")
    except ImportError:
        prices_native.to_csv(CACHE_PARQUET.with_suffix(".csv"))

    meta_cache = load_meta_cache()
    meta, meta_failed = fetch_meta(list(prices_native.columns), meta_cache)

    # Determine which FX pairs we need (every distinct non-base currency in the
    # universe + the benchmark currency).
    needed_ccys = set()
    for tkr in prices_native.columns:
        ccy = ticker_currency(meta, tkr)
        if ccy != BASE_CCY:
            needed_ccys.add(ccy)
    if BENCHMARK_CCY != BASE_CCY:
        needed_ccys.add(BENCHMARK_CCY)
    fx_pairs = sorted(f"{c}{BASE_CCY}=X" for c in needed_ccys)
    print(f"FX pairs needed: {fx_pairs or '(none)'}")
    fx = download_fx(fx_pairs)
    if not fx.empty:
        try:
            fx.to_parquet(FX_CACHE)
            print(f"Cached FX to {FX_CACHE}")
        except Exception as e:
            print(f"WARN couldn't cache FX: {e}", file=sys.stderr)

    # Convert to base currency
    prices = convert_to_base(prices_native, meta, fx, base=BASE_CCY)
    bench_meta = pd.DataFrame({"currency": [BENCHMARK_CCY]}, index=[BENCHMARK])
    bench_df = convert_to_base(bench_native.to_frame(name=BENCHMARK), bench_meta, fx, base=BASE_CCY) \
               if not bench_native.empty else pd.DataFrame()
    bench = bench_df[BENCHMARK] if not bench_df.empty else pd.Series(dtype=float)
    nasdaq_meta = pd.DataFrame({"currency": [BENCHMARK2_CCY]}, index=[BENCHMARK2])
    nasdaq_df = convert_to_base(nasdaq_native.to_frame(name=BENCHMARK2), nasdaq_meta, fx, base=BASE_CCY) \
                if not nasdaq_native.empty else pd.DataFrame()
    nasdaq_b = nasdaq_df[BENCHMARK2] if not nasdaq_df.empty else pd.Series(dtype=float)

    if watchlist_only:
        transactions = _synthesize_watchlist_transactions(watchlist, prices)
        print(f"  synthesized {len(transactions)} watchlist tickers as equal-weight "
              f"positions (tracked from each ticker's window start)")
        watchlist = watchlist.iloc[0:0]   # rendered as the basket, not a separate panel

    returns = build_positions(transactions, prices)
    returns_native = build_positions(transactions, prices_native)
    print(f"Built {len(returns)} positions (base={BASE_CCY}) — "
          f"{int((returns.status == 'open').sum())} open, "
          f"{int((returns.status == 'closed').sum())} closed")

    basket = compute_basket_mtm_series(transactions, prices)
    if not demo and not watchlist_only:
        validate_build_invariants(basket, prices_native, returns)
    print(f"Basket series ({BASE_CCY}): {len(basket)} daily points "
          f"(range {basket.min():+.2f} to {basket.max():+.2f})")

    first_purchase = pd.Timestamp(transactions[transactions.action == "BUY"].date.min())
    bench_series = compute_benchmark_series(bench, first_purchase)
    print(f"Benchmark {BENCHMARK} series ({BASE_CCY}): {len(bench_series)} points")
    nasdaq_series = compute_benchmark_series(nasdaq_b, first_purchase)
    print(f"Nasdaq {BENCHMARK2} series ({BASE_CCY}): {len(nasdaq_series)} points")

    contrib = compute_contributors(returns)
    if not contrib.empty:
        print(f"Contributors: top {contrib.iloc[0].name} ({contrib.iloc[0].contribution_pp:+.2f} pp), "
              f"worst {contrib.iloc[-1].name} ({contrib.iloc[-1].contribution_pp:+.2f} pp)")
    else:
        print("Contributors: none (no open positions)")

    signals = compute_signals(prices)
    sig_counts = signals.signal.value_counts()
    print(f"Signals: {dict(sig_counts.head(5))}")

    # Quant signals (modal sub-row): 200d distance / ATR / RSI / 52w pos / Volume.
    # Derived from native OHLCV so ATR uses real H/L; ATR is then converted to
    # GBP using the same FX series the rest of the dashboard already uses.
    quant_metrics = compute_quant_metrics(ohlcv_native, fx, meta) \
        if not ohlcv_native.empty else pd.DataFrame()
    if not quant_metrics.empty:
        print(f"Quant metrics: {len(quant_metrics)} tickers (SMA200 / ATR / RSI / 52w / Volume)")

    # Analyst data: fetched for every tracked ticker (open + closed) so the
    # main table can surface target/upside/rec for all rows. The 7-day TTL
    # means typical builds refresh nothing — only ~one batch fetch per week.
    # `analyst_candidates` still drives the "Re-entry ideas" panel, which is
    # closed-positions-only ("should I buy back in?").
    # v2.7: include watch-only names so the enriched watchlist can show Street
    # upside + news cites (they already have technicals via signals/quant_metrics).
    all_fetch_tickers = sorted(set(returns.index.tolist()) | watch_tickers)
    analyst_candidates = sorted(returns[returns.status == "closed"].index.tolist()) \
        if not returns.empty else []
    analyst_cache = load_analyst_cache()
    if demo:
        # Demo keeps the simple "snapshot the cache before fetch overwrites it"
        # diff -- its rating moves are illustrative, not tracked over time.
        snapshot_prior_analyst()
    if all_fetch_tickers:
        analyst, analyst_failed = fetch_analyst_data(all_fetch_tickers, analyst_cache)
    else:
        analyst, analyst_failed = analyst_cache, set()
    # v2.8 rating-moves baseline. Real build: a rolling ~2-week window selected
    # from a committed analyst-snapshot history (survives stateless CI, unlike the
    # old mtime-based prior). Demo: the last-build prior cache.
    rating_baseline_date = None
    rating_baseline_seeded = False
    if not demo:
        _today = pd.Timestamp(datetime.now(timezone.utc).date())
        seed_rating_history(PRIOR_ANALYST_CACHE, _today)   # cold-start backfill
        _hist = append_analyst_history(analyst, _today)
        rating_baseline_date, _baseline_df = select_rating_baseline(_hist, _today)
        rating_baseline_seeded = rating_baseline_is_seeded(rating_baseline_date)
        rating_moves = compute_rating_moves(None, analyst, prior_df=_baseline_df)
    else:
        rating_moves = compute_rating_moves(PRIOR_ANALYST_CACHE, analyst)
    rating_prior_exists = ((rating_baseline_date is not None) if not demo
                           else PRIOR_ANALYST_CACHE.exists())
    if rating_moves:
        _since = (f" since {rating_baseline_date.date()}"
                  if rating_baseline_date is not None else "")
        print(f"Rating moves: {len(rating_moves)} material moves{_since}")
    elif not demo and rating_baseline_date is None:
        print("Rating moves: building history -- the 2-week window fills in over the next fortnight")

    # Per-ticker news: same 7-day TTL pattern as analyst cache. Most builds
    # reuse the parquet; a fresh fetch happens roughly once per week per ticker.
    ticker_news_cache = load_ticker_news_cache()
    if all_fetch_tickers:
        ticker_news, news_failed = fetch_ticker_news(all_fetch_tickers, ticker_news_cache)
    else:
        ticker_news, news_failed = ticker_news_cache, set()

    # Industry outlook universe — loaded from universe.csv, cached with 30-day
    # TTL so daily builds reuse and only ~once a month does this run a fresh
    # yfinance batch for ~100 large-caps.
    universe_tickers = load_universe()
    if universe_tickers:
        # Reuse the freshly-updated meta_cache so newly-fetched universe meta
        # entries persist into the same meta.csv as the portfolio.
        meta_cache_for_universe = load_meta_cache()
        universe_outlook = fetch_universe_outlook(universe_tickers, meta_cache_for_universe)
    else:
        universe_outlook = pd.DataFrame()

    news_items = fetch_news()

    # In demo mode we inline SortableJS so the standalone demo.html works when
    # opened directly from disk (no vendor/ adjacent). Real builds keep the
    # external <script src="vendor/Sortable.min.js"> reference.
    sortable_inline_js: str | None = None
    if demo and SORTABLE_VENDOR.exists():
        sortable_inline_js = SORTABLE_VENDOR.read_text(encoding="utf-8")

    # T10: unusual-volume chips near the hero subtitle. Filter to open
    # positions where today's volume is >2x the 63-day average AND the
    # latest-day move is non-trivial (|move| > 1%). Sorted by abs(move),
    # capped at 3 pills (more would clutter the hero).
    unusual_vol = []
    if not quant_metrics.empty and not prices.empty and not returns.empty and len(prices.index) >= 2:
        open_tkrs = set(returns[returns.status == "open"].index.tolist())
        for tkr in quant_metrics.index:
            if tkr not in open_tkrs or tkr not in prices.columns:
                continue
            vr = quant_metrics.loc[tkr, "vol_ratio"]
            if pd.isna(vr) or float(vr) <= 2.0:
                continue
            # v1.8.1 B1: use each ticker's last 2 VALID prices instead of the
            # global index endpoints. When the build runs on a weekend/holiday
            # (latest_date = Sunday today) the latest row is NaN for US tickers
            # so the global-endpoint version silently dropped every match.
            s = prices[tkr].dropna()
            if len(s) < 2:
                continue
            last_px = float(s.iloc[-1])
            prev_px = float(s.iloc[-2])
            if not (last_px > 0 and prev_px > 0):
                continue
            daily_pct = (last_px / prev_px - 1) * 100
            if abs(daily_pct) <= 1:
                continue
            unusual_vol.append({
                "ticker":    str(tkr),
                "daily_pct": float(daily_pct),
                "vol_ratio": float(vr),
            })
        unusual_vol.sort(key=lambda x: abs(x["daily_pct"]), reverse=True)
        unusual_vol = unusual_vol[:3]
        if unusual_vol:
            print(f"Unusual volume: {len(unusual_vol)} open name(s) on >2x vol with >1% move")

    # Build-health: union of every fetcher's failed-ticker set so the footer
    # surfaces silent yfinance failures that would otherwise hide in stderr.
    # "attempted" is the held+watch universe (OHLCV is the gate — anything
    # missing here cascades). Meta/analyst/news failures are extra colour.
    all_failed = ohlcv_failed | meta_failed | analyst_failed | news_failed
    # v1.9 D1: split the count so the footer matches the basket size users see
    # elsewhere on the page. "attempted" is the full fetch (held + watch); the
    # extra `held` / `watch_only` fields let the footer render the breakdown
    # clearly ("185 held + 2 watch") rather than a confusing total.
    n_watch_only = len(watch_tickers - txn_tickers)
    n_held = len(ticker_list) - n_watch_only
    build_health = {
        "attempted": len(ticker_list),
        "succeeded": len(ticker_list) - len(ohlcv_failed),
        "n_held": n_held,
        "n_watch_only": n_watch_only,
        "failed": sorted(all_failed),
        "retries_recovered": retries_recovered,
        "build_seconds": int(time.time() - t0),
    }
    print(f"Build health: {build_health['succeeded']}/{build_health['attempted']} OK, "
          f"{retries_recovered} retry-recovered, {build_health['build_seconds']}s")
    if all_failed:
        print(f"  failed: {', '.join(sorted(all_failed))}")

    # v2.3 Market expectations: prediction-market sentiment. Live fetch only on
    # the real (local) build; demo/CI renders from the committed cache so the
    # daily cron makes no Kalshi/Polymarket calls.
    pred_themes = load_prediction_themes()
    if not demo and pred_themes:
        snapshot_prior_predictions()
        pred_current = fetch_predictions(pred_themes)
        if pred_current:
            save_predictions_cache(pred_current)
    else:
        pred_current = load_predictions_cache()
    pred_prior = load_predictions_cache(PRIOR_PREDICTIONS_CACHE)
    prediction_rows = compute_prediction_moves(pred_prior, pred_current)
    # v3.0 #6: drop far-dated markets (resolve > ~3 months out) so the panel
    # stays relevant; evaluated at render time so the window stays current.
    prediction_rows = filter_predictions_horizon(prediction_rows)
    if prediction_rows:
        print(f"Market expectations: {len(prediction_rows)} markets "
              f"({'live' if not demo else 'cached'})")

    # v2.2 Big Brain: universe discovery lane (shortlist + deepen)
    bb_universe_obs = []
    auto_value_rows = None          # v3.0 #5: value_rows shared with render_html
    auto_tickers: list[str] = []
    try:
        held_sold = set(returns.index.tolist()) if not returns.empty else set()
        bb_shortlist = _bb_universe_shortlist(universe_outlook, exclude=held_sold, n=40)
        if bb_shortlist:
            bb_q, bb_sig, bb_news, bb_ohlcv = _bb_deepen_universe(bb_shortlist, meta, fx)
            bb_sector_avg = _bb_sector_avg_returns(returns, meta)
            bb_universe_obs = _bb_build_universe_observations(
                bb_shortlist, bb_q, bb_sig, None, bb_news,
                sector_avg=bb_sector_avg, outlook=universe_outlook)
            print(f"Big Brain: {len(bb_shortlist)} universe shortlisted, "
                  f"{len(bb_universe_obs)} idea candidates")
            # v3.0 #5: auto-watchlist — names flagged by BOTH the value screen and
            # Big Brain. Reuse the BB-deepen OHLCV/quant/news + universe analyst
            # (already in memory) so these get full-parity cards with no extra fetch.
            bb_idea_set = {o.get("ticker") for o in bb_universe_obs}
            auto_value_rows = build_value_screen(universe_outlook, log_tickers=txn_tickers,
                                                 bb_idea_tickers=bb_idea_set)
            auto_tickers = select_auto_watchlist(auto_value_rows, watch_tickers)
            if auto_tickers and not bb_ohlcv.empty:
                bb_close = bb_ohlcv.xs("Close", axis=1, level=1)
                keep = [t for t in auto_tickers if t in bb_close.columns]
                if keep:
                    meta, _ = fetch_meta(keep, meta)          # industry label + currency
                    add_native = bb_close[keep].dropna(how="all")
                    add_base = convert_to_base(add_native, meta, fx, base=BASE_CCY)
                    new_n = [c for c in keep if c not in prices_native.columns]
                    new_b = [c for c in keep if c not in prices.columns]
                    if new_n:
                        prices_native = pd.concat([prices_native, add_native[new_n]], axis=1)
                    if new_b:
                        prices = pd.concat([prices, add_base[new_b]], axis=1)
                    if bb_q is not None and not bb_q.empty:
                        q_add = bb_q.loc[bb_q.index.intersection(keep)]
                        q_add = q_add[~q_add.index.isin(quant_metrics.index)]
                        if not q_add.empty:
                            quant_metrics = pd.concat([quant_metrics, q_add])
                    if bb_news is not None and not bb_news.empty and ticker_news is not None:
                        n_add = bb_news.loc[bb_news.index.intersection(keep)]
                        n_add = n_add[~n_add.index.isin(ticker_news.index)]
                        if not n_add.empty:
                            ticker_news = pd.concat([ticker_news, n_add])
                    acols = [c for c in ("target_mean", "current_price",
                                         "recommendation", "num_analysts")
                             if c in universe_outlook.columns]
                    if acols and analyst is not None:
                        a_add = universe_outlook.loc[
                            universe_outlook.index.intersection(keep), acols]
                        a_add = a_add[~a_add.index.isin(analyst.index)]
                        if not a_add.empty:
                            analyst = pd.concat([analyst, a_add])
                    print(f"Auto-watchlist: {len(keep)} Value/BB name(s): {', '.join(keep)}")
    except Exception as e:
        print(f"WARN Big Brain universe lane skipped: {e}", file=sys.stderr)

    html = render_html(returns, prices, meta, basket, bench_series, contrib, transactions,
                       signals, prices_native, returns_native, untracked=untracked,
                       watchlist=watchlist, news_items=news_items, analyst=analyst,
                       analyst_candidates=analyst_candidates, fx=fx,
                       universe_outlook=universe_outlook,
                       quant_metrics=quant_metrics,
                       ticker_news=ticker_news,
                       demo_mode=_is_sample_build(demo, source),
                       watchlist_only=watchlist_only,
                       sortable_inline_js=sortable_inline_js,
                       build_health=build_health,
                       rating_moves=rating_moves,
                       prior_analyst_exists=rating_prior_exists,
                       rating_baseline_date=rating_baseline_date,
                       rating_baseline_seeded=rating_baseline_seeded,
                       unusual_vol=unusual_vol,
                       bb_universe_obs=bb_universe_obs,
                       prediction_rows=prediction_rows,
                       nasdaq_series=nasdaq_series,
                       value_rows=auto_value_rows,
                       auto_tickers=auto_tickers)

    out_path = DEMO_OUT_HTML if demo else OUT_HTML
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    print(f"Wrote {out_path} ({out_path.stat().st_size/1024:.1f} KB)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Build the stocks dashboard. Default writes docs/index.html "
                    "using log.xlsx if present; --demo writes a self-contained "
                    "demo.html using transactions.csv only."
    )
    parser.add_argument("--demo", action="store_true",
                        help="Force the standalone demo build (repo-root "
                             "demo.html, transactions.csv, inlined SortableJS).")
    parser.add_argument("--weight", choices=["equal", "value"], default=None,
                        help="Position weighting: 'equal' (default, one unit per "
                             "position) or 'value' (capital-weighted by real share "
                             "quantities). Overrides the WEIGHT_MODE env var.")
    parser.add_argument("--watchlist-only", action="store_true",
                        help="Build from watchlist.csv only (no positions): each "
                             "ticker is tracked equal-weight from its window start. "
                             "Lets someone use the dashboard with zero trade history.")
    parser.add_argument("--from-snapshot", action="store_true",
                        help="Render docs/index.html from the committed "
                             "basket.snapshot.csv (CI publish path; no log.xlsx).")
    parser.add_argument("--export-snapshot", action="store_true",
                        help="Fast path after a trade: regenerate basket.snapshot.csv "
                             "from log.xlsx and exit (no full build, no network). "
                             "Then commit basket.snapshot.csv and push; CI republishes.")
    args = parser.parse_args()
    if args.weight:
        WEIGHT_MODE = args.weight   # module-level rebind; functions read this global
    if args.export_snapshot:
        if not LOG_XLSX.exists():
            print(f"--export-snapshot needs {LOG_XLSX.name} (author-only); nothing to do.")
            sys.exit(1)
        _txns, _ = load_transactions_from_log()
        snap = write_basket_snapshot(_txns)
        # Privacy guard BEFORE the author commits the public file. CI runs the
        # same check, but only after this snapshot would already be pushed — so
        # catch a regressed export here, where a leak can still be stopped.
        from sanity_check import assert_snapshot_is_clean
        try:
            assert_snapshot_is_clean(pd.read_csv(BASKET_SNAPSHOT_CSV))
        except AssertionError as e:
            print(f"ABORT: snapshot failed privacy guard ({e}). NOT safe to commit.",
                  file=sys.stderr)
            sys.exit(2)
        print(f"Snapshot regenerated ({len(snap)} rows, privacy guard passed). Next:\n"
              f"  git add basket.snapshot.csv\n"
              f"  git commit -m \"trade update\"\n"
              f"  git push        # CI rebuilds + republishes")
        sys.exit(0)
    main(demo=args.demo, watchlist_only=args.watchlist_only,
         from_snapshot=args.from_snapshot)
