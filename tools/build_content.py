"""Clued daily content builder.

For one language it:
  1. reads today's world news from Wikipedia (falls back to English news when that Wikipedia has none),
  2. reads the curated geography / economics topics (the lists live in index.html, one source of truth),
  3. attaches real shapes from Natural Earth (lakes, rivers, regions, seas) so the globe can colour them,
  4. narrates every card with a neural voice (Kokoro or Piper) into small MP3 files.

Output: out/content/<lang>/{news.json, topics.json, a/*.mp3}
Usage:  python tools/build_content.py <lang>
"""
import hashlib, html, json, os, re, subprocess, sys, time, urllib.parse, urllib.request, wave
from datetime import datetime, timedelta, timezone
from pathlib import Path

LANG = sys.argv[1] if len(sys.argv) > 1 else "en"
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out" / "content" / LANG
CACHE = ROOT / ".cache-content" / LANG          # persisted between runs (actions/cache)
AUDIO_CACHE = CACHE / "audio"
for d in (OUT / "a", AUDIO_CACHE): d.mkdir(parents=True, exist_ok=True)
LOG = []
def log(*a):
    s = " ".join(str(x) for x in a); print(s, flush=True); LOG.append(s)

UA = {"User-Agent": "CluedBot/1.0 (https://github.com/Til70707/clued; educational app)", "Accept": "application/json"}
def get(url, tries=3, raw=False):
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=40) as r:
                data = r.read()
                return data if raw else json.loads(data)
        except urllib.error.HTTPError as e:
            if e.code == 404: return None
            time.sleep(1.5 * (k + 1))
        except Exception:
            time.sleep(1.5 * (k + 1))
    return None

# ---------- shared rules (same as the app) ----------
BLOCK = re.compile(r"porn|onlyfans|hentai|\bsex(ual|y)?\b|\bnud(e|ity|ism|ist)\b|erotic|fetish|\bbdsm\b|\bescort\b|\bstripper|\brape\b|incest|camgirl", re.I)
MINOR = re.compile(r"nogomet|košark|fußball|fussball|fútbol|calcio|baloncesto|pallacanestro|schauspieler|attore|(^|\s)acteur|igral(ec|ka)|pev(ec|ka)|sänger|cantante|chanteu|football|soccer|basketball|tennis|cricket|\bracing|cyclist|\bgolf\b|rugby|baseball|ice hockey|athlete|footballer|\bsports?\b|tournament|championship|formula one|grand prix|olympic|\bfilm\b|album|singer|\bactor|\bactress|rapper|\bband\b|television series|video game|beauty pageant|music festival|award ceremony", re.I)
SKIP_TITLES = re.compile(r"^(Main_Page|Special:|Wikipedia:|File:|Portal:|Help:|Template:|Category:)")
# pictures that make bad backgrounds: logos, flags, emblems, slogans, maps
BAD_IMG = re.compile(r"\.svg($|/)|flag|logo|emblem|coat_of_arms|seal_of|wappen|grb_|banner|slogan|locator|location_map|_map\b|map_of", re.I)

def strip_html(h):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", h or ""))).strip()

def trim(t, mx=420):
    t = re.sub(r"\s+", " ", t or "").strip()
    t = re.sub(r"\s*\(\s*\{\{[^)]*\)", "", t)          # leftover wiki templates like "({{ety|ar|…}})"
    t = re.sub(r"\{\{[^}]*\}\}|\{\{|\}\}", "", t)
    if len(t) <= mx: return t
    cut = t[:mx]; dot = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return cut[:dot + 1] if dot > mx * 0.45 else re.sub(r"\s+\S*$", "", cut) + "…"

def for_speech(t):
    t = re.sub(r"\s*\([^()]*\)", "", t)            # drop "(pronunciation; other names)"
    t = re.sub(r"\s*\[[^\]]*\]", "", t)
    t = t.replace("…", ".").replace("–", ", ").replace("—", ", ")
    return re.sub(r"\s+", " ", t).strip()

def ok_page(p):
    if not p or not p.get("titles"): return False
    t = p["titles"].get("canonical") or p.get("title") or ""
    if SKIP_TITLES.search(t) or BLOCK.search(t) or BLOCK.search(p.get("extract") or "") or BLOCK.search(p.get("description") or ""): return False
    return p.get("type") != "disambiguation" and len(p.get("extract") or "") > 80

def good_image(p):
    o, th = p.get("originalimage") or {}, p.get("thumbnail") or {}
    src = o.get("source") or th.get("source") or ""
    if not src or BAD_IMG.search(src): return None
    if (o.get("width") or 0) <= 1280 and re.search(r"\.(jpe?g|png|webp)$", o.get("source") or "", re.I):
        big = o["source"]
    else:
        t = th.get("source") or ""
        big = re.sub(r"/\d+px-", "/1280px-", t) if re.search(r"/\d+px-", t) else (o.get("source") or t)
    return {"img": big, "thumb": th.get("source") or big, "imgW": o.get("width") or th.get("width") or 0, "imgH": o.get("height") or th.get("height") or 0}

def card_from_page(p, kind, **extra):
    title = (p.get("titles") or {}).get("normalized") or p.get("title")
    c = {"id": f"{kind}:{(p.get('titles') or {}).get('canonical') or title}", "kind": kind, "title": title, "name": title,
         "desc": p.get("description") or "", "text": trim(p.get("extract")), "url": ((p.get("content_urls") or {}).get("mobile") or {}).get("page") or "",
         "img": "", "thumb": "", "imgW": 0, "imgH": 0}
    im = good_image(p)
    if im: c.update(im)
    co = p.get("coordinates")
    if co and "lat" in co and "lon" in co: c["coords"] = [round(co["lon"], 3), round(co["lat"], 3)]
    c.update(extra)
    return c

