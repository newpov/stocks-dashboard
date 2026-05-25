// News-proxy Cloudflare Worker.
//
// Fetches a hardcoded allowlist of finance RSS feeds server-side, parses them
// into a small JSON payload, returns it with CORS headers so the dashboard
// (served from newpov.github.io) can call this from the browser. The
// allowlist matters: without it the Worker would be an open CORS proxy that
// could be abused to fetch arbitrary URLs.
//
// Edge-cached for 10 minutes — at typical traffic this means a couple of
// origin fetches per hour to each feed, not per visitor.

// Finance-focused feed allowlist. Narrower than "top stories" feeds, so
// irrelevant general/politics headlines don't leak in. Add/remove freely.
const FEEDS = [
  { source: "MarketWatch",   url: "https://feeds.content.dowjones.io/public/rss/mw_marketpulse" },
  { source: "MarketWatch",   url: "https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines" },
  { source: "Motley Fool",   url: "https://www.fool.com/feeds/index.aspx" },
  { source: "CNBC Business", url: "https://www.cnbc.com/id/10001147/device/rss/rss.html" },
  { source: "Yahoo Finance", url: "https://finance.yahoo.com/rss/topstories" },
];

const MAX_ITEMS = 30;   // returned to the browser; the UI filters down to ~6 visible
const CACHE_TTL_SECONDS = 600;   // 10 min — RSS publishers don't update faster

// Second-line defence: skip headlines whose title doesn't look finance-shaped.
// Cheap word filter — adjust the list to taste. An item passes if ANY token
// appears (case-insensitive). Empty title or off-topic titles are dropped.
const FINANCE_KEYWORDS = [
  "stock", "shares", "market", "rally", "earnings", "buy", "sell", "trade",
  "trader", "trading", "invest", "fund", "etf", "bond", "yield", "rate",
  "fed ", "dividend", "profit", "loss", "revenue", "guidance", "ipo", "merger",
  "acquisition", "valuation", "p/e ", "eps", "options", "futures", "sp500",
  "s&p", "nasdaq", "dow ", "ftse", "ceo", "cfo", "upgrade", "downgrade",
  "analyst", "price target", "outlook", "forecast", "treasury", "inflation",
  "recession", "bull", "bear", "crypto", "bitcoin", "ethereum", "$",
];

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
  "Access-Control-Max-Age": "86400",
};

export default {
  async fetch(request, env, ctx) {
    if (request.method === "OPTIONS") {
      return new Response(null, { headers: CORS_HEADERS });
    }
    if (request.method !== "GET") {
      return jsonResponse({ error: "Method not allowed" }, 405);
    }
    const url = new URL(request.url);
    if (url.pathname !== "/news") {
      return jsonResponse({ error: "Not found", hint: "GET /news" }, 404);
    }

    // Edge cache: serve from cache when we have a fresh entry.
    const cache = caches.default;
    const cacheKey = new Request(url.toString(), { method: "GET" });
    const cached = await cache.match(cacheKey);
    if (cached) return cached;

    const perFeed = await Promise.all(FEEDS.map(async (f) => {
      try {
        const resp = await fetch(f.url, {
          cf: { cacheTtl: CACHE_TTL_SECONDS, cacheEverything: true },
          headers: { "User-Agent": "stocks-dashboard-news-proxy/1.0 (+github.com/newpov/stocks-dashboard)" },
        });
        if (!resp.ok) return [];
        const xml = await resp.text();
        return parseRss(xml, f.source);
      } catch (e) {
        return [];
      }
    }));

    const merged = perFeed.flat();
    const seen = new Set();
    const deduped = [];
    for (const item of merged) {
      if (seen.has(item.link)) continue;
      if (!isFinanceRelevant(item.title)) continue;
      seen.add(item.link);
      deduped.push(item);
    }
    deduped.sort((a, b) => new Date(b.published) - new Date(a.published));
    const items = deduped.slice(0, MAX_ITEMS);

    const response = jsonResponse({
      items,
      fetched_at: new Date().toISOString(),
      source_count: FEEDS.length,
    }, 200, { "Cache-Control": `public, max-age=${CACHE_TTL_SECONDS}` });

    // Stash in edge cache so subsequent visitors within the TTL don't trigger
    // any upstream fetches.
    ctx.waitUntil(cache.put(cacheKey, response.clone()));
    return response;
  },
};

function jsonResponse(body, status = 200, extraHeaders = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "Content-Type": "application/json",
      ...CORS_HEADERS,
      ...extraHeaders,
    },
  });
}

// Tiny RSS 2.0 parser — extracts <item> blocks with title, link, pubDate.
// Robust enough for the major-publisher feeds we proxy; not a general XML parser.
function parseRss(xml, source) {
  const items = [];
  const itemMatches = xml.match(/<item[\s>][\s\S]*?<\/item>/g) || [];
  for (const block of itemMatches) {
    const title = extractTag(block, "title");
    const link = extractTag(block, "link");
    const pubDate = extractTag(block, "pubDate") || extractTag(block, "dc:date") || extractTag(block, "published");
    if (!title || !link) continue;
    let publishedIso;
    try {
      publishedIso = pubDate ? new Date(pubDate).toISOString() : new Date().toISOString();
    } catch {
      publishedIso = new Date().toISOString();
    }
    items.push({ title: title.trim(), link: link.trim(), source, published: publishedIso });
  }
  return items;
}

function isFinanceRelevant(title) {
  const t = (title || "").toLowerCase();
  if (!t) return false;
  for (const kw of FINANCE_KEYWORDS) {
    if (t.includes(kw)) return true;
  }
  return false;
}

function extractTag(xml, tag) {
  const re = new RegExp(`<${tag}(?:\\s[^>]*)?>([\\s\\S]*?)</${tag}>`, "i");
  const m = xml.match(re);
  if (!m) return "";
  let v = m[1].trim();
  // Unwrap CDATA if present.
  const cdata = v.match(/^<!\[CDATA\[([\s\S]*?)\]\]>$/);
  if (cdata) v = cdata[1];
  return decodeEntities(v);
}

// Decode HTML entities. Handles:
//   1. Numeric hex   — &#x2018; → '
//   2. Numeric dec   — &#8217;  → '
//   3. Common named  — &amp; &lt; &gt; &quot; &apos; &nbsp; etc.
// Order matters: numeric first, then named, so a decimal-encoded ampersand
// (e.g. &#38;amp;) decodes correctly to &amp; → &.
function decodeEntities(s) {
  if (!s) return "";
  // Hex numeric — &#x2018;
  s = s.replace(/&#x([0-9a-fA-F]+);/g, (_, hex) => {
    try { return String.fromCodePoint(parseInt(hex, 16)); }
    catch { return _; }
  });
  // Decimal numeric — &#8217;
  s = s.replace(/&#([0-9]+);/g, (_, dec) => {
    try { return String.fromCodePoint(parseInt(dec, 10)); }
    catch { return _; }
  });
  // Named entities — the handful that show up in finance RSS titles.
  const named = {
    "amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'",
    "nbsp": " ", "ndash": "–", "mdash": "—",
    "lsquo": "‘", "rsquo": "’",
    "ldquo": "“", "rdquo": "”",
    "hellip": "…", "trade": "™", "copy": "©", "reg": "®",
  };
  s = s.replace(/&([a-zA-Z]+);/g, (orig, name) => name in named ? named[name] : orig);
  return s;
}
