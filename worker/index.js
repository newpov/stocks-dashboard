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

const FEEDS = [
  { source: "Yahoo Finance", url: "https://finance.yahoo.com/news/rssindex" },
  { source: "MarketWatch",   url: "https://feeds.content.dowjones.io/public/rss/mw_topstories" },
];

const MAX_ITEMS = 12;
const CACHE_TTL_SECONDS = 600;   // 10 min — RSS publishers don't update faster

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

function extractTag(xml, tag) {
  const re = new RegExp(`<${tag}(?:\\s[^>]*)?>([\\s\\S]*?)</${tag}>`, "i");
  const m = xml.match(re);
  if (!m) return "";
  let v = m[1].trim();
  // Unwrap CDATA if present.
  const cdata = v.match(/^<!\[CDATA\[([\s\S]*?)\]\]>$/);
  if (cdata) v = cdata[1];
  // Decode the common HTML entities (titles often carry &amp;, &#x27; etc.)
  return v
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&apos;/g, "'")
    .replace(/&#x27;/g, "'")
    .replace(/&#39;/g, "'");
}