# ---------- topic lists from index.html ----------
SRC = (ROOT / "index.html").read_text(encoding="utf-8")
def js_value(name):
    m = re.search(r"const " + name + r" = (.+?);\n", SRC)
    return json.loads(m.group(1))
CAPITALS = js_value("CAPITALS"); GEO = js_value("GEO_TOPICS"); ECON = js_value("ECON_TOPICS")
TOPICS = [(t, "geo") for t in GEO] + [(t, "econ") for t in ECON] + [(t, "geo") for t in CAPITALS]
seen = set(); TOPICS = [x for x in TOPICS if not (x[0] in seen or seen.add(x[0]))]

# ---------- map data ----------
def load_geo(url, name):
    f = ROOT / ".cache-geo" / name
    f.parent.mkdir(exist_ok=True)
    if not f.exists():
        data = get(url, raw=True)
        if data: f.write_bytes(data)
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None

NE = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/"
layers = {
    "lake": load_geo(NE + "ne_10m_lakes.geojson", "lakes10.json"),
    "river": load_geo(NE + "ne_50m_rivers_lake_centerlines.geojson", "rivers50.json"),
    "region": load_geo(NE + "ne_50m_geography_regions_polys.geojson", "regions50.json"),
    "sea": load_geo(NE + "ne_50m_geography_marine_polys.geojson", "marine50.json"),
    "lake_eu": load_geo(NE + "ne_10m_lakes_europe.geojson", "lakes10eu.json"),
    "region10": load_geo(NE + "ne_10m_geography_regions_polys.geojson", "regions10.json"),
    "sea10": load_geo(NE + "ne_10m_geography_marine_polys.geojson", "marine10.json"),
}
SHAPE_TYPE = {"lake_eu": "lake", "region10": "region", "sea10": "sea"}  # extra detail layers draw like their 1:50m cousins
# English topic -> (layer, Natural Earth name). Names checked case-insensitively.
FEATURE = {
    "Lake Baikal": ("lake", "Lake Baikal"), "Caspian Sea": ("lake", "Caspian Sea|Caspian"), "Aral Sea": ("lake", "Aral Sea"), "Dead Sea": ("lake", "Dead Sea"),
    "Lake Bled": ("lake_eu", "Bled|Lake Bled|Blejsko jezero"),
    "Nile": ("river", "Nile"), "Danube": ("river", "Danube"), "Amazon River": ("river", "Amazonas"), "Yangtze": ("river", "Chang Jiang"),
    "Ganges": ("river", "Ganges"), "Mississippi River": ("river", "Mississippi"), "Congo River": ("river", "Congo"), "Mekong": ("river", "Mekong"),
    "Sahara": ("region", "SAHARA"), "Gobi Desert": ("region", "GOBI DESERT"), "Atacama Desert": ("region", "ATACAMA DESERT"), "Namib": ("region", "NAMIB DESERT"),
    "Himalayas": ("region", "HIMALAYAS"), "Alps": ("region", "ALPS"), "Andes": ("region", "ANDES"), "Rocky Mountains": ("region", "ROCKY MOUNTAINS"),
    "Ural Mountains": ("region", "URAL MOUNTAINS"), "Caucasus": ("region", "CAUCASUS MTS."), "Patagonia": ("region", "PATAGONIA"), "Siberia": ("region", "SIBERIA"),
    "Sahel": ("region", "SAHEL"), "Balkans": ("region", "BALKAN PENINSULA|BALKAN PEN.|BALKANS"), "Scandinavia": ("region", "SCANDINAVIA"), "Great Rift Valley": ("region", "GREAT RIFT VALLEY"),
    "Amazon rainforest": ("region", "AMAZON BASIN"), "Horn of Africa": ("region", "HORN OF AFRICA"), "Central Asia": ("region", "CENTRAL ASIA|TURAN LOWLAND|KAZAKH UPLANDS"),
    "Mediterranean Sea": ("sea", "Mediterranean Sea"), "Adriatic Sea": ("sea", "Adriatic Sea"), "South China Sea": ("sea", "South China Sea"),
    "Pacific Ocean": ("sea", "North Pacific Ocean|South Pacific Ocean"), "Atlantic Ocean": ("sea", "North Atlantic Ocean|South Atlantic Ocean"),
    "Indian Ocean": ("sea", "Indian Ocean"), "Arctic Ocean": ("sea", "Arctic Ocean"), "Strait of Hormuz": ("sea", "Strait of Hormuz|Persian Gulf"),
    "Strait of Malacca": ("sea", "Strait of Malacca|Malacca Strait"), "Strait of Gibraltar": ("sea", "Strait of Gibraltar"), "Bosporus": ("sea10", "Bosporus|Bosphorus|Istanbul Bogazi|Sea of Marmara"),
    "Great Barrier Reef": ("region10", "GREAT BARRIER REEF|Great Barrier Reef"),
}
def round_geom(g, nd=2):
    def r(c):
        if isinstance(c[0], (int, float)): return [round(c[0], nd), round(c[1], nd)]
        return [r(x) for x in c]
    return {"type": g["type"], "coordinates": r(g["coordinates"])}
try:
    from shapely.geometry import shape, mapping
    from shapely.ops import unary_union
except Exception:
    shape = None
def _norm(n):
    n = (n or "").lower().replace("mts.", "mountains").replace("pen.", "peninsula").replace("des.", "desert")
    return re.sub(r"[^a-z]+", " ", n).strip()
