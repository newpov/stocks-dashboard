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

import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

ROOT = Path(__file__).parent
TRANSACTIONS_CSV = ROOT / "transactions.csv"
LOG_XLSX = ROOT / "log.xlsx"           # Trading 212-style real transaction log
TICKERS_CSV = ROOT / "tickers.csv"  # legacy fallback
OUT_HTML = ROOT / "docs" / "index.html"
CACHE_PARQUET = ROOT / "data" / "prices_cache.parquet"
BENCHMARK_CACHE = ROOT / "data" / "benchmark_cache.parquet"
META_CSV = ROOT / "data" / "meta.csv"
WATCHLIST_CSV = ROOT / "watchlist.csv"
ANALYST_CACHE = ROOT / "data" / "analyst_cache.parquet"
ANALYST_TTL_DAYS = 7    # refetch a ticker's analyst data when older than this

# How many candidates the analyst panel shows in total (the grid scrolls
# internally past ~6 visible). Safety cap for very large closed-position lists.
ANALYST_TOP_N = 50

DEFAULT_BASELINE = pd.Timestamp("2024-10-21")
START_DATE = "2024-10-14"
BENCHMARK = "SPY"
BENCHMARK_CCY = "USD"            # native currency of the benchmark
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
def load_transactions() -> pd.DataFrame:
    """Load and validate transactions.csv (ticker, date, action, shares).

    Each row is a single BUY or SELL event. The script aggregates these per
    ticker into positions, looking up prices from yfinance for the transaction
    dates so cost basis is consistent with the daily price data.
    """
    df = pd.read_csv(TRANSACTIONS_CSV)
    df["ticker"] = df["ticker"].astype(str).str.strip().str.upper()
    df = df[df["ticker"] != ""].copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["action"] = df["action"].astype(str).str.strip().str.upper()
    df["shares"] = pd.to_numeric(df["shares"], errors="coerce").fillna(0.0)
    df = df[df["action"].isin(["BUY", "SELL"])]
    df = df[df["shares"] > 0]
    df = df.dropna(subset=["date"])
    return df.sort_values(["ticker", "date"]).reset_index(drop=True)


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

    # Each broker entry = 1 unit (fractional-share platforms hide the qty).
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


def download_prices(tickers: list[str]) -> pd.DataFrame:
    end = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
    data = yf.download(
        tickers, start=START_DATE, end=end,
        auto_adjust=True, progress=False, group_by="ticker", threads=True,
    )
    closes: dict[str, pd.Series] = {}
    failed: list[str] = []
    for t in tickers:
        try:
            s = data[t]["Close"]
            if s.notna().any():
                closes[t] = s
            else:
                failed.append(t)
        except (KeyError, ValueError):
            failed.append(t)

    for t in failed:
        try:
            s = yf.Ticker(t).history(start=START_DATE, end=end, auto_adjust=True)["Close"]
            if s.notna().any():
                s.index = s.index.tz_localize(None) if s.index.tz is not None else s.index
                closes[t] = s
                print(f"RETRY OK: {t}", file=sys.stderr)
        except Exception as e:
            print(f"WARN retry failed for {t}: {e}", file=sys.stderr)

    df = pd.DataFrame(closes).sort_index()
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    return df


def download_benchmark() -> pd.Series:
    end = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        df = yf.download(BENCHMARK, start=START_DATE, end=end,
                         auto_adjust=True, progress=False, threads=False)
        if df.empty:
            return pd.Series(dtype=float)
        s = df["Close"]
        if hasattr(s, "ndim") and s.ndim > 1:
            s = s.iloc[:, 0]
        if s.index.tz is not None:
            s.index = s.index.tz_localize(None)
        s.name = BENCHMARK
        return s
    except Exception as e:
        print(f"WARN benchmark download failed: {e}", file=sys.stderr)
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


def fetch_meta(tickers: list[str], cache: pd.DataFrame) -> pd.DataFrame:
    # A ticker needs re-fetching if it's not in cache OR if currency is missing
    # (the latter handles upgrading old caches that pre-date the currency column)
    missing = []
    for t in tickers:
        if t not in cache.index:
            missing.append(t)
        elif not str(cache.loc[t, "currency"] or "").strip():
            missing.append(t)
    if not missing:
        return cache
    print(f"Fetching metadata for {len(missing)} ticker(s) (parallel x4)...", flush=True)

    def one(t: str):
        try:
            info = yf.Ticker(t).info or {}
            return t, {
                "sector": (info.get("sector") or "").strip(),
                "industry": (info.get("industry") or "").strip(),
                "name": (info.get("shortName") or info.get("longName") or t).strip(),
                "currency": (info.get("currency") or "USD").strip(),
            }
        except Exception as e:
            print(f"  meta fail {t}: {e}", file=sys.stderr)
            return t, {"sector": "", "industry": "", "name": t, "currency": "USD"}

    rows = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(one, t): t for t in missing}
        for i, fut in enumerate(as_completed(futures), 1):
            t, meta = fut.result()
            rows.append({"ticker": t, **meta})
            if i % 10 == 0 or i == len(missing):
                print(f"  meta: {i}/{len(missing)}", flush=True)

    new_df = pd.DataFrame(rows).set_index("ticker")
    combined = pd.concat([cache.drop(index=[t for t in missing if t in cache.index]),
                           new_df]) if not cache.empty else new_df
    combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    META_CSV.parent.mkdir(parents=True, exist_ok=True)
    combined.to_csv(META_CSV)
    print(f"  cached metadata to {META_CSV}", flush=True)
    return combined


def load_analyst_cache() -> pd.DataFrame:
    if not ANALYST_CACHE.exists():
        return pd.DataFrame(columns=[
            "target_mean", "target_high", "target_low", "num_analysts",
            "recommendation", "rec_mean", "current_price", "fetched_at",
        ]).rename_axis("ticker")
    df = pd.read_parquet(ANALYST_CACHE)
    if "fetched_at" in df.columns:
        df["fetched_at"] = pd.to_datetime(df["fetched_at"], utc=True)
    return df


def fetch_analyst_data(tickers: list[str], cache: pd.DataFrame,
                       ttl_days: int = ANALYST_TTL_DAYS) -> pd.DataFrame:
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
        return cache
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
                "fetched_at":   now,
            }
        except Exception as e:
            print(f"  analyst fail {t}: {e}", file=sys.stderr)
            return t, {
                "target_mean": None, "target_high": None, "target_low": None,
                "num_analysts": None, "recommendation": "", "rec_mean": None,
                "current_price": None, "fetched_at": now,
            }

    rows = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(one, t): t for t in to_fetch}
        for i, fut in enumerate(as_completed(futures), 1):
            t, data = fut.result()
            rows.append({"ticker": t, **data})
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
    return combined


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


