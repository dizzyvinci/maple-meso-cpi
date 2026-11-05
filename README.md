# inflation.watch

A consumer price index for the MapleStory Classic World Free Market, plus the NX-to-meso rate, as a static site that rebuilds itself.

## What is in here

| File | Role |
|---|---|
| `build.py` | The whole pipeline: fetches the sources, computes the Meso CPI and NX rate, writes `index.html` and `data.json`. |
| `template.html` | The page. `build.py` injects the data blob into it. Edit this to change the look. |
| `index.html` | The built site. Serve this. |
| `data.json` | The same numbers as JSON. An open page checks it every minute and reloads when a newer build lands. |
| `market.json` | Last copy of the maplestory-classic.com daily feed. |
| `intraday.json` | Ledger of every mesos.watch poll (14 days kept, 7 embedded in the page). |
| `ticks.json` | One row per daily-feed refresh seen. |
| `mw-ids.json` | mesos.watch item ids, built once from its category pages. |
| `version.json` | ~150 bytes: build time and the headline figures. Open tabs poll this, not `data.json`. |
| `fallback.html` / `404.html` | Last known figures with the time of last update, no fonts or scripts. Serve it when the site or its sources fail. |
| `.github/workflows/build.yml` | Rebuilds every 10 minutes on GitHub Actions and commits the result for GitHub Pages. |

## Sources and permissions

- **maplestory-classic.com `market.json`**: published for developers. Daily median sale prices since Founder's Access; this is the daily series and the launch-week base.
- **mesos.watch item pages**: its scanners log Free Market sales as they happen. Its public rules say the data serves the site only, so the live poll runs **only because the site owner has mesos.watch's permission**, identifies itself in every request (`UA` in `build.py`) and sleeps half a second between pages. Keep it that way; set `MW_ENABLED = False` to turn it off.
- **NiaMeowDB NX:Meso indicator**: one credited figure per build, which its reuse policy allows. A full feed needs their written OK (contact@meowdb.com).
- Not used, because their terms forbid automated collection: meso.watch, maplestory.watch.

## Run it

```
python3 build.py                 # fetch everything and build
python3 build.py --offline       # rebuild from the last market.json
python3 build.py --no-mesoswatch # embed the last polls without polling
```

Cron, every 10 minutes:

```
*/10 * * * * cd /srv/inflation-watch && python3 build.py >> build.log 2>&1
```

## Deploy on your accounts

Target setup: the repo at **github.com/dizzyvinci/inflation-watch**, GitHub Actions rebuilding every 10 minutes, **Cloudflare Pages** serving it on inflation.watch from the same Cloudflare account.

1. Create the empty repo on GitHub, then from this folder:
   `git init && git add -A && git commit -m "inflation.watch" && git branch -M main && git remote add origin git@github.com:dizzyvinci/inflation-watch.git && git push -u origin main`
2. Repo → Settings → Actions → General → Workflow permissions → "Read and write". The workflow then runs every 10 minutes and commits the built files.
3. Cloudflare → Workers & Pages → Create → Pages → Connect to Git → pick the repo. Framework preset "None", build command empty, output directory `/`. Every commit from the workflow deploys automatically (about a minute).
4. Pages project → Custom domains → add `inflation.watch` (the DNS record is created for you when the zone is in the same account). Unlimited bandwidth, global cache, gzip/brotli on by default.
5. Optional: `worker.js` in front of the domain serves the last cached page or `fallback.html` whenever the origin fails, so a visitor at the worst moment still sees the last update time. Pages alone almost never needs it.

## Deploy

**GitHub Pages (simplest, free):** push this folder to a repo, enable Pages from the `main` branch root, and the included workflow keeps it fresh every 10 minutes (GitHub sometimes delays scheduled runs by a few minutes). Point your domain at Pages and add a `CNAME` file with the domain.

**Your own server:** any static host or VPS. Put the folder under nginx/Caddy, add the cron line, done. The page needs no backend; `data.json` next to `index.html` is what makes an open tab refresh itself.

**Cloudflare Pages / Netlify:** same idea; use their scheduled builds or a cron on a tiny box that commits.

The claude.ai artifact is a hosted preview of the same `index.html`; it does not poll on its own and is refreshed when asked.

## Capacity and failure

Nothing is computed per visitor. The site is four static files, so a CDN serves hundreds of thousands of hits without touching anything: put Cloudflare (free) in front of whatever host you use, or use Cloudflare Pages, which has no bandwidth cap. GitHub Pages alone has a soft 100 GB/month limit; at ~30 KB compressed per page view that is about three million views, so it is fine until it isn't, and Cloudflare in front removes the question. Compression matters: enable gzip/brotli (Pages and Cloudflare do it by default).

What an open tab does: once a minute, only while visible, it revalidates `version.json` (a 304 when nothing changed) and reloads only when a newer build exists. On any error it backs off up to ten minutes. At 100,000 open tabs that is under 2,000 tiny requests a second at the CDN edge and zero at the origin.

Polling the sources never scales with traffic: one poll every ten minutes whatever the visitor count.

When a source fails, `build.py` keeps the last good copy (cached feed, last good mesos.watch poll), marks the build degraded, and the page says so in the strip under the header and in the Freshness card. If the daily feed has never been fetched and is down, the build stops rather than publish an empty page. Every page states its last update time in the header, the strip, the Freshness card and the footer; after 40 minutes without a rebuild the header turns amber, after three hours red.

If the host itself fails, serve `fallback.html`:

```
# nginx
error_page 500 502 503 504 /fallback.html;
location = /fallback.html { root /srv/inflation-watch; internal; }
```

Cloudflare: Custom Pages → upload `fallback.html` for 5xx errors (Pro plan), or keep the free "Always Online" copy. GitHub Pages serves `404.html` for missing paths; it is the same page.
