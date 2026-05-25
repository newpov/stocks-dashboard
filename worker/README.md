# News-proxy Worker

Tiny Cloudflare Worker that proxies finance RSS feeds with CORS headers, so the
dashboard can fetch fresh news directly from the browser on every page load
(instead of being frozen until the next CI rebuild).

- **Zero dependencies** (single `index.js`, regex-based RSS parser).
- **Allowlisted** to two feeds (Yahoo Finance + MarketWatch). Not an open CORS proxy.
- **Edge-cached for 10 minutes**: most visitors hit Cloudflare's cache, not the origin feed.
- **Free** within Cloudflare's 100,000 requests/day Workers tier.

## One-time deploy (~5 minutes)

1. **Sign up for Cloudflare** (free, no card required): https://dash.cloudflare.com/sign-up
2. **Install Wrangler** (Cloudflare's CLI):
   ```
   npm install -g wrangler
   ```
   (needs Node 18+)
3. **Log in:**
   ```
   wrangler login
   ```
   Opens a browser window — confirm the OAuth prompt.
4. **Deploy from this folder:**
   ```
   cd worker
   wrangler deploy
   ```
   Wrangler prints something like:
   ```
   Published stocks-dashboard-news (1.23 sec)
     https://stocks-dashboard-news.<your-subdomain>.workers.dev
   ```
5. **Copy that URL** and paste it into `NEWS_WORKER_URL` near the top of
   [`../build.py`](../build.py). Append `/news` (the Worker's route), e.g.
   `https://stocks-dashboard-news.example.workers.dev/news`.
6. **Rebuild and push:**
   ```
   cd ..
   python build.py
   git add docs/ build.py
   git commit -m "Enable live news via Cloudflare Worker"
   git push
   ```

That's it. The next time anyone opens the dashboard, the news box loads live
headlines via your Worker instead of whatever was baked at build time.

## How it works

```
Browser
   │
   │ 1. Loads docs/index.html (static, from GitHub Pages)
   │     — initial paint shows build-time news as a fallback
   │
   │ 2. JS calls GET https://<your>.workers.dev/news
   ▼
Worker (Cloudflare edge)
   │   → Returns cached JSON if cache is < 10 min old
   │   → Otherwise: fetch Yahoo + MarketWatch RSS, parse, dedupe,
   │     return JSON like {items: [...], fetched_at: "..."}
   │
   │ 3. JS replaces .news-list with the live items
   │     and updates the "as of" timestamp
   ▼
Page shows live news, no further requests.
```

If the Worker is unreachable for any reason (Cloudflare outage, DNS, the URL
hasn't been set yet), the page silently keeps showing the build-time fallback.
No broken state.

## Updating the feed list

Edit the `FEEDS` array in [`index.js`](index.js) — add RSS URLs you want and
re-deploy with `wrangler deploy`. The allowlist is intentional; don't accept
URLs from query parameters or you'll have built an open proxy.

## Cost

Zero unless you exceed 100,000 Worker requests/day. For a personal dashboard
with a few visits/day this is essentially impossible to hit — the math is
~3M req/month free, and you'd use maybe 50.

## Logs and debugging

After deploy: https://dash.cloudflare.com → Workers & Pages → your Worker →
Logs. With `observability.enabled = true` (set in `wrangler.toml`) you can
tail live logs from the dashboard or via `wrangler tail`.