def convert_to_base(prices: pd.DataFrame, meta: pd.DataFrame,
                    fx: pd.DataFrame, base: str = BASE_CCY) -> pd.DataFrame:
    """Convert native-currency prices to base currency using daily FX rates.
    Returns a new DataFrame with the same shape but base-currency values."""
    out = prices.copy()
    for tkr in prices.columns:
        raw_ccy = str(meta.loc[tkr, "currency"]) if tkr in meta.index else "USD"
        ccy, divisor = normalize_currency(raw_ccy)
        if divisor != 1.0:
            out[tkr] = out[tkr] / divisor
        if ccy == base:
            continue
        fx_key = f"{ccy}{base}=X"
        if fx_key in fx.columns:
            rate = fx[fx_key].reindex(out.index).ffill().bfill()
            out[tkr] = out[tkr] * rate
        else:
            print(f"WARN no FX series {fx_key}, leaving {tkr} as {ccy}", file=sys.stderr)
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


def build_positions(transactions: pd.DataFrame, prices: pd.DataFrame) -> pd.DataFrame:
    """Aggregate transactions into positions using average-cost accounting.

    Output column names mirror the old returns shape (baseline, baseline_date,
    latest, total_pct, ...) so the render layer keeps working; new columns
    (shares_held, status, total_invested, ...) extend the schema.
    """
    if transactions.empty:
        return pd.DataFrame()
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
        txns["price"] = txns["date"].apply(lambda d: _txn_price(d, ticker_series))
        txns = txns.dropna(subset=["price"])
        if txns.empty:
            continue

        buys = txns[txns.action == "BUY"]
        sells = txns[txns.action == "SELL"]
        total_bought = float(buys.shares.sum())
        total_sold = float(sells.shares.sum())
        shares_held = total_bought - total_sold
        if total_bought <= 0:
            continue

        avg_buy_price = float((buys.shares * buys.price).sum() / total_bought)
        total_invested = float((buys.shares * buys.price).sum())
        total_received = float((sells.shares * sells.price).sum()) if total_sold > 0 else 0.0
        avg_sell_price = (float((sells.shares * sells.price).sum() / total_sold)
                          if total_sold > 0 else float("nan"))

        first_buy_date = pd.Timestamp(buys.date.min())
        last_action_date = pd.Timestamp(txns.date.max())
        latest = float(ticker_series.iloc[-1])

        # Transactional-recency rule: a position is "closed" when the most
        # recent trade row is a SELL, "open" otherwise. This is robust to
        # partial exits and to multiple open/close cycles on the same
        # ticker, where share-net math (e.g. shares_held > 0) would either
        # misclassify or require real per-row share quantities we don't have.
        last_action = str(txns.iloc[-1].action).upper()
        status = "closed" if last_action == "SELL" else "open"
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
        ytd_ref = float(ytd_sub.iloc[-1]) if not ytd_sub.empty else avg_buy_price
        ytd_pct = (latest / ytd_ref - 1) * 100 if status == "open" and ytd_ref > 0 else float("nan")

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
            "weight": total_invested if status == "open" else 0.0,
            "shares_held": shares_held,
            "total_bought": total_bought,
            "total_sold": total_sold,
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
            "last_action_date": last_action_date,
            "n_transactions": len(txns),
            "transactions": txn_list,
        })
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).set_index("ticker")


def compute_basket_mtm_series(transactions: pd.DataFrame, prices: pd.DataFrame) -> pd.Series:
    """Daily portfolio % return on cumulative capital deployed.

    For each date t:
        cum_invested(t) = Σ buy_shares × buy_price for buys up to t (capital put in)
        cum_value(t)    = Σ active_shares(t) × price(t)  (currently-held market value)
                         + Σ sell_shares × sell_price for sells up to t  (cash from sells)
        return(t)       = (cum_value(t) − cum_invested(t)) / cum_invested(t)

    This includes BOTH unrealized gain on held shares AND realized cash from
    past sells, so closing a winning position doesn't make the line drop.
    """
    if transactions.empty:
        return pd.Series(dtype=float)
    p = prices.ffill()
    dates = p.index
    cum_invested = pd.Series(0.0, index=dates)
    cum_value = pd.Series(0.0, index=dates)

    for tkr, txns in transactions.groupby("ticker"):
        if tkr not in p.columns:
            continue
        s = p[tkr]
        txns = txns.sort_values("date").copy()
        txns["price"] = txns["date"].apply(lambda d: _txn_price(d, s.dropna()))
        txns = txns.dropna(subset=["price"])
        if txns.empty:
            continue

        # Snap each transaction to the nearest trading day >= its date so we
        # can place share/cash deltas onto the price-series index cleanly.
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

        # Share delta: +shares on BUY, −shares on SELL. Cumsum across all dates
        # gives the running net position at each trading day.
        sign = snap_df.action.map({"BUY": 1, "SELL": -1})
        delta = (sign * snap_df.shares).groupby(snap_df.date).sum()
        active_shares = delta.reindex(dates).fillna(0).cumsum()

        # Cumulative buy cash (capital put in)
        buys = snap_df[snap_df.action == "BUY"]
        if not buys.empty:
            buy_cash = (buys.shares * buys.price).groupby(buys.date).sum()
            cum_buy = buy_cash.reindex(dates).fillna(0).cumsum()
        else:
            cum_buy = pd.Series(0.0, index=dates)

        # Cumulative sell cash (proceeds — counts toward portfolio "value")
        sells = snap_df[snap_df.action == "SELL"]
        if not sells.empty:
            sell_cash = (sells.shares * sells.price).groupby(sells.date).sum()
            cum_sell = sell_cash.reindex(dates).fillna(0).cumsum()
        else:
            cum_sell = pd.Series(0.0, index=dates)

        # Active market value (currently-held shares marked to today's close)
        active_value = active_shares * s
        ticker_total_value = active_value.fillna(0) + cum_sell

        cum_invested = cum_invested.add(cum_buy, fill_value=0)
        cum_value = cum_value.add(ticker_total_value, fill_value=0)

    return ((cum_value / cum_invested.replace(0, np.nan)) - 1).fillna(0) * 100


def compute_benchmark_series(bench: pd.Series, start_date: pd.Timestamp) -> pd.Series:
    if bench.empty:
        return pd.Series(dtype=float)
    s = bench.ffill()
    sub = s.loc[:start_date]
    base = float(sub.iloc[-1]) if not sub.empty else float(s.iloc[0])
    rebased = (s / base - 1) * 100
    return rebased.loc[start_date:]


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