def _names(f):
    pr = f["properties"]
    return [pr.get(k) for k in ("name", "NAME", "name_en", "NAME_EN", "name_alt", "NAME_ALT") if pr.get(k)]
def find_feature(topic):
    spec = FEATURE.get(topic)
    if not spec or not shape: return None
    layer, names = spec
    want = [_norm(n) for n in names.split("|")]
    order = [layer] + [l for l in layers if l != layer]   # the named layer first, then every other layer
    hits, used = [], layer
    for l in order:
        fc = layers.get(l)
        if not fc: continue
        for w in want:  # earlier names are better names: stop at the first one that matches
            hits = [f for f in fc["features"] if any(_norm(n) == w for n in _names(f))]
            if hits: break
        if hits: used = l; break
    if not hits and layers.get(layer):  # last try: a longer name that contains ours, in the named layer only
        hits = [f for f in layers[layer]["features"] if any(re.search(r"\b" + re.escape(w) + r"\b", _norm(n)) for w in want for n in _names(f))]
    if not hits:
        cands = sorted({n for l in layers.values() if l for f in l["features"] for n in _names(f) if any(w.split()[0] in _norm(n) for w in want)})[:12]
        log("  no shape for", topic, "| near:", cands); return None
    geom = unary_union([shape(f["geometry"]) for f in hits])
    minx, miny, maxx, maxy = geom.bounds
    tol = max(0.005, min(0.3, max(maxx - minx, maxy - miny) / 250))
    geom = geom.simplify(tol, preserve_topology=True)
    ft = SHAPE_TYPE.get(used, used)
    if ft == "lake" and topic.endswith("Sea") and used != "lake": ft = "sea"
    return {"ftype": ft, "geom": round_geom(mapping(geom), 3 if max(maxx - minx, maxy - miny) < 2 else 2), "bbox": [round(minx, 2), round(miny, 2), round(maxx, 2), round(maxy, 2)]}

# country outlines (world-atlas 110m) -> "which country is this point in?"
WA = get("https://cdn.jsdelivr.net/npm/world-atlas@2.0.2/countries-110m.json") or {}
COUNTRIES = {g["properties"]["name"] for g in (WA.get("objects", {}).get("countries", {}).get("geometries", []))}
COUNTRY_ALIAS = {"United States": "United States of America", "Czech Republic": "Czechia", "Bosnia and Herzegovina": "Bosnia and Herz.", "North Macedonia": "Macedonia",
                 "Democratic Republic of the Congo": "Dem. Rep. Congo", "Central African Republic": "Central African Rep.", "South Sudan": "S. Sudan",
                 "Dominican Republic": "Dominican Rep.", "State of Palestine": "Palestine", "Ivory Coast": "Côte d'Ivoire", "Eswatini": "eSwatini"}
COUNTRY_SHAPES = {}
def _topo_shapes():
    if not WA or not shape: return
    from shapely.geometry import Polygon, MultiPolygon
    tf = WA.get("transform"); arcs = []
    for a in WA["arcs"]:
        x = y = 0; pts = []
        for p in a:
            if tf: x += p[0]; y += p[1]; pts.append((x * tf["scale"][0] + tf["translate"][0], y * tf["scale"][1] + tf["translate"][1]))
            else: pts.append(tuple(p))
        arcs.append(pts)
    arc = lambda i: arcs[i] if i >= 0 else arcs[~i][::-1]
    ring = lambda ids: [pt for k, i in enumerate(ids) for pt in (arc(i) if k == 0 else arc(i)[1:])]
    for g in WA["objects"]["countries"]["geometries"]:
        try:
            polys = [g["arcs"]] if g["type"] == "Polygon" else g["arcs"] if g["type"] == "MultiPolygon" else []
            geom = MultiPolygon([Polygon(ring(pp[0]), [ring(h) for h in pp[1:]]) for pp in polys]).buffer(0)
            COUNTRY_SHAPES[g["properties"]["name"]] = geom
        except Exception:
            pass
def country_at(lon, lat):
    if not COUNTRY_SHAPES: return None
    from shapely.geometry import Point
    pt = Point(lon, lat)
    for n, g in COUNTRY_SHAPES.items():
        if g.contains(pt): return n
    best = min(COUNTRY_SHAPES.items(), key=lambda kv: kv[1].distance(pt))  # coastal towns can fall just outside the coarse outline
    return best[0] if best[1].distance(pt) < 1.2 else None
_topo_shapes()

# ---------- Wikipedia ----------
API = f"https://{LANG}.wikipedia.org/api/rest_v1"
def summary(lang, title, rev=None):
    if rev: return get(f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(title.replace(' ', '_'), safe='')}/{rev}")
    return get(f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(title.replace(' ', '_'), safe='')}")

def lang_titles(titles):
    """English title -> title in LANG (via interlanguage links)."""
    if LANG == "en": return {t: t for t in titles}
    out = {}
    for i in range(0, len(titles), 50):
        chunk = titles[i:i + 50]
        q = get("https://en.wikipedia.org/w/api.php?action=query&format=json&formatversion=2&redirects=1&prop=langlinks&lllang=" + LANG
                + "&lllimit=max&titles=" + urllib.parse.quote("|".join(chunk)))
        if not q: continue
        qq = q.get("query", {}); alias = {a["from"]: a["to"] for a in qq.get("normalized", []) + qq.get("redirects", [])}
        ll = {p["title"]: (p.get("langlinks") or [{}])[0].get("title") for p in qq.get("pages", [])}
        for t in chunk:
            k = t
            for _ in range(3):
                if k in alias: k = alias[k]
            if ll.get(k): out[t] = ll[k]
    return out

