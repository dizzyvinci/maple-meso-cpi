#!/usr/bin/env python3
"""inflation.watch — build script.

Pulls the daily Free Market medians that maplestory-classic.com publishes
for developers (market.json), computes the Meso CPI and the NX:meso rate,
and renders index.html from template.html.

Run it on a schedule. The feed changes once or twice a day at no fixed
hour, so poll hourly and the page is never more than an hour behind it;
every run also records a tick in ticks.json (NX rate, CPI, cash-item
medians) so intraday movement shows once the feed starts refreshing
faster. Cron, every hour:

    0 * * * * cd /path/to/inflation-watch && python3 build.py >> build.log 2>&1

    python3 build.py              # fetch + build
    python3 build.py --offline    # rebuild from the last market.json on disk
    python3 build.py --no-niadb   # skip the NiaMeowDB cross-check fetch
    python3 build.py --no-mesoswatch  # skip the mesos.watch live poll

Sources: maplestory-classic.com market.json (daily history backbone, published for
developers), mesos.watch item pages (live sale medians, polled by permission of
mesos.watch), NiaMeowDB's NX:Meso indicator (one credited figure per build).
"""
import json, sys, os, urllib.request, datetime, math
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
FEED = "https://maplestory-classic.com/market.json"
# NiaMeowDB publishes its own NX:Meso indicator; its reuse policy allows quoting
# individual figures with credit and a link (meowdb.com/reuse). One number per
# build, credited on the page. A full feed needs their written OK: contact@meowdb.com
NIADB = "https://meowdb.com/msclassic/free-market"
# mesos.watch: live sale medians from its in-game scanners. Its public rules say the
# API serves the site only, so this adapter runs ONLY because the site owner has
# mesos.watch's permission; identify yourself and keep the pace polite.
MW_ENABLED = True
MW_ITEM = "https://mesos.watch/item/{id}-{slug}"
MW_DELAY = 0.5            # seconds between requests (≈80 pages per poll → about a minute)
MW_STALE_H = 6            # a mesos.watch price older than this is left out of the live figures
INTRADAY_KEEP_DAYS = 14
INTRADAY_EMBED_DAYS = 7
UA = "inflation.watch build (fan-made index; data access by permission of each source)"
TICKS_KEEP = 400

# ---------------------------------------------------------------- settings
BASE_START, BASE_END = "2026-10-21", "2026-10-27"   # Grand Launch week = 100
SERIES_START = "2026-10-07"                          # Oct 6 has one sale
HEADLINE_N = 50          # items in the headline basket: the most mesos changing hands
CAT_N = 12               # items per category sub-index
MIN_DAILY_SALES = 50     # liquidity floor: at least this many sales a day in launch week
# Items whose price regime changed for a reason that is not meso inflation. "exclude"
# keeps them out of the index; a note shows as an asterisk wherever the item appears.
BREAKS = {
    "Wolbi Throwing Stars": {"exclude": True, "since": "2026-10-26",
        "note": "NPC supply opened once players reached the citizenship rank that sells Wolbi stars (price fell about 90% on Oct 25–26); the Free Market price measures NPC supply after that, not demand for mesos, so it is shown but left out of the index."},
}
ICON_SRC = "https://maplestory.io/api/GMS/255/item/{id}/icon"
MIN_COVERAGE = 0.85      # share of basket weight that must have a real trade that day
WEIGHT_CAP = 0.08        # no single item above 8% of the headline basket
CAT_CAP = 0.20           # ... or 20% of a category basket
MIN_SALES = 5            # a daily median counts only with 5+ sales (the feed's own low-data line)
STALE_DAYS = 3           # carry a price forward at most this long, then drop the item that day
EXCLUDE_SLUGS = {        # NX-derived items the feed files under "Use"
    "mystery-palette-exchange-coupon", "palette-cleanser-exchange-coupon",
}
EXCLUDE_K = {"Cash Shop"}
CAT_LABEL = {"Etc": "Materials", "Use": "Consumables", "Scrolls": "Scrolls",
             "Equips": "Equipment", "Weapons": "Weapons", "Stars": "Weapons"}
CAT_ORDER = ["Materials", "Consumables", "Scrolls", "Equipment", "Weapons"]
EVENTS = [
    {"date": "2026-10-06", "label": "Founder's Access"},
    {"date": "2026-10-21", "label": "Grand Launch"},
    {"date": "2026-10-28", "label": "Halloween Cash Shop rotation"},
]
# Cash Shop items that sell for mesos on the Free Market, with Nexon's NX price.
# Permanent or 14-day items only: no crates (gambling EV), no 90-day bundles.
NX_PRICES = {
    "selective-5-slot-coupon": 5000, "storage-room-5-slot-coupon": 5000,
    "sp-reset-scroll": 7000, "ap-reset-scroll": 7000,
    "water-of-life": 2000, "pet-name-tag": 1800,
    "black-kitty": 3500, "black-pig": 3500, "brown-kitty": 3500,
    "brown-puppy": 3500, "pink-bunny": 3500, "white-bunny": 3500,
    "auto-hp-potion-skill": 2500, "auto-mp-potion-skill": 2500,
    "auto-move-skill": 3000, "expanded-auto-move-skill": 3000,
    "mystery-hair-coupon": 5500, "mystery-face-coupon": 3500,
    "mystery-hair-color-coupon": 1700, "signature-hair-coupon": 7500,
    "signature-face-coupon": 5500, "signature-hair-color-coupon": 4500,
    "signature-eye-color-coupon": 2500, "signature-skin-color-coupon": 2000,
    "red-ribbon": 3200, "yellow-hat": 3200, "red-hat": 3200,
    "black-hat": 3200, "red-top-hat": 3200,
}

