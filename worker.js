// inflation.watch — optional Cloudflare Worker in front of the site.
// Serves the site from the origin as usual; when the origin times out or answers 5xx,
// serves the last cached copy of the page, or fallback.html, so visitors always get the
// last known figures with their time of last update.
//
// Deploy: Workers & Pages → Create Worker → paste → add a route for your domain (e.g. inflation.watch/*)
// and set ORIGIN to where the files live (Cloudflare Pages URL or your server).
// Free plan: 100,000 requests/day; the $5 paid plan is 10M/month. Not needed if Pages alone serves the site.

const ORIGIN = "https://inflation-watch.pages.dev";   // change me
const TIMEOUT_MS = 8000;

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const upstream = ORIGIN + url.pathname + url.search;
    const cache = caches.default;
    try {
      const ctl = new AbortController();
      const t = setTimeout(() => ctl.abort(), TIMEOUT_MS);
      const res = await fetch(upstream, { headers: request.headers, signal: ctl.signal, cf: { cacheTtl: 60 } });
      clearTimeout(t);
      if (res.ok || res.status === 304) {
        if (request.method === "GET" && res.ok) ctx.waitUntil(cache.put(request, res.clone()));
        return res;
      }
      throw new Error("origin " + res.status);
    } catch (e) {
      const cached = await cache.match(request);
      if (cached) return withNote(cached, "served from the edge cache: the origin is unavailable");
      const fb = await cache.match(new Request(ORIGIN + "/fallback.html")) || await fetch(ORIGIN + "/fallback.html").catch(() => null);
      if (fb && fb.ok) return withNote(new Response(fb.body, { status: 503, headers: { "content-type": "text/html; charset=utf-8", "retry-after": "120" } }), "fallback page");
      return new Response("inflation.watch is temporarily unavailable. Try again in a minute.", { status: 503, headers: { "retry-after": "120" } });
    }
  }
};
function withNote(res, note) { const r = new Response(res.body, res); r.headers.set("x-inflation-watch", note); return r; }