# ---------- news ----------
ORG = re.compile(r"embassy|consulate|headquarters|organi[sz]ation|agency|company|corporation|political party|university|militant|movement|council|court|ministry|government|armed forces|army|bank\b|institute|bureau|committee|airline|newspaper|broadcaster|alliance|treaty|conference|summit|\bpresident\b|politician|born \d", re.I)

OBIT = re.compile(r"^\d{1,2}\.? (de )?\w+ ?:|^\w+ \d{1,2}:|futbolist|calciator|footballeur|fußballspieler|nogometa", re.I)   # obituaries in some "In the news" feeds
TECHY = re.compile(r"security update|vulnerabilit|\breleases? (version|v\d)|\bversion \d|patch(es)? for", re.I)        # software release notes are not world news
def page_title(p):
    return (p.get("titles") or {}).get("normalized") or (p.get("title") or "").replace("_", " ")

def _co(p):
    c = p.get("coordinates") or {}
    return [round(c["lon"], 3), round(c["lat"], 3)] if "lat" in c and "lon" in c else None

def locate(pages, event):
    """Where did it happen? -> (coords, place name or None, country name as on the world map)"""
    is_country = lambda p: COUNTRY_ALIAS.get(page_title(p), page_title(p)) in COUNTRIES
    others = [p for p in pages if p is not event and not is_country(p)]
    for p in ([event] if event else []) + others:
        co = _co(p)
        if not co or ORG.search(p.get("description") or ""): continue
        place = None if p is event else page_title(p)        # an event page has a position, but its name is not a place name
        return co, place, country_at(*co)
    for p in pages:
        if is_country(p):
            return _co(p), None, COUNTRY_ALIAS.get(page_title(p), page_title(p))
    return None, None, None

def news_card(story, pages, event, src_lang, pic=None):
    main = event or sorted(pages[:4], key=lambda p: -len(p.get("extract") or ""))[0]
    c = card_from_page(main, "news")
    c.pop("coords", None)
    c["id"] = "news:" + hashlib.sha1(story[:120].encode()).hexdigest()[:12]
    c["title"] = trim(story, 140); c["headline"] = story
    st = stable(src_lang, page_title(main))
    c["text"] = trim(story + " " + ((st or main).get("extract") or ""))
    if st and st.get("_stable"): c["checked"] = st["_stable"]
    c["topic"] = page_title(main)
    c["links"] = [page_title(p) for p in pages]
    im = good_image(pic) if pic else None
    c.update(im or {"img": "", "thumb": "", "imgW": 0, "imgH": 0})   # no picture of the event itself: the globe shows where it happened
    co, place, ctry = locate(pages, event)
    if not co and src_lang == "en":  # no page with a position: a country named in the story ("Saudi Arabian airports")
        names = sorted(set(COUNTRY_SHAPES) | {k for k, v in COUNTRY_ALIAS.items() if v in COUNTRY_SHAPES}, key=len, reverse=True)
        hit = next((n for n in names if re.search(r"\b" + re.escape(n), story)), None)
        if hit:
            ctry = COUNTRY_ALIAS.get(hit, hit); pt = COUNTRY_SHAPES[ctry].representative_point(); co = [round(pt.x, 2), round(pt.y, 2)]
    if not co and not c.get("img"):  # nowhere to point at: a photo of a person or thing in the story instead
        im = next((good_image(p) for p in pages if good_image(p)), None)
        if im: c.update(im)
    if co: c["coords"] = co
    if place: c["place"] = place
    if ctry: c["country"] = ctry
    c["src"] = src_lang
    return c

def itn_cards(feed, lang_tag):
    """'In the news' from the featured feed (the most important stories of the day)."""
    cards = []
    for n in (feed or {}).get("news", []):
        story_html = n.get("story") or ""; story = strip_html(story_html)
        links = n.get("links") or []
        pages = [p for p in links if ok_page(p)]
        if not story or not pages or BLOCK.search(story) or MINOR.search(story) or OBIT.search(story): continue
        if any(MINOR.search(p.get("description") or "") for p in links): continue
        # the bold link in the story is the event itself; its picture fits the story
        bold = re.search(r"<b>\s*<a [^>]*href=\"\./([^\"#]+)", story_html)
        event = None
        if bold:
            bt = urllib.parse.unquote(bold.group(1)).replace("_", " ")
            event = next((p for p in pages if page_title(p) == bt or ((p.get("titles") or {}).get("canonical") or "").replace("_", " ") == bt), None)
        cards.append(news_card(story, pages, event, lang_tag, pic=event))
    return cards

def fetch_feed(lang, day):
    return get(f"https://{lang}.wikipedia.org/api/rest_v1/feed/featured/{day:%Y/%m/%d}")

# English "Current events" portal: many more serious stories per day, sorted by category
from html.parser import HTMLParser
CE_CATS = ["Armed conflicts and attacks", "International relations", "Politics and elections", "Disasters and accidents",
           "Business and economy", "Science and technology", "Health and environment", "Law and crime"]
CE_ALL = set(CE_CATS) | {"Sports", "Arts and culture", "Arts and entertainment"}
class _N:
    def __init__(s, tag, attrs, parent): s.tag, s.attrs, s.parent, s.kids = tag, dict(attrs), parent, []
    def text(s, skip=lambda n: False):
        return "".join(k if isinstance(k, str) else ("" if skip(k) else k.text(skip)) for k in s.kids)