# ---------------------------------------------------------------- helpers
STATUS = {}   # per-source health for this build; ends up in data.json and on the page

def load_feed(offline=False):
    """Download the daily feed; on any failure keep the last good copy on disk."""
    path = os.path.join(HERE, "market.json")
    st = {"ok": False, "fromCache": False, "error": None}
    if not offline:
        try:
            req = urllib.request.Request(FEED, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
            feed = json.loads(raw)
            if not feed.get("items"):
                raise ValueError("feed has no items")
            open(path, "wb").write(raw)
            st["ok"] = True
        except Exception as e:
            st["error"] = str(e)[:140]
    if not st["ok"]:
        try:
            feed = json.load(open(path))
            st["fromCache"] = True
            st["cachedAt"] = feed.get("updatedAt")
        except Exception as e:
            STATUS["feed"] = st
            raise SystemExit("no daily feed and no cached market.json: " + str(e))
    STATUS["feed"] = st
    return feed

def fetch_niadb():
    """NiaMeowDB's live NX:Meso indicator, as one credited figure."""
    import re, html as htmlmod
    try:
        req = urllib.request.Request(NIADB, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            page = r.read().decode("utf-8", "replace")
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}
    text = re.sub(r"<[^>]+>", " ", page)
    text = htmlmod.unescape(re.sub(r"\s+", " ", text))
    m = re.search(r"1 NX = ?([0-9]+(?:\.[0-9]+)?) mesos", text)
    n = re.search(r"From ([0-9,]+) NX item prices today", text)
    if not m:
        return {"ok": False, "error": "indicator not found on page"}
    return {"ok": True, "rate": float(m.group(1)), "n": n.group(1) if n else None,
            "at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="minutes"),
            "url": NIADB, "name": "NiaMeowDB NX:Meso Indicator"}

def _norm(name):
    import re
    return re.sub(r"[^a-z0-9]", "", name.lower())

def mw_ids():
    """slug -> item id map (mw-ids.json). Built once from mesos.watch's category pages
    when the file is missing: 36 requests, then cached."""
    import re, time
    path = os.path.join(HERE, "mw-ids.json")
    try:
        ids = json.load(open(path))
        if ids: return ids
    except Exception:
        pass
    ids = {}
    try:
        def get(u):
            return urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": UA}), timeout=30).read().decode("utf-8", "replace")
        idx = get("https://mesos.watch/items")
        for c in sorted(set(re.findall(r'href="(/items/[a-z0-9-]+)"', idx))):
            try:
                for m in re.finditer(r'href="/item/(\d+)-([a-z0-9-]+)"', get("https://mesos.watch" + c)):
                    ids[m.group(2)] = int(m.group(1))
            except Exception:
                pass
            time.sleep(MW_DELAY)
        if ids: json.dump(ids, open(path, "w"), sort_keys=True)
    except Exception:
        pass
    return ids

def fetch_mesoswatch(names):
    """One item page per name. Returns {name: {p, n, window, updated, low, listings}}.
    p = mesos.watch's typical price (median of recent sales), n = sales in its window."""
    import re, html as htmlmod, time
    ids = mw_ids()
    by_norm = {_norm(slug): (iid, slug) for slug, iid in ids.items()}
    out = {}
    for name in names:
        hit = by_norm.get(_norm(name))
        if not hit:
            continue
        iid, slug = hit
        try:
            req = urllib.request.Request(MW_ITEM.format(id=iid, slug=slug), headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                page = r.read().decode("utf-8", "replace")
        except Exception as e:
            out[name] = {"error": str(e)[:80]}
            time.sleep(MW_DELAY); continue
        text = re.sub(r"<script[^>]*>.*?</script>", " ", page, flags=re.S)
        text = htmlmod.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)))
        rec = {"id": iid}
        m = re.search(r"updated (\d{1,2}) ([A-Z][a-z]{2}), (\d{2}):(\d{2}) UTC", text)
        if m:
            now = datetime.datetime.now(datetime.timezone.utc)
            mon = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"].index(m.group(2)) + 1
            yr = now.year if not (mon == 12 and now.month == 1) else now.year - 1
            rec["updated"] = f"{yr:04d}-{mon:02d}-{int(m.group(1)):02d}T{m.group(3)}:{m.group(4)}:00Z"
        m = re.search(r"Typical price (?:[\d.,]+[kmb]? )?([\d,]+) mesos · median of ([\d,]+) sales?, last (\d+ (?:minute|hour|day)s?)", text)
        if m:
            rec["p"] = float(m.group(1).replace(",", "")); rec["n"] = int(m.group(2).replace(",", "")); rec["window"] = m.group(3)
        elif re.search(r"Typical price [^·]*asking", text):
            rec["asking"] = True
        for m in re.finditer(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', page, flags=re.S):
            try:
                g = json.loads(m.group(1))
                for node in g.get("@graph", [g]):
                    off = node.get("offers") if isinstance(node, dict) else None
                    if off:
                        rec["low"] = off.get("lowPrice"); rec["listings"] = off.get("offerCount")
            except Exception:
                pass
        out[name] = rec
        time.sleep(MW_DELAY)
    return out

def record_intraday(data, mw):
    """Append this poll to intraday.json and return the embedded intraday series.
    A poll where most pages failed is not recorded: the last good poll stays."""
    path = os.path.join(HERE, "intraday.json")
    try:
        polls = json.load(open(path))
    except Exception:
        polls = []
    got = sum(1 for r in mw.values() if "p" in r)
    STATUS["mesoswatch"] = {"ok": got >= 0.6 * max(1, len(mw)), "items": got, "asked": len(mw),
                            "errors": sum(1 for r in mw.values() if "error" in r)}
    if not STATUS["mesoswatch"]["ok"]:
        prev = load_intraday()
        if prev: prev["stale"] = True
        return prev
    now = datetime.datetime.now(datetime.timezone.utc)
    now_iso = now.isoformat(timespec="seconds").replace("+00:00", "Z")
    basket = {r["n"]: r for r in data["items"]}
    nxmap = {r["n"]: r["nx"] for r in data["nx"]["items"]}
    def fresh(rec):
        if not rec or "p" not in rec or "updated" not in rec: return False
        age = (now - datetime.datetime.fromisoformat(rec["updated"].replace("Z", "+00:00"))).total_seconds() / 3600
        return age <= MW_STALE_H
    def per_hour(rec):
        """sales per hour: mesos.watch reports the last N sales and the window they took."""
        import re
        m = re.match(r"(\d+) (minute|hour|day)", rec.get("window", "") or "")
        if not m: return 0.0
        v, u = int(m.group(1)), m.group(2)
        hours = v / 60 if u == "minute" else v if u == "hour" else v * 24
        return rec.get("n", 0) / max(hours, 0.25)
    # live CPI: same weights and launch-week base as the daily index
    num = den = 0.0
    for name, r in basket.items():
        rec = mw.get(name)
        if fresh(rec) and rec.get("n", 0) >= 5:
            num += r["w"] * rec["p"] / r["p0"]; den += r["w"]
    cpi = round(100 * num / den, 2) if den else None
    cov = round(den / 100, 3)
    # live NX rate: sales-weighted over mesos.watch's windows
    mn = md = 0.0; trades = 0
    for name, nxp in nxmap.items():
        rec = mw.get(name)
        if fresh(rec) and rec.get("n", 0) > 0:
            w = per_hour(rec)
            mn += rec["p"] * w; md += nxp * w; trades += rec["n"]
    nx = round(mn / md, 3) if md else None
    items = {name: {k: rec.get(k) for k in ("p", "n", "window", "updated", "low", "listings")}
             for name, rec in mw.items() if "p" in rec}
    for name, rec in items.items():
        rec["perHour"] = round(per_hour(rec), 1)
    basket_ph = round(sum(items[n]["perHour"] for n in basket if n in items))
    polls.append({"at": now_iso, "cpi": cpi, "cov": cov, "nx": nx, "nxTrades": trades, "basketPerHour": basket_ph, "items": items})
    cutoff = (now - datetime.timedelta(days=INTRADAY_KEEP_DAYS)).isoformat()
    polls = [p for p in polls if p["at"] >= cutoff]
    json.dump(polls, open(path, "w"))
    embed_cut = (now - datetime.timedelta(days=INTRADAY_EMBED_DAYS)).isoformat()
    series = [{k: p.get(k) for k in ("at", "cpi", "cov", "nx", "nxTrades", "basketPerHour")} for p in polls if p["at"] >= embed_cut]
    latest = polls[-1]
    return {"series": series, "latest": {"at": latest["at"], "cpi": cpi, "cov": cov, "nx": nx, "nxTrades": trades,
                                        "basketPerHour": basket_ph, "items": items}, "polls": len(polls)}

def load_intraday():
    """Embed the existing ledger without polling (used with --no-mesoswatch)."""
    try:
        polls = json.load(open(os.path.join(HERE, "intraday.json")))
    except Exception:
        return None
    if not polls:
        return None
    now = datetime.datetime.now(datetime.timezone.utc)
    embed_cut = (now - datetime.timedelta(days=INTRADAY_EMBED_DAYS)).isoformat()
    series = [{k: p.get(k) for k in ("at", "cpi", "cov", "nx", "nxTrades", "basketPerHour")} for p in polls if p["at"] >= embed_cut]
    l = polls[-1]
    return {"series": series, "latest": {"at": l["at"], "cpi": l["cpi"], "cov": l["cov"], "nx": l["nx"],
            "nxTrades": l["nxTrades"], "basketPerHour": l.get("basketPerHour"), "items": l["items"]}, "polls": len(polls),
            "source": {"name": "mesos.watch", "url": "https://mesos.watch/"}}

def attach_icon_ids(data):
    """MapleStory item ids (from the mesos.watch map), and the game's own item icons from
    the maplestory.io community API, cached in icons/ and embedded as data URIs so the page
    needs no remote image host (and still shows them where remote images are blocked)."""
    import base64, time
    ids = mw_ids()
    by_norm = {_norm(slug): iid for slug, iid in ids.items()}
    idir = os.path.join(HERE, "icons"); os.makedirs(idir, exist_ok=True)
    for r in data["items"] + data["flagged"] + data["nx"]["items"]:
        r["id"] = by_norm.get(_norm(r["n"]))
        if not r["id"]: continue
        p = os.path.join(idir, f"{r['id']}.png"); miss = os.path.join(idir, f"{r['id']}.missing")
        if not os.path.exists(p) and not os.path.exists(miss):
            try:
                req = urllib.request.Request(ICON_SRC.format(id=r["id"]), headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=20) as resp:
                    open(p, "wb").write(resp.read())
            except Exception:
                open(miss, "w").write("")
            time.sleep(0.2)
        if os.path.exists(p):
            r["icon"] = "data:image/png;base64," + base64.b64encode(open(p, "rb").read()).decode()

def record_niadb(data):
    """One credited figure per build goes into niadb.json; the page plots the series as 'reported'."""
    path = os.path.join(HERE, "niadb.json")
    try:
        rows = json.load(open(path))
    except Exception:
        rows = []
    cc = data.get("crosscheck")
    if cc and cc.get("ok"):
        if not rows or rows[-1]["rate"] != cc["rate"] or rows[-1]["at"][:13] != cc["at"][:13]:
            rows.append({"at": cc["at"], "rate": cc["rate"], "n": cc.get("n")})
        rows = rows[-2000:]
        json.dump(rows, open(path, "w"))
    return rows

def smega_rate(path, nxmap, hours=6):
    """Asking-price NX rate from megaphone text, one row per line:
       <ISO time>\t<message>   e.g.  2026-11-05T01:12:00Z\tS> Selective 5-slot Coupon 85k ea
    A line counts when it is a sell offer (S>, sell, selling, WTS) that names a cash item
    and a price with k/m suffix; the item's ask is the median of its offers in the
    window, and the rate is the sales-per-hour-free sum of asks over NX, weighted by
    offer count. It measures what sellers hope for, so it sits above sale-based rates."""
    import re, statistics
    try:
        lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
    except Exception:
        return None
    now = datetime.datetime.now(datetime.timezone.utc)
    cut = (now - datetime.timedelta(hours=hours)).isoformat()
    offers = {}
    for ln in lines:
        if "\t" not in ln: continue
        t, msg = ln.split("\t", 1)
        if t < cut: continue
        low = msg.lower()
        if not re.search(r"(^|\W)(s>|sell|selling|wts)", low): continue
        for name, nxp in nxmap.items():
            if name.lower() in low:
                m = re.search(r"(\d+(?:\.\d+)?)\s*([km])\b", low.split(name.lower(), 1)[1])
                if not m: continue
                v = float(m.group(1)) * (1000 if m.group(2) == "k" else 1000000)
                offers.setdefault(name, []).append(v)
    if not offers:
        return {"ok": False, "offers": 0}
    num = den = 0.0; n = 0
    for name, vs in offers.items():
        ask = statistics.median(vs); num += ask * len(vs); den += nxmap[name] * len(vs); n += len(vs)
    return {"ok": True, "rate": round(num / den, 3), "offers": n, "items": len(offers), "hours": hours}

KX_ENABLED = True          # kerning.gg (Kerning Exchange): 7-day median smega asks and bids per item
KX_EVERY_MIN = 120         # its medians span 7 days, so a poll every two hours is plenty
KX_DELAY = 0.6

def _kx_num(s):
    s = s.replace(",", "")
    mult = 1000000 if s.endswith("m") else 1000 if s.endswith("k") else 1
    return float(s.rstrip("km")) * mult

def kerning_poll(nx_items):
    """Reads each cash item's page on kerning.gg: smega selling and buying 7-day medians with
    how many players shouted them, and the lowest ask in 24 hours. Pages only, polite pace,
    identified UA; robots.txt allows it and the site states no restriction on reuse. Credited."""
    import re, html as _html, time
    ids = mw_ids(); by_norm = {_norm(k): v for k, v in ids.items()}
    out, errors = {}, 0
    for r in nx_items:
        iid = r.get("id") or by_norm.get(_norm(r["n"]))
        if not iid: continue
        try:
            req = urllib.request.Request(f"https://kerning.gg/item/{iid}", headers={"User-Agent": UA})
            page = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
            t = _html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", page, flags=re.S))))
            def grab(label):
                mm = re.search(label + r" ([\d.,]+[km]?|—)(?: (\d+) players)?", t)
                return (mm.group(1), mm.group(2)) if mm else (None, None)
            sell, selln = grab("Selling 7-day median"); buy, buyn = grab("Buying 7-day median"); low, _ = grab("Lowest asking 24 hours")
            pm = re.search(r"smegad by (\d+) players", t); players = pm.group(1) if pm else "0"
            if sell is None: errors += 1; continue
            out[r["n"]] = {"sell": _kx_num(sell) if sell != "—" else None, "sellN": int(selln or 0),
                           "buy": _kx_num(buy) if buy != "—" else None, "buyN": int(buyn or 0),
                           "low": _kx_num(low) if low != "—" else None, "players": int(players), "nx": r["nx"]}
        except Exception:
            errors += 1
        time.sleep(KX_DELAY)
    return out, errors

def record_smega(data):
    """Smega asking-price rate. Primary source: kerning.gg medians (sell = ask, buy = bid),
    weighted by how many players shouted each item. A local smega.log (see smega_rate) adds
    its own rows when present."""
    nxmap = {r["n"]: r["nx"] for r in data["nx"]["items"]}
    path = os.path.join(HERE, "smega.json")
    try:
        rows = json.load(open(path))
    except Exception:
        rows = []
    now = data["generatedAt"]
    last_kx = max([r["at"] for r in rows if r.get("src") == "kerning.gg"], default="")
    due = (not last_kx) or (datetime.datetime.fromisoformat(now.replace("Z", "+00:00")) - datetime.datetime.fromisoformat(last_kx.replace("Z", "+00:00"))).total_seconds() >= KX_EVERY_MIN * 60
    kx_status = {"ok": None, "items": 0, "errors": 0, "polled": False}
    if KX_ENABLED and due and "--offline" not in sys.argv and "--no-kerning" not in sys.argv:
        items, errors = kerning_poll(data["nx"]["items"])
        kx_status.update({"polled": True, "items": len(items), "errors": errors})
        asks = [(v["sell"] / v["nx"], v["sellN"]) for v in items.values() if v["sell"] and v["sellN"]]
        bids = [(v["buy"] / v["nx"], v["buyN"]) for v in items.values() if v["buy"] and v["buyN"]]
        if asks and len(items) >= 10:
            ask = sum(r * w for r, w in asks) / sum(w for _, w in asks)
            bid = (sum(r * w for r, w in bids) / sum(w for _, w in bids)) if bids else None
            rows.append({"at": now, "rate": round(ask, 3), "bid": round(bid, 3) if bid else None,
                         "offers": sum(w for _, w in asks), "items": len(items), "src": "kerning.gg",
                         "perItem": {k: {"sell": v["sell"], "buy": v["buy"], "low": v["low"], "sellN": v["sellN"], "buyN": v["buyN"]} for k, v in items.items()}})
            kx_status["ok"] = True
        else:
            kx_status["ok"] = False
    res = smega_rate(os.path.join(HERE, "smega.log"), nxmap)
    if res and res.get("ok"):
        rows.append({"at": now, "rate": res["rate"], "offers": res["offers"], "items": res["items"], "src": "smega.log"})
    rows = rows[-2000:]
    json.dump(rows, open(path, "w"))
    STATUS["kerning"] = kx_status
    series = [{k: r.get(k) for k in ("at", "rate", "bid", "offers", "items", "src")} for r in rows]
    latest_kx = next((r for r in reversed(rows) if r.get("src") == "kerning.gg"), None)
    return {"series": series, "latest": latest_kx, "log": res}

def record_tick(data):
    """Append one row per distinct feed refresh to ticks.json."""
    path = os.path.join(HERE, "ticks.json")
    try:
        ticks = json.load(open(path))
    except Exception:
        ticks = []
    feed_at = data["source"]["updatedAt"]
    if any(t["feedAt"] == feed_at for t in ticks):
        return ticks
    nx = data["nx"]
    ticks.append({"at": data["generatedAt"], "feedAt": feed_at,
                  "cpi": data["headline"]["latest"], "nx": nx["latest"], "nx7": nx["latest7"],
                  "items": {r["s"]: r["p"] for r in nx["items"]}})
    ticks = ticks[-TICKS_KEEP:]
    json.dump(ticks, open(path, "w"))
    return ticks

def daterange(a, b):
    d = datetime.date.fromisoformat(a)
    e = datetime.date.fromisoformat(b)
    while d <= e:
        yield d.isoformat()
        d += datetime.timedelta(days=1)

def filled_prices(hist, dates):
    """median per date, counting only days with MIN_SALES+ sales. Gaps are
    forward-filled; `age` is days since the last counted trade (0 = traded
    that day; a large number before the first trade, which keeps the item
    out of the index until it has really been sold)."""
    m = {h[0]: h[1] for h in hist if h[2] >= MIN_SALES}
    prices, age = [], []
    last, a = None, 10 ** 6
    for d in dates:
        if d in m:
            last, a = m[d], 0
        else:
            a += 1
        prices.append(last); age.append(a)
    first = next((p for p in prices if p is not None), None)
    prices = [first if p is None else p for p in prices]
    return prices, age

def capped_weights(members, cap=WEIGHT_CAP):
    """base-week expenditure shares, with no item above `cap` (excess is
    redistributed over the uncapped items in proportion to their share)."""
    raw = [m["q"] * m["p0"] for m in members]
    tot = sum(raw)
    w = [x / tot for x in raw]
    if cap * len(w) <= 1:
        return w
    fixed = set()
    while True:
        over = [i for i in range(len(w)) if i not in fixed and w[i] > cap + 1e-12]
        if not over:
            return w
        fixed.update(over)
        for i in over: w[i] = cap
        free = [i for i in range(len(w)) if i not in fixed]
        spare = 1 - sum(w[i] for i in fixed)
        fs = sum(w[i] for i in free) or 1
        for i in free: w[i] = spare * w[i] / fs

def laspeyres(members, dates, cap=WEIGHT_CAP):
    """weighted mean of price relatives (p_t / p_base) with capped base-week
    expenditure weights. An item whose last counted trade is more than
    STALE_DAYS old is left out that day and the other weights are rescaled.
    Returns series and coverage (share of weight that traded that day)."""
    w = capped_weights(members, cap)
    for m, wi in zip(members, w): m["w"] = wi
    series, coverage = [], []
    for i, _ in enumerate(dates):
        live = [m for m in members if m["age"][i] <= STALE_DAYS]
        tot = sum(m["w"] for m in live) or 1
        series.append(round(100 * sum(m["w"] * m["prices"][i] / m["p0"] for m in live) / tot, 2))
        coverage.append(round(sum(m["w"] for m in members if m["age"][i] == 0), 3))
    return series, coverage

def prep_member(item, dates):
    hist = item["h"]
    base = [h for h in hist if BASE_START <= h[0] <= BASE_END]
    if not base:
        return None
    s = sum(h[2] for h in base)
    if s == 0:
        return None
    p0 = sum(h[1] for h in base) / len(base)          # mean of daily medians
    q = s / len(list(daterange(BASE_START, BASE_END)))  # average daily sales
    prices, age = filled_prices(hist, dates)
    if all(a > 0 for a in age):
        return None
    return {"n": item["n"], "s": item["s"], "k": item["k"],
            "cat": CAT_LABEL.get(item["k"], item["k"]),
            "q": q, "p0": p0, "prices": prices, "age": age,
            "sold": item["sold"], "sellers": item.get("sellers", 0),
            "last": item["h"][-1][0]}

def pct(a, b):
    return None if not b else round(100 * (a / b - 1), 2)

def trim_start(series, coverage, dates):
    """first index where coverage >= MIN_COVERAGE"""
    for i, c in enumerate(coverage):
        if c >= MIN_COVERAGE:
            return i
    return 0

# ---------------------------------------------------------------- compute
def compute(feed):
    items = feed["items"]
    all_dates = sorted({h[0] for it in items for h in it["h"]})
    last = all_dates[-1]
    dates = list(daterange(SERIES_START, last))

    # --- candidates: every non-cash item with launch-week data and real liquidity,
    #     ranked by launch-week meso volume (average daily sales × average daily price)
    tradeable = [it for it in items if it["k"] not in EXCLUDE_K and it["s"] not in EXCLUDE_SLUGS]
    cands = []
    for it in tradeable:
        m = prep_member(it, dates)
        if m and m["q"] >= MIN_DAILY_SALES:
            m["vol"] = m["q"] * m["p0"]
            m["note"] = BREAKS.get(m["n"], {}).get("note")
            m["excluded"] = BREAKS.get(m["n"], {}).get("exclude", False)
            cands.append(m)
    cands.sort(key=lambda m: -m["vol"])
    members = [m for m in cands if not m["excluded"]][:HEADLINE_N]
    flagged = [m for m in cands if m["excluded"]][:3]   # shown with a note, not indexed
    head_series, head_cov = laspeyres(members, dates)
    start = trim_start(head_series, head_cov, dates)

    li = len(dates) - 1
    base_i0 = dates.index(BASE_START); base_i1 = dates.index(BASE_END)
    rows, contrib = [], []
    for m in members:
        w = m["w"]
        p_now, p_prev = m["prices"][li], m["prices"][li - 1]
        d7 = m["prices"][li - 7] if li - 7 >= 0 else None
        rows.append({
            "n": m["n"], "s": m["s"], "cat": m["cat"], "w": round(100 * w, 2), "vol": round(m["vol"]),
            "note": m.get("note"),
            "q": round(m["q"], 1), "p0": round(m["p0"], 2), "p": p_now,
            "chgBase": pct(p_now, m["p0"]), "chg1d": pct(p_now, p_prev),
            "chg7d": pct(p_now, d7) if d7 else None,
            "sold": m["sold"], "sellers": m["sellers"],
            "spark": [round(x, 2) for x in m["prices"][start:]],
            "age": m["age"][li], "last": m["last"],
        })
        contrib.append({"n": m["n"], "s": m["s"], "cat": m["cat"],
                        "pts": round(100 * w * (p_now / m["p0"] - 1), 2)})
    rows.sort(key=lambda r: -r["w"])
    contrib.sort(key=lambda c: -abs(c["pts"]))
    flagged_rows = []
    for m in flagged:
        p_now = m["prices"][li]
        flagged_rows.append({"n": m["n"], "s": m["s"], "cat": m["cat"], "w": 0, "vol": round(m["vol"]), "note": m["note"],
                             "q": round(m["q"], 1), "p0": round(m["p0"], 2), "p": p_now, "chgBase": pct(p_now, m["p0"]),
                             "chg1d": None, "chg7d": None, "sold": m["sold"], "sellers": m["sellers"],
                             "spark": [round(x, 2) for x in m["prices"][start:]], "age": m["age"][li], "last": m["last"],
                             "excluded": True})

    # --- category sub-indices: the CAT_N biggest meso-volume items of each category
    cats = {}
    for label in CAT_ORDER:
        cm = [dict(m) for m in cands if m["cat"] == label and not m["excluded"]][:CAT_N]
        if not cm: continue
        s, c = laspeyres(cm, dates, CAT_CAP)
        cats[label] = {"series": s[start:], "coverage": c[start:],
                       "items": [x["n"] for x in cm],
                       "latest": s[li], "chg1d": pct(s[li], s[li - 1]),
                       "chg7d": pct(s[li], s[li - 7]) if li - 7 >= 0 else None}

    # --- whole-market activity (every item, not just the basket)
    sales = defaultdict(int); turnover = defaultdict(float)
    for it in items:
        for d, p, n in it["h"]:
            sales[d] += n; turnover[d] += p * n
    activity = {"sales": [sales.get(d, 0) for d in dates[start:]],
                "turnover": [round(turnover.get(d, 0)) for d in dates[start:]]}

    # --- NX : meso
    by = {it["s"]: it for it in items}
    nx_num = defaultdict(float); nx_den = defaultdict(float); nx_trades = defaultdict(int)
    nx_rows = []
    for slug, nxp in NX_PRICES.items():
        it = by.get(slug)
        if not it: continue
        m = {h[0]: (h[1], h[2]) for h in it["h"]}
        for d, (p, n) in m.items():
            nx_num[d] += p * n; nx_den[d] += nxp * n; nx_trades[d] += n
        # trailing 7-day item rate
        last7 = [h for h in it["h"] if h[0] > dates[max(0, li - 7)]]
        s7 = sum(h[2] for h in last7)
        r7 = (sum(h[1] * h[2] for h in last7) / s7 / nxp) if s7 else None
        nx_rows.append({"n": it["n"], "s": slug, "nx": nxp, "p": it["p"],
                        "sold": it["sold"], "rate": round(it["p"] / nxp, 2),
                        "rate7": round(r7, 2) if r7 else None,
                        "trades7": s7, "last": it["h"][-1][0]})
    nx_rows.sort(key=lambda r: -r["sold"])
    nx_dates = [d for d in dates if nx_den.get(d)]
    nx_rate = [round(nx_num[d] / nx_den[d], 3) for d in nx_dates]
    # trailing 7-day volume-weighted rate
    nx_rate7 = []
    for i, d in enumerate(nx_dates):
        win = nx_dates[max(0, i - 6):i + 1]
        nx_rate7.append(round(sum(nx_num[x] for x in win) / sum(nx_den[x] for x in win), 3))
    nx_base = [i for i, d in enumerate(nx_dates) if BASE_START <= d <= BASE_END]
    nx_base_rate = (sum(nx_num[nx_dates[i]] for i in nx_base) /
                    sum(nx_den[nx_dates[i]] for i in nx_base)) if nx_base else None
    nx = {"dates": nx_dates, "rate": nx_rate, "rate7": nx_rate7,
          "trades": [nx_trades[d] for d in nx_dates],
          "latest": nx_rate[-1], "latest7": nx_rate7[-1],
          "prev": nx_rate[-2] if len(nx_rate) > 1 else None,
          "base": round(nx_base_rate, 3) if nx_base_rate else None,
          "items": nx_rows, "latestDate": nx_dates[-1]}

    return {
        "source": {"name": "maplestory-classic.com Free Market feed", "url": FEED,
                   "updatedAt": feed["updatedAt"]},
        "generatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "base": {"start": BASE_START, "end": BASE_END, "label": "Launch week (Oct 21–27, 2026)"},
        "dates": dates[start:],
        "lastDate": last,
        "partialLast": last == feed["updatedAt"][:10],  # feed cut mid-day
        "headline": {"series": head_series[start:], "coverage": head_cov[start:],
                     "latest": head_series[li], "chg1d": pct(head_series[li], head_series[li - 1]),
                     "chg7d": pct(head_series[li], head_series[li - 7]) if li - 7 >= 0 else None,
                     "n": len(members),
                     "turnoverBase": round(sum(m["q"] * m["p0"] for m in members))},
        "cats": cats,
        "items": rows,
        "flagged": flagged_rows,
        "breaks": BREAKS,
        "contrib": contrib,
        "activity": activity,
        "nx": nx,
        "events": [e for e in EVENTS if e["date"] >= dates[start]],
        "itemCount": len(items),
    }

def render(data):
    tpl = open(os.path.join(HERE, "template.html"), encoding="utf-8").read()
    blob = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    out = tpl.replace("__IW_DATA__", blob)
    path = os.path.join(HERE, "index.html")
    open(path, "w", encoding="utf-8").write(out)
    # tiny file an open tab polls to learn a newer build exists
    it = data.get("intraday") or {}
    lv = it.get("latest") or {}
    json.dump({"generatedAt": data["generatedAt"], "feedAt": data["source"]["updatedAt"],
               "pollAt": lv.get("at"), "nx": lv.get("nx") or data["nx"]["latest"],
               "cpi": data["headline"]["latest"], "degraded": data.get("status", {}).get("degraded", False)},
              open(os.path.join(HERE, "version.json"), "w"), separators=(",", ":"))
    render_fallback(data)
    return path

def render_fallback(data):
    """fallback.html: last known figures, no fonts, no scripts, under 6 KB.
    Serve it as the error page (nginx error_page 500 502 503 504, Cloudflare custom page)."""
    it = data.get("intraday") or {}
    lv = it.get("latest") or {}
    nx = data["nx"]; h = data["headline"]
    rate = lv.get("nx") or nx["latest"]
    def fmt(v): return f"{v:,.0f}"
    def t(iso): return iso.replace("T", " ")[:16] + " UTC" if iso else "unknown"
    deg = data.get("status", {}).get("degraded")
    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#0b1220"><meta http-equiv="refresh" content="120">
<title>inflation.watch — last known figures</title>
<style>body{{margin:0;background:#0b1220;color:#e9eef9;font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;display:flex;min-height:100vh;align-items:center;justify-content:center;padding:20px;box-sizing:border-box}}
.c{{max-width:560px;width:100%;background:#121c33;border:1px solid #1f2b48;border-radius:14px;padding:26px 28px}}h1{{font-size:20px;margin:0 0 4px}}h1 b{{color:#f3c454}}
.n{{font-size:54px;font-weight:700;letter-spacing:-.03em;color:#ffd978;line-height:1;margin:14px 0 2px}}.n small{{font-size:14px;font-weight:500;color:#8e9bb8;letter-spacing:0}}
.m{{color:#8e9bb8}}.row{{display:flex;justify-content:space-between;gap:12px;padding:7px 0;border-top:1px solid #1f2b48;font-size:14px}}.w{{background:#2a2410;border:1px solid #6b5520;color:#ffd978;border-radius:9px;padding:10px 12px;margin:14px 0;font-size:14px}}a{{color:#6ea0ff}}</style></head>
<body><div class="c"><h1>inflation<b>.watch</b></h1><div class="m">MapleStory Classic World · last known figures</div>
<div class="w">The full site{" or its data sources are" if deg else " is"} unavailable right now. These are the last figures it published; this page refreshes itself every two minutes.</div>
<div class="n">{fmt(rate*1000)} <small>mesos per 1,000 NX</small></div>
<div class="m">Meso CPI {h['latest']:.1f} (launch week = 100){f" · live estimate {lv['cpi']:.1f}" if lv.get('cpi') else ""}</div>
<div style="margin-top:16px">
<div class="row"><span class="m">Last update (page built)</span><b>{t(data['generatedAt'])}</b></div>
<div class="row"><span class="m">Live poll (mesos.watch)</span><b>{t(lv.get('at'))}</b></div>
<div class="row"><span class="m">Daily feed</span><b>{t(data['source']['updatedAt'])}</b></div>
<div class="row"><span class="m">Launch-week rate</span><b>{fmt(nx['base']*1000)}</b></div>
</div>
<p class="m" style="font-size:13px;margin:16px 0 0"><a href="./">Try the full site</a> · fan-made, not affiliated with Nexon</p></div></body></html>"""
    open(os.path.join(HERE, "fallback.html"), "w", encoding="utf-8").write(html)
    # GitHub Pages serves 404.html for any missing path; same content is a sensible stand-in there
    open(os.path.join(HERE, "404.html"), "w", encoding="utf-8").write(html)

if __name__ == "__main__":
    offline = "--offline" in sys.argv
    feed = load_feed(offline)
    data = compute(feed)
    data["crosscheck"] = None if "--no-niadb" in sys.argv else fetch_niadb()
    STATUS["niadb"] = {"ok": bool(data["crosscheck"] and data["crosscheck"].get("ok")),
                       "error": (data["crosscheck"] or {}).get("error")}
    data["intraday"] = None
    if MW_ENABLED and "--no-mesoswatch" in sys.argv:
        data["intraday"] = load_intraday()        # last polls, no new request
    elif MW_ENABLED:
        names = [r["n"] for r in data["items"]] + [r["n"] for r in data["nx"]["items"]]
        try:
            mw = fetch_mesoswatch(names)
        except Exception as e:
            mw = {n: {"error": str(e)[:80]} for n in names}
        data["intraday"] = record_intraday(data, mw)
        if data["intraday"]:
            data["intraday"]["source"] = {"name": "mesos.watch", "url": "https://mesos.watch/"}
    attach_icon_ids(data)
    data["niadb"] = record_niadb(data)
    data["smega"] = record_smega(data)
    data["status"] = STATUS
    data["status"]["degraded"] = (not STATUS.get("feed", {}).get("ok", False)) or \
                                 (MW_ENABLED and "--no-mesoswatch" not in sys.argv and not STATUS.get("mesoswatch", {}).get("ok", False))
    data["ticks"] = record_tick(data)
    data["ticks"] = [{k: v for k, v in t.items() if k != "items"} for t in data["ticks"]]
    json.dump(data, open(os.path.join(HERE, "data.json"), "w"), indent=1)
    p = render(data)
    h = data["headline"]; nx = data["nx"]
    print(f"feed {data['source']['updatedAt']}  dates {data['dates'][0]}..{data['lastDate']}")
    print(f"Meso CPI {h['latest']}  1d {h['chg1d']}%  7d {h['chg7d']}%  basket {h['n']}")
    print(f"NX:meso {nx['latest']} mesos/NX (7d {nx['latest7']}, base {nx['base']})")
    cc = data.get("crosscheck")
    if cc: print("NiaDB cross-check", cc.get("rate"), cc.get("error", ""))
    it = data.get("intraday")
    if it:
        l = it["latest"]; print(f"mesos.watch live: NX {l['nx']}  CPI {l['cpi']}  coverage {l['cov']}  items {len(l['items'])}  polls {it['polls']}{'  (STALE, poll failed)' if it.get('stale') else ''}")
    print("status", json.dumps(STATUS))
    print("ticks", len(data["ticks"]), "| wrote", p)