def compute_contributors(returns_df: pd.DataFrame) -> pd.DataFrame:
    """Contribution in percentage points to the current weighted-basket return."""
    in_basket = returns_df[returns_df.weight > 0].copy()
    total_w = in_basket.weight.sum()
    if total_w == 0:
        in_basket["contribution_pp"] = 0.0
    else:
        in_basket["contribution_pp"] = (in_basket.weight * in_basket.total_pct) / total_w
    return in_basket.sort_values("contribution_pp", ascending=False)


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
    return (str(s) if s is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


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
                 signals: pd.DataFrame) -> str:
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
            f'<span class="badge-weight" title="{r.shares_held:g} units held '
            f'({int(r.total_bought)} buys, {int(r.total_sold)} sells)">{r.shares_held:g} u</span>'
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

        rows.append(
            f'<tr data-ticker="{tkr}" data-total="{r.total_pct:.4f}" data-weight="{r.weight:.4f}">'
            f'<td class="t-ticker">'
            f'<div class="tkr-main">{tkr}{ccy_badge}{weight_badge}</div>'
            f'<div class="tkr-sub">{industry}</div>'
            f'</td>'
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
      <th data-col="1" data-num="0">Signal</th>
      <th>Trend</th>
      <th data-col="3" data-num="1" class="num">Last</th>
      <th data-col="4" data-num="1" class="num dim-mobile">Cost</th>
      <th data-col="5" data-num="1" class="num dim-mobile">Purchased</th>
      <th data-col="6" data-num="1" class="num">Since baseline</th>
      <th data-col="7" data-num="1" class="num dim-mobile">1W</th>
      <th data-col="8" data-num="1" class="num dim-mobile">1M</th>
      <th data-col="9" data-num="1" class="num dim-mobile">3M</th>
      <th data-col="10" data-num="1" class="num">YTD</th>
      <th data-col="11" data-num="1" class="num dim-mobile" title="For closed positions: how the stock has moved since you sold. Positive = regret, negative = lucky escape.">Post-exit</th>
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
            f'<span class="badge-weight" title="{r.shares_held:g} units held">{r.shares_held:g} u</span>'
            if r.status == "open" else
            '<span class="badge-closed" title="Position closed">CLOSED</span>'
        )
        cards.append(
            f'<div class="card" data-ticker="{tkr}" data-total="{r.total_pct:.4f}" data-weight="{r.weight:.4f}">'
            f'<div class="card-head">'
            f'<div class="card-head-left">'
            f'<span class="card-tkr">{tkr}{ccy_badge}{weight_badge}</span>'
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
        # Contributors WT column now shows cost basis (£ amount actually invested
        # in this position) rather than the old unitless weight.
        cost_str = f"{BASE_SYMBOL}{r.weight:,.0f}"
        return (
            f'<tr data-ticker="{tkr}">'
            f'<td class="ct-tkr">{tkr}<div class="ct-ind">{ind}</div></td>'
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
                               meta: pd.DataFrame, n: int = 8) -> str:
    """Full-width 'Top detractors' panel with technical signal + analyst rec +
    suggested action. Only open positions — you can't exit a closed one."""
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
        cost_str = f"{BASE_SYMBOL}{r.weight:,.0f}"
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
        rows.append(
            f'<tr data-ticker="{tkr}">'
            f'<td class="dt-tkr">{tkr}<div class="dt-ind">{ind}</div></td>'
            f'<td class="num dt-cost">{cost_str}</td>'
            f'<td class="num neg dt-ret">{r.total_pct:+.1f}%</td>'
            f'<td class="num neg dt-contrib">{r.contribution_pp:+.2f} pp</td>'
            f'<td class="dt-sig sig-{sig_tone}">'
            f'<div class="sig-main">{_esc(sig_label)}</div>'
            f'<div class="sig-detail">{_esc(sig_detail)}</div>'
            f'</td>'
            f'<td class="dt-rec"><span class="an-rec {rec_cls}">{rec_label}</span>'
            f'<div class="dt-upside">{upside_str} target</div></td>'
            f'<td class="dt-action"><span class="dt-action-pill dt-action-{action_tone}">{action_label}</span></td>'
            f'</tr>'
        )
    body = "".join(rows)
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
            f'<tr data-ticker="{tkr}">'
            f'<td class="rg-tkr">{tkr}<div class="rg-ind">{ind}</div></td>'
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
    return payload


def render_watchlist(watchlist_payload: dict, meta: pd.DataFrame) -> str:
    if not watchlist_payload:
        return ""
    cards = []
    for tkr, d in watchlist_payload.items():
        ind = _esc(_industry_label(meta, tkr))
        name = _esc(d["name"])
        note = _esc(d.get("note") or "")
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
        cards.append(
            f'<div class="wl-card" data-ticker="{tkr}">'
            f'  <div class="wl-head">'
            f'    <div class="wl-tkr">{tkr}<div class="wl-ind">{ind}</div></div>'
            f'    <div class="wl-pct {cls}">{total:+.1f}%</div>'
            f'  </div>'
            f'  <div class="wl-name">{name}</div>'
            f'  <div class="wl-price"><span class="wl-latest">{latest_str}</span>{gbp_line}</div>'
            f'  <div class="wl-spark {cls}">{sparkline}</div>'
            f'  <div class="wl-foot"><span class="wl-period">12-month</span>{note_html}</div>'
            f'</div>'
        )
    return f"""<section class="watchlist-section">
  <div class="wl-head-row">
    <h3>Watchlist <span class="muted">({len(watchlist_payload)})</span></h3>
    <p class="muted">12-month price history for tickers you're following but
    don't (yet) hold. Add or remove via <code>watchlist.csv</code>. Click a card for the full chart.</p>
  </div>
  <div class="wl-grid">{''.join(cards)}</div>
</section>"""


def build_analyst_payload(candidates: list[str], analyst: pd.DataFrame,
                          prices_native: pd.DataFrame, meta: pd.DataFrame,
                          signals: pd.DataFrame | None = None,
                          top_n: int = ANALYST_TOP_N) -> list[dict]:
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
        rec = (a.get("recommendation") or "").strip().lower()
        # Technical signal — empty/neutral when no data
        if not sig_df.empty and tkr in sig_df.index:
            sig_row = sig_df.loc[tkr]
            sig_label = str(sig_row.signal)
            sig_tone = str(sig_row.tone)
        else:
            sig_label, sig_tone = "—", "neutral"
        rows.append({
            "ticker": tkr,
            "name": str(meta.loc[tkr, "name"]) if tkr in meta.index else tkr,
            "industry": _industry_label(meta, tkr),
            "currency": ccy,
            "ccy_symbol": CCY_SYMBOLS.get(ccy, ccy + " "),
            "current": current_native,
            "target_mean": target_major,
            "upside_pct": upside_pct,
            "num_analysts": int(a["num_analysts"]) if pd.notna(a.get("num_analysts")) else 0,
            "recommendation": rec,
            "signal": sig_label,
            "signal_tone": sig_tone,
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
        cards.append(
            f'<div class="an-card" data-ticker="{d["ticker"]}">'
            f'  <div class="an-head">'
            f'    <div class="an-tkr">{d["ticker"]}<div class="an-ind">{_esc(d["industry"])}</div></div>'
            f'    <div class="an-rec {rec_cls}">{label}</div>'
            f'  </div>'
            f'  <div class="an-name">{_esc(d["name"])}</div>'
            f'  <div class="an-line"><span class="an-cur">{cur}</span>'
            f'    <span class="an-dot">·</span>'
            f'    <span class="an-upside {upside_cls}">{d["upside_pct"]:+.1f}% upside</span></div>'
            f'  <div class="an-signal sig-{sig_tone}">'
            f'    <span class="an-signal-dot"></span>{_esc(sig_label)}</div>'
            f'  <div class="an-foot">{d["num_analysts"]} analysts</div>'
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
            f'<a class="news-row" data-source="{_esc(src)}" href="{_esc(it["link"])}" target="_blank" rel="noopener noreferrer">'
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


def render_toolbar(panel_n: int, n: int) -> str:
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
</div>"""


def build_data_payload(returns: pd.DataFrame, prices: pd.DataFrame,
                       meta: pd.DataFrame, contrib: pd.DataFrame,
                       signals: pd.DataFrame,
                       prices_native: pd.DataFrame,
                       returns_native: pd.DataFrame) -> dict:
    daily = prices.ffill()
    contrib_lookup = contrib["contribution_pp"].to_dict() if not contrib.empty else {}
    payload = {}
    for tkr, r in returns.iterrows():
        if tkr not in daily.columns:
            continue
        s = daily[tkr].dropna()
        if s.empty:
            continue
        s_from_base = s.loc[s.index >= r.baseline_date]
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
    return payload


def build_portfolio_payload(basket: pd.Series, bench: pd.Series,
                            first_purchase: pd.Timestamp) -> dict:
    # Resample to weekly to keep JSON small (~3KB vs ~25KB daily)
    def _weekly(s):
        if s.empty:
            return [], []
        w = s.resample("W-FRI").last().ffill().dropna()
        return [d.strftime("%Y-%m-%d") for d in w.index], [round(float(v), 4) for v in w.tolist()]

    b_dates, b_values = _weekly(basket)
    s_dates, s_values = _weekly(bench)
    return {
        "first_purchase": first_purchase.strftime("%Y-%m-%d"),
        "basket": {"dates": b_dates, "values": b_values},
        "spy": {"dates": s_dates, "values": s_values},
    }


def render_html(returns: pd.DataFrame, prices: pd.DataFrame, meta: pd.DataFrame,
                basket: pd.Series, bench: pd.Series, contrib: pd.DataFrame,
                transactions: pd.DataFrame, signals: pd.DataFrame,
                prices_native: pd.DataFrame, returns_native: pd.DataFrame,
                untracked: pd.DataFrame | None = None,
                watchlist: pd.DataFrame | None = None,
                news_items: list[dict] | None = None,
                analyst: pd.DataFrame | None = None,
                analyst_candidates: list[str] | None = None) -> str:
    weekly = prices.resample("W-FRI").last().ffill()
    defs_html = render_svg_defs()
    table_html = render_table(returns, weekly, meta, signals)
    detractors_html = render_detractors_strategy(
        contrib, returns, signals,
        analyst if analyst is not None else pd.DataFrame(),
        meta,
    )
    regret_html = render_regret_tracker(returns, meta)
    untracked_html = render_untracked(untracked) if untracked is not None else ""
    # Watchlist plumbing is preserved; the section is temporarily hidden — the
    # analyst panel takes its left-rail slot until the watchlist comes back.
    watchlist_payload = (build_watchlist_payload(watchlist, prices, prices_native, meta)
                         if watchlist is not None and not watchlist.empty else {})
    candidates = analyst_candidates or []
    analyst_rows = (build_analyst_payload(candidates, analyst, prices_native, meta, signals=signals)
                    if analyst is not None and not analyst.empty else [])
    analyst_html = render_analyst_signals(analyst_rows, len(candidates)) if (analyst_rows or candidates) else ""
    news_html = render_news(news_items or [])

    latest_date = prices.index[-1].strftime("%d %b %Y")
    built = datetime.now(timezone.utc).strftime("%d %b %Y &middot; %H:%M UTC")

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

    data_dict = build_data_payload(returns, prices, meta, contrib, signals,
                                   prices_native, returns_native)
    for tkr, entry in watchlist_payload.items():
        if tkr not in data_dict:
            data_dict[tkr] = entry
    data_json = json.dumps(data_dict, separators=(",", ":"))
    # JSON-encode the Worker URL so quoting is always correct in the embedded JS,
    # and an unset URL becomes the literal `""` (falsy in the JS branch).
    news_worker_url_js = json.dumps(NEWS_WORKER_URL or "")
    portfolio_json = json.dumps(
        build_portfolio_payload(basket, bench, first_purchase), separators=(",", ":")
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
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
  .palette-toggle{{
    position:absolute;top:18px;right:28px;display:flex;gap:4px;
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
  .container{{position:relative}}
  @media (max-width:700px){{
    .palette-toggle{{position:static;justify-content:flex-end;margin:8px 0 -8px}}
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
  .hero-legend{{display:flex;gap:18px;font-family:var(--font-mono);font-size:11.5px;align-items:center}}
  .leg{{display:flex;align-items:center;gap:6px;color:var(--text-2)}}
  .leg-swatch{{width:14px;height:3px;border-radius:1px}}
  .leg-swatch.basket{{background:var(--accent)}}
  .leg-swatch.spy{{background:var(--text-dim);height:1px;border-top:1px dashed var(--text-dim)}}
  .hero-chart-svg-wrap{{position:relative;width:100%;height:320px}}
  .hero-chart-svg{{width:100%;height:100%;display:block}}
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
  .stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:14px}}
  .stat{{
    background:linear-gradient(180deg,var(--surface) 0%,var(--ink-soft) 100%);
    border:1px solid var(--border);border-radius:12px;padding:16px 20px;position:relative;overflow:hidden;
  }}
  .stat::before{{content:"";position:absolute;top:0;left:0;right:0;height:1px;
    background:linear-gradient(90deg,transparent,rgba(255,255,255,0.06),transparent)}}
  .stat-label{{font-size:10px;color:var(--text-dim);text-transform:uppercase;letter-spacing:0.18em;font-weight:500}}
  .stat-value{{
    font-family:var(--font-display);font-size:36px;font-weight:400;margin-top:6px;line-height:1;
    letter-spacing:-0.015em;font-variant-numeric:tabular-nums;
  }}
  .stat-meta{{font-family:var(--font-mono);font-size:11px;color:var(--text-dim);margin-top:6px}}
  .pos{{color:var(--up)}}
  .neg{{color:var(--down)}}
  .build-info{{margin-top:14px;font-family:var(--font-mono);font-size:11px;color:var(--text-dim)}}
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
  .watchlist-section,.news-section,.analyst-section{{
    background:linear-gradient(180deg,var(--surface) 0%,var(--ink-soft) 100%);
    border:1px solid var(--border);border-radius:12px;padding:18px 20px;
  }}

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
  .an-foot{{font-family:var(--font-mono);font-size:9.5px;color:var(--text-dim);
    text-transform:uppercase;letter-spacing:0.08em}}
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
  .wl-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:10px}}
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
    .wl-grid{{grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:8px}}
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
  .modal-chart-wrap{{position:relative;width:100%;height:340px;background:var(--ink-soft);border:1px solid var(--border);border-radius:10px;padding:16px}}
  .modal-chart{{width:100%;height:100%;display:block}}
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
    .modal-stat-val{{font-size:13px}}
    .modal-chart-wrap{{height:240px;padding:10px}}
  }}
</style>
</head>
<body>
{defs_html}
<div class="container">

<div class="palette-toggle" role="tablist" aria-label="Color palette">
  <button data-palette="default" aria-pressed="true">Default</button>
  <button data-palette="softdark">Soft Dark</button>
  <button data-palette="light">Light</button>
  <button data-palette="bloomberg">Amber</button>
</div>

<header>
  <div class="eyebrow">{n_open} open <span class="dot">&middot;</span> {n_closed} closed <span class="dot">&middot;</span> first buy {first_purchase_str} <span class="dot">&middot;</span> in {BASE_CCY}</div>
  <h1>The basket since <em>October &rsquo;24</em></h1>

  <div class="hero-chart-wrap">
    <div class="hero-chart-head">
      <div class="hero-head-left">
        <div class="hero-title">Basket vs Benchmark</div>
        <div class="hero-sub">time-weighted return &middot; renormalized as positions enter</div>
      </div>
      <div class="hero-legend">
        <div class="leg"><span class="leg-swatch basket"></span>Basket</div>
        <div class="leg"><span class="leg-swatch spy"></span>SPY</div>
      </div>
    </div>
    <div class="hero-chart-svg-wrap">
      <svg class="hero-chart-svg" id="hero-chart" preserveAspectRatio="none"></svg>
      <div class="hero-tip" id="hero-tip" hidden></div>
    </div>
  </div>

  <div class="stats">
    <div class="stat">
      <div class="stat-label">Basket return</div>
      <div class="stat-value {_cls(basket_final)}">{basket_final:+.1f}%</div>
      <div class="stat-meta">since {first_purchase_str}</div>
    </div>
    <div class="stat">
      <div class="stat-label">vs SPY</div>
      <div class="stat-value {_cls(vs_spy)}">{vs_spy:+.1f} pp</div>
      <div class="stat-meta">SPY {spy_final:+.1f}% same period</div>
    </div>
    <div class="stat">
      <div class="stat-label">Top contributor</div>
      <div class="stat-value pos">{best_contrib_pp:+.1f} pp</div>
      <div class="stat-meta">{best_contrib_name} &middot; {BASE_SYMBOL}{best_contrib_wt:,.0f} basis</div>
    </div>
    <div class="stat">
      <div class="stat-label">Top detractor</div>
      <div class="stat-value neg">{worst_contrib_pp:+.1f} pp</div>
      <div class="stat-meta">{worst_contrib_name} &middot; {BASE_SYMBOL}{worst_contrib_wt:,.0f} basis</div>
    </div>
  </div>

  <div class="build-info">
    <span class="live"></span>last close {latest_date} &middot; rebuilt {built}
  </div>
</header>

{detractors_html}

<div class="wl-news-row">
  {analyst_html}
  {news_html}
</div>

<section class="panel active" id="panel-0">
  {render_toolbar(0, n_total)}
  {table_html}
</section>

{regret_html}

<footer>Built locally &middot; data via yfinance &middot; TWR basket vs SPY &middot; click any row for the full chart</footer>

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
    <div class="modal-chart-wrap">
      <svg class="modal-chart" preserveAspectRatio="none"></svg>
      <div class="modal-tip" hidden></div>
    </div>
  </div>
</div>

<script>
const DATA = {data_json};
const PORTFOLIO = {portfolio_json};

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

// ---- Hero chart (basket + SPY)
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
  if (!b.values.length) {{ svg.innerHTML = '<text x="50%" y="50%" fill="#6b7185" font-family="Geist Mono" font-size="12" text-anchor="middle">No basket data</text>'; return; }}

  // Combined min/max across both series
  const allVals = [...b.values, ...s.values, 0];
  const vmin = Math.min(...allVals);
  const vmax = Math.max(...allVals);
  const span = (vmax - vmin) || 1;
  const padL = 48, padR = 56, padT = 18, padB = 32;
  const innerW = W - padL - padR;
  const innerH = H - padT - padB;

  function buildPoints(series) {{
    if (!series.values.length) return {{xs:[], ys:[], dates:[], vals:[]}};
    const n = series.values.length;
    const xs = series.values.map((_, i) => padL + (n === 1 ? innerW/2 : (i/(n-1)) * innerW));
    const ys = series.values.map(v => padT + (1 - (v - vmin)/span) * innerH);
    return {{xs, ys, dates: series.dates, vals: series.values}};
  }}
  const basket = buildPoints(b);
  const spy = buildPoints(s);

  // Y ticks (5 levels)
  const yTicks = [];
  for (let i = 0; i <= 5; i++) {{
    const v = vmin + (i/5) * span;
    const y = padT + (1 - (v - vmin)/span) * innerH;
    yTicks.push({{v, y}});
  }}
  // X ticks (~5)
  const n = basket.xs.length;
  const xTickCount = Math.min(5, n);
  const xTicks = [];
  for (let i = 0; i < xTickCount; i++) {{
    const idx = Math.round((i/(xTickCount-1)) * (n-1));
    xTicks.push({{idx, x: basket.xs[idx], date: basket.dates[idx]}});
  }}

  const basketPL = basket.xs.map((x, i) => `${{x.toFixed(1)}},${{basket.ys[i].toFixed(1)}}`).join(' ');
  const spyPL = spy.xs.map((x, i) => `${{x.toFixed(1)}},${{spy.ys[i].toFixed(1)}}`).join(' ');
  const zeroY = padT + (1 - (0 - vmin)/span) * innerH;

  const basketEnd = basket.vals[basket.vals.length - 1];
  const spyEnd = spy.vals.length ? spy.vals[spy.vals.length - 1] : 0;
  const basketColor = '#f59e0b';
  const spyColor = '#6b7185';

  // Area fill for basket
  const areaD = `M ${{basket.xs[0].toFixed(1)}},${{(padT + innerH).toFixed(1)}} ` +
                basket.xs.map((x, i) => `L ${{x.toFixed(1)}},${{basket.ys[i].toFixed(1)}}`).join(' ') +
                ` L ${{basket.xs[n-1].toFixed(1)}},${{(padT + innerH).toFixed(1)}} Z`;

  let html = '';
  // Y grid
  html += yTicks.map(t =>
    `<line x1="${{padL}}" y1="${{t.y.toFixed(1)}}" x2="${{padL + innerW}}" y2="${{t.y.toFixed(1)}}" stroke="rgba(255,255,255,0.04)" stroke-width="1"/>` +
    `<text x="${{padL - 8}}" y="${{(t.y + 3.5).toFixed(1)}}" fill="#6b7185" font-size="10" font-family="Geist Mono, monospace" text-anchor="end">${{t.v >= 0 ? '+' : ''}}${{t.v.toFixed(0)}}%</text>`
  ).join('');
  // Zero line
  html += `<line x1="${{padL}}" y1="${{zeroY.toFixed(1)}}" x2="${{padL + innerW}}" y2="${{zeroY.toFixed(1)}}" stroke="rgba(255,255,255,0.18)" stroke-width="0.8" stroke-dasharray="3 3"/>`;
  // X labels
  html += xTicks.map(t =>
    `<text x="${{t.x.toFixed(1)}}" y="${{(padT + innerH + 18).toFixed(1)}}" fill="#6b7185" font-size="10" font-family="Geist Mono, monospace" text-anchor="middle">${{fmtDate(t.date)}}</text>`
  ).join('');
  // Basket area
  html += `<path d="${{areaD}}" fill="url(#grad-amber-lg)"/>`;
  // SPY line (dashed)
  if (spy.xs.length) {{
    html += `<polyline points="${{spyPL}}" fill="none" stroke="${{spyColor}}" stroke-width="1.4" stroke-dasharray="4 3" stroke-linejoin="round"/>`;
  }}
  // Basket line
  html += `<polyline points="${{basketPL}}" fill="none" stroke="${{basketColor}}" stroke-width="2.2" stroke-linejoin="round" stroke-linecap="round"/>`;

  // End labels
  html += `<text x="${{(padL + innerW + 6).toFixed(1)}}" y="${{(basket.ys[n-1] + 4).toFixed(1)}}" fill="${{basketColor}}" font-size="11" font-family="Geist Mono, monospace" font-weight="500">${{basketEnd >= 0 ? '+' : ''}}${{basketEnd.toFixed(1)}}%</text>`;
  if (spy.ys.length) {{
    html += `<text x="${{(padL + innerW + 6).toFixed(1)}}" y="${{(spy.ys[spy.ys.length-1] + 4).toFixed(1)}}" fill="${{spyColor}}" font-size="11" font-family="Geist Mono, monospace">${{spyEnd >= 0 ? '+' : ''}}${{spyEnd.toFixed(1)}}%</text>`;
  }}

  // Crosshair
  html += `<line class="hero-cross" x1="0" y1="${{padT}}" x2="0" y2="${{padT + innerH}}" stroke="${{basketColor}}" stroke-width="0.8" stroke-dasharray="2 3" opacity="0"/>`;
  html += `<circle class="hero-dot-basket" cx="0" cy="0" r="4" fill="${{basketColor}}" opacity="0"/>`;
  html += `<circle class="hero-dot-spy" cx="0" cy="0" r="3.5" fill="${{spyColor}}" opacity="0"/>`;

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
    tip.innerHTML =
      `<div class="tip-date">${{fmtDate(basket.dates[bestI])}}</div>` +
      `<div class="tip-row"><span class="tip-label">Basket</span><span class="${{bv >= 0 ? 'pos' : 'neg'}}">${{bv >= 0 ? '+' : ''}}${{bv.toFixed(2)}}%</span></div>` +
      (sv !== null ? `<div class="tip-row"><span class="tip-label">SPY</span><span class="${{sv >= 0 ? 'pos' : 'neg'}}">${{sv >= 0 ? '+' : ''}}${{sv.toFixed(2)}}%</span></div>` : '');
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
document.querySelector('#ret-table th[data-col="6"]')?.click();

// ---- Filtering
const TOTALS = Object.fromEntries(Object.entries(DATA).map(([t, d]) => [t, d.total]));
const WEIGHTS = Object.fromEntries(Object.entries(DATA).map(([t, d]) => [t, d.weight]));

function applyFilter(panel) {{
  const search = (panel.querySelector('.search')?.value || '').trim().toUpperCase();
  const activeChip = panel.querySelector('.chip.active');
  const mode = activeChip ? activeChip.dataset.filter : 'all';
  const sorted = Object.entries(TOTALS).sort((a, b) => b[1] - a[1]);
  let allowed = null;
  if (mode === 'top10') allowed = new Set(sorted.slice(0, 10).map(([t]) => t));
  else if (mode === 'bottom10') allowed = new Set(sorted.slice(-10).map(([t]) => t));

  const items = panel.querySelectorAll('#ret-table tbody tr');
  items.forEach(el => {{
    const t = el.dataset.ticker;
    const total = parseFloat(el.dataset.total);
    const weight = parseFloat(el.dataset.weight) || 0;
    let show = true;
    if (search && !t.includes(search)) show = false;
    if (mode === 'basket' && weight <= 0) show = false;
    if (mode === 'closed' && weight > 0) show = false;
    if (mode === 'winners' && total < 0) show = false;
    if (mode === 'losers' && total >= 0) show = false;
    if (allowed && !allowed.has(t)) show = false;
    el.classList.toggle('hidden', !show);
  }});
}}

document.querySelectorAll('.panel').forEach(panel => {{
  panel.querySelectorAll('.chip').forEach(chip => {{
    chip.addEventListener('click', (e) => {{
      e.stopPropagation();
      panel.querySelectorAll('.chip').forEach(c => c.classList.remove('active'));
      chip.classList.add('active');
      applyFilter(panel);
    }});
  }});
  const s = panel.querySelector('.search');
  if (s) {{
    s.addEventListener('input', () => applyFilter(panel));
    s.addEventListener('click', (e) => e.stopPropagation());
  }}
}});

// ---- Modal
const modal = document.getElementById('modal');
const modalSvg = modal.querySelector('.modal-chart');
const modalTip = modal.querySelector('.modal-tip');
const modalChartWrap = modal.querySelector('.modal-chart-wrap');

let currentTicker = null;
let chartPoints = null;

function openModal(ticker) {{
  const d = DATA[ticker];
  if (!d) return;
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
  const sinceLabel = (d.status === 'watch') ? 'last 12 months' : ('since ' + fmtDate(d.baseline_date));
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
  modal.querySelectorAll('.modal-stat-val').forEach(el => {{
    const k = el.dataset.key;
    el.textContent = vals[k];
    el.className = 'modal-stat-val';
    if (k.match(/^(w1|m1|m3|ytd)$/) && d[k] !== null && d[k] !== undefined && !Number.isNaN(d[k])) {{
      el.classList.add(d[k] >= 0 ? 'pos' : 'neg');
    }}
  }});
  modal.removeAttribute('hidden');
  document.body.classList.add('modal-open');
  requestAnimationFrame(() => renderBigChart(ticker));
}}

function closeModal() {{
  modal.setAttribute('hidden', '');
  document.body.classList.remove('modal-open');
  modalTip.setAttribute('hidden', '');
}}

function renderBigChart(ticker) {{
  currentTicker = ticker;
  const d = DATA[ticker];
  const rect = modalChartWrap.getBoundingClientRect();
  const W = Math.max(rect.width - 32, 300);
  const H = Math.max(rect.height - 32, 200);
  modalSvg.setAttribute('viewBox', `0 0 ${{W}} ${{H}}`);
  modalSvg.setAttribute('width', W);
  modalSvg.setAttribute('height', H);

  const prices = d.prices;
  const dates = d.dates;
  const baseline = d.baseline;
  const rebased = prices.map(p => (p / baseline - 1) * 100);

  const vmin = Math.min(0, ...rebased);
  const vmax = Math.max(0, ...rebased);
  const span = (vmax - vmin) || 1;
  const padL = 52, padR = 14, padT = 14, padB = 34;
  const innerW = W - padL - padR;
  const innerH = H - padT - padB;
  const n = rebased.length;

  const xs = rebased.map((_, i) => padL + (n === 1 ? innerW/2 : (i/(n-1)) * innerW));
  const ys = rebased.map(v => padT + (1 - (v - vmin)/span) * innerH);
  const zeroY = padT + (1 - (0 - vmin)/span) * innerH;
  const isUp = d.total >= 0;
  const color = isUp ? '#34d399' : '#f87171';
  const gradId = isUp ? 'grad-up-lg' : 'grad-down-lg';

  const yTicks = [];
  for (let i = 0; i <= 5; i++) {{
    const v = vmin + (i/5) * span;
    const y = padT + (1 - (v - vmin)/span) * innerH;
    yTicks.push({{v, y}});
  }}
  const xTickCount = Math.min(5, n);
  const xTicks = [];
  for (let i = 0; i < xTickCount; i++) {{
    const idx = Math.round((i/(xTickCount-1)) * (n-1));
    xTicks.push({{idx, x: xs[idx], date: dates[idx]}});
  }}

  const pl = xs.map((x, i) => `${{x.toFixed(1)}},${{ys[i].toFixed(1)}}`).join(' ');
  const areaD = `M ${{xs[0].toFixed(1)}},${{(padT + innerH).toFixed(1)}} ` +
                xs.map((x, i) => `L ${{x.toFixed(1)}},${{ys[i].toFixed(1)}}`).join(' ') +
                ` L ${{xs[n-1].toFixed(1)}},${{(padT + innerH).toFixed(1)}} Z`;

  let html = '';
  html += yTicks.map(t =>
    `<line x1="${{padL}}" y1="${{t.y.toFixed(1)}}" x2="${{padL + innerW}}" y2="${{t.y.toFixed(1)}}" stroke="rgba(255,255,255,0.04)" stroke-width="1"/>` +
    `<text x="${{padL - 8}}" y="${{(t.y + 3.5).toFixed(1)}}" fill="#6b7185" font-size="10" font-family="Geist Mono, monospace" text-anchor="end">${{t.v >= 0 ? '+' : ''}}${{t.v.toFixed(0)}}%</text>`
  ).join('');
  html += `<line x1="${{padL}}" y1="${{zeroY.toFixed(1)}}" x2="${{padL + innerW}}" y2="${{zeroY.toFixed(1)}}" stroke="rgba(255,255,255,0.18)" stroke-width="0.8" stroke-dasharray="3 3"/>`;
  html += xTicks.map(t =>
    `<text x="${{t.x.toFixed(1)}}" y="${{(padT + innerH + 18).toFixed(1)}}" fill="#6b7185" font-size="10" font-family="Geist Mono, monospace" text-anchor="middle">${{fmtDate(t.date)}}</text>`
  ).join('');
  html += `<path d="${{areaD}}" fill="url(#${{gradId}})"/>`;
  html += `<polyline points="${{pl}}" fill="none" stroke="${{color}}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
  html += `<line class="crosshair" x1="0" y1="${{padT}}" x2="0" y2="${{padT + innerH}}" stroke="${{color}}" stroke-width="0.8" stroke-dasharray="2 3" opacity="0"/>`;
  html += `<circle class="dot" cx="0" cy="0" r="4" fill="${{color}}" stroke="${{color}}" opacity="0"/>`;

  modalSvg.innerHTML = html;
  chartPoints = {{xs, ys, dates, prices, rebased, padL, padT, innerW, innerH, vmin, span, baseline, color}};

  // Transaction markers (buy/sell dots) — only if the per-stock transactions are available
  if (d.transactions && d.transactions.length > 0) {{
    let markerHtml = '';
    for (const t of d.transactions) {{
      const txnTime = new Date(t.date).getTime();
      let bestIdx = 0, bestDiff = Infinity;
      for (let i = 0; i < dates.length; i++) {{
        const diff = Math.abs(new Date(dates[i]).getTime() - txnTime);
        if (diff < bestDiff) {{ bestDiff = diff; bestIdx = i; }}
      }}
      const mx = xs[bestIdx];
      const isBuy = t.action === 'BUY';
      const mColor = isBuy ? '#34d399' : '#f87171';
      const label = isBuy ? 'B' : 'S';
      const labelY = padT + innerH - 5;
      markerHtml += `<line x1="${{mx.toFixed(1)}}" y1="${{padT.toFixed(1)}}" x2="${{mx.toFixed(1)}}" y2="${{(padT + innerH).toFixed(1)}}" stroke="${{mColor}}" stroke-width="0.7" stroke-dasharray="3 2" opacity="0.45"/>`;
      markerHtml += `<circle cx="${{mx.toFixed(1)}}" cy="${{labelY.toFixed(1)}}" r="6" fill="${{mColor}}" stroke="#0b0e17" stroke-width="0.5"/>`;
      markerHtml += `<text x="${{mx.toFixed(1)}}" y="${{(labelY + 3).toFixed(1)}}" fill="#0b0e17" font-size="8.5" font-weight="700" text-anchor="middle" font-family="Geist Mono, monospace">${{label}}</text>`;
    }}
    modalSvg.insertAdjacentHTML('beforeend', markerHtml);
  }}
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
document.addEventListener('keydown', (e) => {{ if (e.key === 'Escape') closeModal(); }});
window.addEventListener('resize', () => {{ if (currentTicker && !modal.hasAttribute('hidden')) renderBigChart(currentTicker); }});

document.querySelectorAll('#ret-table tbody tr').forEach(row => {{
  row.addEventListener('click', () => openModal(row.dataset.ticker));
}});
document.querySelectorAll('.contrib-table tbody tr, .regret-table tbody tr, .dt-table tbody tr').forEach(row => {{
  row.addEventListener('click', () => openModal(row.dataset.ticker));
}});
document.querySelectorAll('.wl-card, .an-card').forEach(card => {{
  card.addEventListener('click', () => openModal(card.dataset.ticker));
}});

// ---- Palette toggle ---------------------------------------------------
// Body class controls which set of CSS variables wins. Persist the choice
// across visits via localStorage so the page remembers the user's preference.
(function setupPalette() {{
  const PALETTE_KEY = 'stocks-dashboard-palette';
  const buttons = document.querySelectorAll('.palette-toggle button');
  function apply(name) {{
    document.body.classList.remove('palette-softdark','palette-light','palette-bloomberg');
    if (name && name !== 'default') document.body.classList.add('palette-' + name);
    buttons.forEach(b => {{
      const active = b.dataset.palette === name;
      b.classList.toggle('active', active);
      b.setAttribute('aria-pressed', String(active));
    }});
    try {{ localStorage.setItem(PALETTE_KEY, name); }} catch (e) {{ /* private mode */ }}
  }}
  const saved = (() => {{ try {{ return localStorage.getItem(PALETTE_KEY); }} catch (e) {{ return null; }} }})();
  apply(saved || 'default');
  buttons.forEach(b => b.addEventListener('click', () => apply(b.dataset.palette)));
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
    return `<a class="news-row" data-source="${{escapeNewsHtml(it.source)}}" href="${{escapeNewsHtml(it.link)}}" target="_blank" rel="noopener noreferrer">`
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
  chipBar.querySelectorAll('.news-chip').forEach(b => {{
    b.classList.toggle('active', b.dataset.src === src);
  }});
  document.querySelectorAll('.news-row').forEach(row => {{
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

refreshNewsFromWorker();
</script>

</body>
</html>
"""


def main() -> None:
    # Prefer the real broker log if present; fall back to the demo CSV otherwise.
    if LOG_XLSX.exists():
        print(f"Loading transactions from {LOG_XLSX}")
        transactions, untracked = load_transactions_from_log()
        if not untracked.empty:
            print(f"  {len(untracked)} untracked manual-fund rows "
                  f"(no ticker/ISIN) — listed separately in the dashboard")
    else:
        print(f"Loading transactions from {TRANSACTIONS_CSV}")
        transactions = load_transactions()
        untracked = pd.DataFrame()
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
    prices_native = download_prices(ticker_list)
    print(f"Got native prices: {prices_native.shape[0]} rows x {prices_native.shape[1]} tickers")

    print(f"Pulling benchmark {BENCHMARK}...")
    bench_native = download_benchmark()

    CACHE_PARQUET.parent.mkdir(parents=True, exist_ok=True)
    try:
        prices_native.to_parquet(CACHE_PARQUET)
        if not bench_native.empty:
            bench_native.to_frame().to_parquet(BENCHMARK_CACHE)
        print(f"Cached native prices to {CACHE_PARQUET}")
    except ImportError:
        prices_native.to_csv(CACHE_PARQUET.with_suffix(".csv"))

    meta_cache = load_meta_cache()
    meta = fetch_meta(list(prices_native.columns), meta_cache)

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

    returns = build_positions(transactions, prices)
    returns_native = build_positions(transactions, prices_native)
    print(f"Built {len(returns)} positions (base={BASE_CCY}) — "
          f"{int((returns.status == 'open').sum())} open, "
          f"{int((returns.status == 'closed').sum())} closed")

    basket = compute_basket_mtm_series(transactions, prices)
    print(f"Basket series ({BASE_CCY}): {len(basket)} daily points "
          f"(range {basket.min():+.2f} to {basket.max():+.2f})")

    first_purchase = pd.Timestamp(transactions[transactions.action == "BUY"].date.min())
    bench_series = compute_benchmark_series(bench, first_purchase)
    print(f"Benchmark {BENCHMARK} series ({BASE_CCY}): {len(bench_series)} points")

    contrib = compute_contributors(returns)
    print(f"Contributors: top {contrib.iloc[0].name} ({contrib.iloc[0].contribution_pp:+.2f} pp), "
          f"worst {contrib.iloc[-1].name} ({contrib.iloc[-1].contribution_pp:+.2f} pp)")

    signals = compute_signals(prices)
    sig_counts = signals.signal.value_counts()
    print(f"Signals: {dict(sig_counts.head(5))}")

    # Analyst panel: candidates = closed positions (stocks previously held).
    # Treats the panel as "should I buy back in?" rather than "what to add."
    analyst_candidates = sorted(returns[returns.status == "closed"].index.tolist()) \
        if not returns.empty else []
    analyst_cache = load_analyst_cache()
    analyst = fetch_analyst_data(analyst_candidates, analyst_cache) if analyst_candidates else analyst_cache

    news_items = fetch_news()

    html = render_html(returns, prices, meta, basket, bench_series, contrib, transactions,
                       signals, prices_native, returns_native, untracked=untracked,
                       watchlist=watchlist, news_items=news_items, analyst=analyst,
                       analyst_candidates=analyst_candidates)
    OUT_HTML.parent.mkdir(parents=True, exist_ok=True)
    OUT_HTML.write_text(html, encoding="utf-8")
    print(f"Wrote {OUT_HTML} ({OUT_HTML.stat().st_size/1024:.1f} KB)")


if __name__ == "__main__":
    main()