class _Tree(HTMLParser):
    VOID = {"br", "img", "hr", "meta", "link", "input", "wbr", "source", "col"}
    def __init__(s):
        super().__init__(convert_charrefs=True); s.root = _N("root", {}, None); s.cur = s.root
    def handle_starttag(s, tag, attrs):
        n = _N(tag, attrs, s.cur); s.cur.kids.append(n)
        if tag not in s.VOID: s.cur = n
    def handle_endtag(s, tag):
        k = s.cur
        while k is not s.root and k.tag != tag: k = k.parent
        if k is not s.root: s.cur = k.parent
    def handle_data(s, d): s.cur.kids.append(d)

def _wiki_links(n):
    out = []
    def rec(k):
        for x in k.kids:
            if isinstance(x, str): continue
            h = x.attrs.get("href") or ""
            if x.tag == "a" and h.startswith("/wiki/") and ":" not in h[6:]:
                out.append(urllib.parse.unquote(h[6:].split("#")[0]).replace("_", " "))
            elif not (x.tag == "ul"): rec(x)
    rec(n); return out

# news outlets with real editorial checks; a Current-events story is used only when one of them reported it
TRUSTED = {"reuters.com": "Reuters", "apnews.com": "AP", "bbc.co.uk": "BBC", "bbc.com": "BBC", "afp.com": "AFP", "france24.com": "France 24",
           "dw.com": "DW", "aljazeera.com": "Al Jazeera", "theguardian.com": "The Guardian", "nytimes.com": "The New York Times",
           "washingtonpost.com": "The Washington Post", "npr.org": "NPR", "bloomberg.com": "Bloomberg", "ft.com": "Financial Times",
           "wsj.com": "The Wall Street Journal", "economist.com": "The Economist", "cnn.com": "CNN", "cbc.ca": "CBC", "abc.net.au": "ABC Australia",
           "lemonde.fr": "Le Monde", "spiegel.de": "Der Spiegel", "elpais.com": "El País", "politico.com": "Politico", "politico.eu": "Politico",
           "euronews.com": "Euronews", "kyodonews.net": "Kyodo", "nhk.or.jp": "NHK", "scmp.com": "SCMP", "thehindu.com": "The Hindu",
           "timesofisrael.com": "The Times of Israel", "haaretz.com": "Haaretz", "rfi.fr": "RFI", "voanews.com": "VOA", "un.org": "UN News",
           "who.int": "WHO", "nature.com": "Nature", "science.org": "Science", "nasa.gov": "NASA", "esa.int": "ESA", "cbsnews.com": "CBS News",
           "nbcnews.com": "NBC News", "abcnews.go.com": "ABC News", "sky.com": "Sky News", "rte.ie": "RTÉ", "yonhapnews.co.kr": "Yonhap",
           "en.yna.co.kr": "Yonhap", "africanews.com": "Africanews", "anews.com.tr": "", "irishtimes.com": "The Irish Times", "independent.co.uk": "The Independent"}
TRUSTED = {k: v for k, v in TRUSTED.items() if v}
def _sources(li):
    out = []
    def rec(k):
        for x in k.kids:
            if isinstance(x, str): continue
            if x.tag == "a" and "external" in (x.attrs.get("class") or ""):
                u = x.attrs.get("href") or ""; host = urllib.parse.urlparse(u).netloc.lower().removeprefix("www.")
                name = next((v for d, v in TRUSTED.items() if host == d or host.endswith("." + d)), None)
                out.append({"name": name or re.sub(r"[()]", "", x.text()).strip(), "url": u, "trusted": bool(name)})
            else: rec(x)
    rec(li); return out

def ce_items(day):
    q = get("https://en.wikipedia.org/w/api.php?action=parse&format=json&formatversion=2&prop=text&page="
            + urllib.parse.quote(f"Portal:Current_events/{day:%Y}_{day:%B}_{day.day}"))
    h = ((q or {}).get("parse") or {}).get("text") or ""
    if not h: return []
    t = _Tree(); t.feed(h)
    items, cat = [], None
    skip = lambda k: (k.tag == "a" and "external" in (k.attrs.get("class") or "")) or k.tag in ("sup", "style")
    def do_ul(ul, topics):
        for li in [k for k in ul.kids if isinstance(k, _N) and k.tag == "li"]:
            sub = [k for k in li.kids if isinstance(k, _N) and k.tag == "ul"]
            if sub:
                for u in sub: do_ul(u, topics + _wiki_links(li))
            else:
                txt = re.sub(r"\s+", " ", li.text(skip)).strip()
                txt = re.sub(r"\s*\(\s*\)\s*$", "", txt)
                items.append({"cat": cat, "story": txt, "links": _wiki_links(li), "topics": topics, "sources": _sources(li)})
    def walk(n):
        nonlocal cat
        for k in n.kids:
            if isinstance(k, str): continue
            if k.tag == "ul": do_ul(k, []); continue
            tx = re.sub(r"\s+", " ", k.text()).strip()
            if tx in CE_ALL: cat = tx; continue
            walk(k)
    walk(t.root)
    return items

def ce_cards(days, have, limit):
    """Pick the most serious stories, a few from each category in turn."""
    by = {c: [] for c in CE_CATS}; untrusted = []
    for day in days:
        for it in ce_items(day):
            s = it["story"]
            if it["cat"] not in by or len(s) < 60 or BLOCK.search(s) or MINOR.search(s) or TECHY.search(s): continue
            if not any(x["trusted"] for x in it["sources"]): untrusted.append(s[:70]); continue   # no reliable outlet behind it
            by[it["cat"]].append(it)
    log(f"  current events: " + ", ".join(f"{k.split()[0]} {len(v)}" for k, v in by.items()) + f"; skipped {len(untrusted)} without a trusted source")
    for u in untrusted[:8]: log("    untrusted:", u)
    cards, round_ = [], 0
    while len(cards) < limit and any(round_ < len(v) for v in by.values()):
        for cat in CE_CATS:
            if len(cards) >= limit: break
            if round_ >= len(by[cat]): continue
            it = by[cat][round_]
            titles = list(dict.fromkeys(it["links"] + it["topics"]))[:6]
            if not titles: continue
            pages = [p for p in (summary("en", tt) for tt in titles) if ok_page(p)]
            if not pages or any(MINOR.search(p.get("description") or "") for p in pages): continue
            c = news_card(it["story"], pages, None, "en")
            if c["id"] in have or c["topic"] in have: continue
            c["cat"] = cat; have.add(c["id"]); have.add(c["topic"])
            c["sources"] = [{"name": x["name"], "url": x["url"]} for x in it["sources"] if x["trusted"]][:3]
            cards.append(c)
        round_ += 1
    return cards

# ---------- translation (English news for the other languages) ----------
NLLB_MODEL = "facebook/nllb-200-distilled-1.3B"   # the bigger model translates noticeably better (Slovenian above all)
NLLB = {"sl": "slv_Latn", "de": "deu_Latn", "es": "spa_Latn", "fr": "fra_Latn", "it": "ita_Latn"}
TR_FILE = CACHE / "translations.json"
TR = json.loads(TR_FILE.read_text(encoding="utf-8")) if TR_FILE.exists() else {}
_tm = {}
def _translator():
    if "m" not in _tm:
        _tm["m"] = None
        try:
            import torch
            from transformers import AutoTokenizer, AutoModelForSeq2SeqLM
            name, cd = NLLB_MODEL, str(Path.home() / ".nllb")
            tok = AutoTokenizer.from_pretrained(name, src_lang="eng_Latn", cache_dir=cd)
            model = AutoModelForSeq2SeqLM.from_pretrained(name, cache_dir=cd).eval()
            torch.set_num_threads(max(1, os.cpu_count() or 2))
            _tm["m"] = (tok, model)
            log("  translator ready:", name)
        except Exception as e:
            log("  translator unavailable:", repr(e)[:300])
    return _tm["m"]

def translate(text):
    """English -> LANG. None when no translator is available."""
    if LANG == "en" or not text: return text
    key = hashlib.sha1((NLLB_MODEL + "|" + LANG + "|" + text).encode()).hexdigest()[:16]
    if key in TR: return TR[key]
    m = _translator()
    if not m: return None
    import torch
    tok, model = m
    # sentence by sentence, but not after "U.S." or "Mr."
    sents = [x for x in re.split(r"(?<![A-Z]\.[A-Z]\.)(?<!\b[A-Z][a-z]\.)(?<!\bMrs\.)(?<=[.!?])\s+(?=[A-Z0-9\"“'(])", text) if x.strip()]
    with torch.no_grad():
        enc = tok(sents, return_tensors="pt", padding=True, truncation=True, max_length=200)
        out = model.generate(**enc, forced_bos_token_id=tok.convert_tokens_to_ids(NLLB[LANG]), num_beams=4,
                             no_repeat_ngram_size=4, max_new_tokens=220)
    res = re.sub(r"\s+", " ", " ".join(tok.batch_decode(out, skip_special_tokens=True))).strip()
    TR[key] = res
    return res

def localize_news(cards):
    """English news cards -> this language: translated headline, background from this language's Wikipedia."""
    if LANG == "en": return cards
    loc = lang_titles(list({t for c in cards for t in [c.get("topic"), c.get("place")] if t}))
    out = []
    for c in cards:
        tr = translate(c["headline"])
        if not tr:                                   # no translator: keep the English text, narrated by the English voice
            c["lang"] = "en"; out.append(c); continue
        c["headline"] = tr; c["title"] = trim(tr, 140)
        bg = ""
        if loc.get(c.get("topic")):
            p = stable(LANG, loc[c["topic"]])
            if ok_page(p): bg = p.get("extract") or ""; c["url"] = ((p.get("content_urls") or {}).get("mobile") or {}).get("page") or c["url"]; c["topic"] = page_title(p)
        c["text"] = trim(tr + " " + bg)
        if c.get("place") and loc.get(c["place"]): c["place"] = loc[c["place"]]
        out.append(c)
    return out

# ---------- voices ----------
KOKORO = {"en": ("a", "am_michael"), "es": ("e", "em_alex"), "it": ("i", "im_nicola")}
PIPER = {"de": "de/de_DE/thorsten/high/de_DE-thorsten-high", "fr": "fr/fr_FR/tom/medium/fr_FR-tom-medium", "sl": "sl/sl_SI/artur/medium/sl_SI-artur-medium"}
_engine = {}
def synth_wav(text, wav_path):
    if LANG in KOKORO:
        if "k" not in _engine:
            from kokoro import KPipeline
            _engine["k"] = KPipeline(lang_code=KOKORO[LANG][0])
        import numpy as np, soundfile as sf
        parts = [a for _, _, a in _engine["k"](text, voice=KOKORO[LANG][1], speed=0.95)]
        audio = np.concatenate([p.numpy() if hasattr(p, "numpy") else p for p in parts])
        sf.write(str(wav_path), audio, 24000)
    else:
        if "p" not in _engine:
            base = "https://huggingface.co/rhasspy/piper-voices/resolve/main/" + PIPER[LANG]
            mdir = ROOT / ".cache-geo" / "piper"; mdir.mkdir(parents=True, exist_ok=True)
            onnx = mdir / (Path(PIPER[LANG]).name + ".onnx")
            for suf in ("", ".json"):
                f = Path(str(onnx) + suf)
                if not f.exists():
                    data = get(base + ".onnx" + suf, raw=True)
                    if not data: raise RuntimeError("piper voice download failed")
                    f.write_bytes(data)
            from piper import PiperVoice
            _engine["p"] = PiperVoice.load(str(onnx))
        v = _engine["p"]
        with wave.open(str(wav_path), "wb") as wf:
            if hasattr(v, "synthesize_wav"):
                try:
                    from piper import SynthesisConfig
                    v.synthesize_wav(text, wf, syn_config=SynthesisConfig(length_scale=1.08))
                except Exception:
                    v.synthesize_wav(text, wf)
            else:
                v.synthesize(text, wf, length_scale=1.08)

def voice_id():
    return f"kokoro-{KOKORO[LANG][1]}" if LANG in KOKORO else f"piper-{Path(PIPER[LANG]).name}"

BUDGET = int(os.environ.get("AUDIO_BUDGET", "600")); made = 0; voice_ok = True
def narrate(text):
    """Return the mp3 file name for this text (cached), or '' if no audio could be made."""
    global made, voice_ok
    if not text or not voice_ok: return ""
    h = hashlib.sha1((voice_id() + "|" + text).encode()).hexdigest()[:16] + ".mp3"
    cached = AUDIO_CACHE / h
    if not cached.exists():
        if made >= BUDGET: return ""
        wav = AUDIO_CACHE / (h + ".wav")
        try:
            synth_wav(text, wav)
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav), "-ac", "1", "-ar", "24000", "-codec:a", "libmp3lame", "-b:a", "40k", str(cached)], check=True)
            made += 1
        except Exception as e:
            log("  VOICE ERROR:", repr(e)[:300]); voice_ok = made > 0  # stop trying if the engine never worked
            return ""
        finally:
            if wav.exists(): wav.unlink()
    (OUT / "a" / h).write_bytes(cached.read_bytes())
    return h

# places whose Wikipedia page has no single position, so the globe knows where to turn
PLACE_AT = {"Prime meridian": [0, 51.48], "Equator": [-78.45, 0], "International Date Line": [180, 0], "Ring of Fire": [150, 15],
            "Gulf Stream": [-70, 36], "Central Asia": [66, 43], "Arctic": [0, 84], "El Niño–Southern Oscillation": [-120, 0],
            "Monsoon": [80, 18], "Mariana Trench": [142.2, 11.35], "Sahel": [5, 14], "Horn of Africa": [45, 8], "Siberia": [100, 62],
            "Balkans": [20, 43], "Scandinavia": [15, 63], "Caucasus": [44, 42.5], "Patagonia": [-69, -45], "Great Barrier Reef": [147, -18]}

# ---------- vandalism guard ----------
# Anyone can edit Wikipedia. Bad edits are usually reverted within minutes or hours, so for background facts we read
# the version of the article that has stood for at least STABLE_DAYS days (when the newest edit is more recent than that).
STABLE_DAYS = 3
def stable(lang, title):
    s = summary(lang, title)
    if not s or not ok_page(s): return s
    try:
        ts = datetime.fromisoformat((s.get("timestamp") or "").replace("Z", "+00:00"))
    except ValueError:
        return s
    cut = datetime.now(timezone.utc) - timedelta(days=STABLE_DAYS)
    if ts <= cut: s["_stable"] = s.get("revision") or "current"; return s
    q = get(f"https://{lang}.wikipedia.org/w/api.php?action=query&format=json&formatversion=2&prop=revisions&rvprop=ids|timestamp&rvlimit=1"
            f"&rvstart={cut:%Y-%m-%dT%H:%M:%SZ}&titles=" + urllib.parse.quote(page_title(s)))
    try:
        rev = q["query"]["pages"][0]["revisions"][0]["revid"]
    except Exception:
        return s                          # a brand-new article: nothing older to compare with
    old = summary(lang, page_title(s), rev)
    if ok_page(old):
        if not old.get("coordinates") and s.get("coordinates"): old["coordinates"] = s["coordinates"]
        old["_stable"] = rev; STABLE_HITS.append(title); return old
    return s
STABLE_HITS = []

# capital of each country: its position and its name in this language (shown with a star on the globe)
CAP_LOC = {}
def add_capital(c, country):
    en = CAPITALS[country]
    s = summary("en", en)
    co = _co(s or {})
    if not co: return
    name = re.sub(r"\s*\(.*?\)$", "", CAP_LOC.get(en) or en).split(",")[0].strip()
    c["capital"] = {"name": name, "coords": co}
    im = good_image(s or {})        # usually a skyline, the main square or a landmark: shown as a postcard on the globe
    if im:
        th = im["thumb"]
        c["capital"]["img"] = re.sub(r"/\d+px-", "/640px-", th) if re.search(r"/\d+px-", th) else im["img"]

# ---------- build ----------
def main():
    t0 = time.time()
    now = datetime.now(timezone.utc)
    # news: the day's most important stories ("In the news") in this language, then English ones
    # (In the news + Current events), translated. Sport, celebrities and showbiz are left out.
    news, have = [], set()
    def add(cards):
        for c in cards:
            if c["id"] not in have and c["topic"] not in have:
                news.append(c); have.add(c["id"]); have.add(c["topic"])
    if LANG != "en":
        for back in range(0, 2): add(itn_cards(fetch_feed(LANG, now - timedelta(days=back)), LANG)[:6])
    native = len(news)
    en_cards = []
    for back in range(0, 2): en_cards += itn_cards(fetch_feed("en", now - timedelta(days=back)), "en")[:6]
    en_have = set(have)
    if LANG != "en" and news:  # the same story told in this language already? (matched through interlanguage links)
        loc = lang_titles(list({c["topic"] for c in en_cards}))
        en_cards = [c for c in en_cards if loc.get(c["topic"]) not in have]
    seen_en = set()
    en_cards = [c for c in en_cards if not (c["id"] in seen_en or seen_en.add(c["id"]))]
    for c in en_cards: en_have.add(c["id"]); en_have.add(c["topic"])
    en_cards += ce_cards([now - timedelta(days=d) for d in (0, 1, 2)], en_have, 16 - min(len(en_cards), 8))
    add(localize_news(en_cards[:22 - native]))
    log(f"{LANG}: news {native} own + {len(news) - native} from English; with place: {sum(1 for c in news if c.get('coords'))}, "
        f"photo: {sum(1 for c in news if c.get('img'))}, untranslated: {sum(1 for c in news if c.get('lang'))}")
    for c in news[:30]: log("   ", ("[" + c.get("cat", "itn")[:8] + "]").ljust(11), (c.get("place") or "-")[:20].ljust(20), (c.get("country") or "-")[:14].ljust(14), c["title"][:90])
    for c in news:
        if not c.get("lang"): c["say"] = for_speech(c["text"])
    TR_FILE.write_text(json.dumps(dict(list(TR.items())[-3000:]), ensure_ascii=False), encoding="utf-8")

    # picture of the day
    photo = None
    feed = fetch_feed(LANG, now) or fetch_feed("en", now)
    im = (feed or {}).get("image") or {}
    if im.get("image", {}).get("source"):
        d = strip_html((im.get("description") or {}).get("html") or (im.get("description") or {}).get("text") or "")
        if d and LANG != "en" and not (fetch_feed(LANG, now) or {}).get("image"):  # English caption: translate it
            d = translate(d) or d
        if d and not BLOCK.search(d):
            src = im["image"]["source"]; th = (im.get("thumbnail") or {}).get("source") or src
            big = re.sub(r"/\d+px-", "/1280px-", th) if re.search(r"/\d+px-", th) else src
            photo = {"id": "photo:" + im.get("title", ""), "kind": "photo", "title": trim(d, 120), "text": trim(d), "img": big, "thumb": th,
                     "imgW": im["image"].get("width", 0), "imgH": im["image"].get("height", 0), "url": im.get("file_page") or "",
                     "credit": strip_html((im.get("artist") or {}).get("html") or (im.get("artist") or {}).get("text") or ""), "say": for_speech(trim(d))}

    # topics (kept between runs; refreshed weekly)
    prev_file = CACHE / "topics.json"
    prev = {c["en"]: c for c in json.loads(prev_file.read_text(encoding="utf-8"))} if prev_file.exists() else {}
    titles = lang_titles([t for t, _ in TOPICS])
    CAP_LOC.update(lang_titles(sorted(set(CAPITALS.values()))))
    topics = []
    for t, kind in TOPICS:
        lt = titles.get(t)
        if not lt: continue
        old = prev.get(t)
        if old and old.get("_v") == 4 and old.get("_at", 0) > time.time() - 7 * 86400:
            for k in ("ftype", "geom", "bbox"): old.pop(k, None)
            f = find_feature(t)                      # shapes are cheap: always use the newest matching rules
            if f: old.update(f)
            topics.append(old); continue
        p = stable(LANG, lt)
        if not ok_page(p): continue
        c = card_from_page(p, kind, en=t)
        if kind == "geo" and not c.get("coords") and t in PLACE_AT: c["coords"] = PLACE_AT[t]
        f = find_feature(t)
        if f: c.update(f)
        if t in CAPITALS: add_capital(c, t)
        # "Singapore. Singapore, officially …" sounds clumsy: read the title only when the text doesn't start with it anyway
        first = re.sub(r"\s*\(.*?\)", "", c["title"]).split()[0].lower() if c["title"] else ""
        c["sayTitle"] = not (len(first) >= 3 and first[:max(3, len(first) - 2)] in c["text"][:80].lower())
        c["say"] = for_speech(f"{c['title']}. {c['text']}" if c["sayTitle"] else c["text"])
        c["_at"] = time.time(); c["_v"] = 4
        topics.append(c)
        time.sleep(0.05)
    prev_file.write_text(json.dumps(topics, ensure_ascii=False), encoding="utf-8")
    log(f"{LANG}: {len(STABLE_HITS)} articles had fresh edits, so their {STABLE_DAYS}-day-old version was used")
    log(f"{LANG}: {len(topics)} topics, {sum(1 for c in topics if c.get('geom'))} with shapes")

    # narration: news first (fresh every day), then topics
    for c in news + ([photo] if photo else []) + topics:
        a = narrate(c.get("say", ""))
        if a: c["audio"] = f"content/{LANG}/a/{a}"
    log(f"{LANG}: voice {voice_id()}, {made} new clips, "
        f"{sum(1 for c in news + topics if c.get('audio'))}/{len(news) + len(topics)} cards with audio, voice ok: {voice_ok}")

    clean = lambda cs: [{k: v for k, v in c.items() if not k.startswith("_") and k != "say"} for c in cs]
    (OUT / "news.json").write_text(json.dumps({"date": now.strftime("%Y-%m-%d"), "news": clean(news), "photo": clean([photo])[0] if photo else None}, ensure_ascii=False), encoding="utf-8")
    (OUT / "topics.json").write_text(json.dumps({"topics": clean(topics)}, ensure_ascii=False), encoding="utf-8")
    log(f"{LANG}: done in {time.time() - t0:.0f}s")

try:
    main()
finally:
    (ROOT / "out").mkdir(exist_ok=True)
    (ROOT / "out" / f"build-log-{LANG}.txt").write_text("\n".join(LOG), encoding="utf-8")
